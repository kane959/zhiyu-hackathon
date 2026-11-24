#!/usr/bin/env python3
"""Zhiyu 黑盒验收。只看 HTTP 行为，不 import 服务端代码。

两种用法：

    python scripts/accept.py                 # 自己起一个临时实例（临时库、随机端口），跑完销毁
    python scripts/accept.py --base URL      # 验收已在跑的服务（本地或线上）

退出码 0 只在全部通过时返回，方便塞进 CI 或交给别人复现。
"""
import argparse
import http.client
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
PROJECT = dict(
    name="验收工具",
    url="/sample",
    audience="验收用工作区",
    material="一段脱敏的中文访谈文字稿",
    task="从访谈稿中找出支持研究结论的原话",
    output="三句带原文位置的候选引文",
    limits="只接收文字，不处理录音。",
)

PASS, FAIL = [], []


def check(name, fn):
    try:
        detail = fn() or ""
        PASS.append(name)
        print(f"  [通过] {name}{('  ' + detail) if detail else ''}")
    except AssertionError as exc:
        FAIL.append((name, str(exc)))
        print(f"  [失败] {name}  {exc}")
    except Exception as exc:  # noqa: BLE001 - 验收脚本要把任何异常都记成失败，而不是崩掉
        FAIL.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"  [失败] {name}  {type(exc).__name__}: {exc}")


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def opener_for(url, jar=None):
    """本机地址绕开系统代理，否则 HTTP_PROXY 会把 127.0.0.1 也拦走，报出误导性的 502。"""
    handlers = []
    if (urlsplit(url).hostname or "") in LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = opener_for(base, self.jar)

    def call(self, path, data=None, headers=None, expect=200):
        """expect=200 时断言并返回 body；expect=None 时返回 (status, body)。"""
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        request = urllib.request.Request(
            self.base + "/api/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=merged,
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
            error.close()
        body = json.loads(raw.decode() or "{}") if raw else {}
        if expect is None:
            return status, body
        assert status == expect, f"{path} 期望 {expect}，实际 {status}"
        return body

    def get_page(self, path, expect=200):
        try:
            with self.opener.open(self.base + path, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, b""


def raw_status(base, path, host):
    parts = urlsplit(base)
    client = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = client(parts.hostname, parts.port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host)
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


# --- 六组验收 -------------------------------------------------------------

def group_available(base):
    print("\nA 服务可用")
    fresh = Client(base)

    def health():
        body = fresh.call("health")
        assert body.get("ok") is True, "ok 不为 true"
        assert body.get("db") == "ok", "db 状态异常"
        assert body.get("version"), "缺少 version，无法核对提交版本"
        return f"build={body['version']}"

    def pages():
        for path, kind in (("/", "text/html"), ("/recover", "text/html"), ("/styles.css", "text/css")):
            status, _ = fresh.get_page(path)
            assert status == 200, f"{path} 返回 {status}"
        assert kind

    def health_no_cookie():
        assert not list(fresh.jar), "健康检查不应该下发 Cookie"
        return "免 Cookie"

    check("GET /api/health 返回 ok/db/version", health)
    check("首页、恢复页、样式表可打开", pages)
    check("健康检查不依赖 Cookie", health_no_cookie)


def group_isolation(base):
    print("\nB 工作区隔离")
    owner, other = Client(base), Client(base)
    state = {}

    def create():
        owner.call("workspace")  # 真实前端总是先取工作台，会话由此建立
        body = owner.call("projects", PROJECT)
        state["pid"] = body["projectId"]
        assert body["name"] == PROJECT["name"], "回写的项目名不符"
        return state["pid"]

    def invisible():
        assert other.call("workspace")["projects"] == [], "另一个工作区看到了别人的项目"

    def direct_denied():
        other.call(f"projects/{state['pid']}", expect=404)

    def owner_visible():
        assert owner.call(f"projects/{state['pid']}")["project"]["name"] == PROJECT["name"]

    check("新工作区可登记项目", create)
    check("另一个工作区列表为空", invisible)
    check("直接猜 URL 访问他人项目 → 404", direct_denied)
    check("本人仍能读到自己的项目", owner_visible)


def group_publish(base):
    print("\nC 匹配与发布")
    owner = Client(base)
    owner.call("workspace")
    pid = owner.call("projects", PROJECT)["projectId"]
    state = {"pid": pid}

    def runs():
        body = owner.call(f"projects/{pid}/runs", {"requestId": "accept-1"})
        assert body["status"] in ("success", "empty"), f"status={body['status']}"
        assert body["candidates"], "没有候选"
        assert all("judgment" in item for item in body["candidates"]), "候选缺判断字段"
        state["run"] = body
        return f"{len(body['candidates'])} 个候选"

    def idempotent():
        again = owner.call(f"projects/{pid}/runs", {"requestId": "accept-1"})
        assert again["runId"] == state["run"]["runId"], "同一 requestId 生成了两个 run"

    def live_not_configured():
        body = owner.call(f"projects/{pid}/runs", {"requestId": "accept-live", "mode": "live"})
        assert body["errorCode"] == "not_configured", "真实模式应明确返回未配置"

    def publish():
        candidate = next(item for item in state["run"]["candidates"] if item["judgment"] == "适配")
        draft = dict(
            runId=state["run"]["runId"],
            candidateId=candidate["id"],
            title="验收用卡片",
            material="脱敏逐字稿",
            steps="准备材料\n核对引文",
            criterion="能定位到原文",
            question="哪一句不能支持结论？",
        )
        card = owner.call(f"projects/{pid}/publish", draft)
        assert card["version"] == 1, f"首版应为 1，实际 {card['version']}"
        state["card"], state["draft"] = card, draft
        return f"slug={card['slug']}"

    def publish_same():
        again = owner.call(f"projects/{pid}/publish", state["draft"])
        assert again["slug"] == state["card"]["slug"], "相同快照重复发布生成了新版本"

    def reader_view():
        reader = Client(base)
        card = reader.call(f"cards/{state['card']['slug']}")
        for leaked in ("owner", "projectSnapshot"):
            assert leaked not in card, f"公开卡片泄露了 {leaked}"
        assert card["version"] == 1
        state["reader"] = reader
        return "无内部字段泄露"

    def traceable():
        cards = owner.call(f"projects/{pid}")["cards"]
        assert cards, "项目详情没有返回卡片"
        latest = cards[0]
        assert latest["runId"] == state["run"]["runId"], "卡片没有记录来源运行"
        assert latest["publishedAt"] > 0, "卡片没有发布时间"
        public = state["reader"].call(f"cards/{latest['slug']}")
        for leaked in ("runId", "candidateId"):
            assert leaked not in public, f"公开卡片泄露了 {leaked}"
        return f"runId={latest['runId'][:8]}…"

    def mode_conflict():
        status, _ = owner.call(
            f"projects/{pid}/runs", {"requestId": "accept-1", "mode": "live"}, expect=None
        )
        assert status == 409, f"同一 requestId 切换模式应返回 409，实际 {status}"
        return "合成与真实不能共用 requestId"

    check("匹配运行返回候选与判断", runs)
    check("相同 requestId 幂等", idempotent)
    check("真实模式返回 not_configured", live_not_configured)
    check("同一 requestId 不可切换模式", mode_conflict)
    check("发布生成 v1 卡片", publish)
    check("相同快照重复发布不升版本", publish_same)
    check("读者可见且不含内部字段", reader_view)
    check("卡片可追溯到运行", traceable)
    state["owner"] = owner
    group_publish.state = state


def group_events(base, state):
    print("\nD 事件与反馈")
    slug = state["card"]["slug"]
    reader, owner = state["reader"], state["owner"]

    def events():
        for path in ("visit", "visit", "open", "open"):
            reader.call(f"cards/{slug}/{path}", {"version": 1})
        return "重复事件已去重"

    def feedback():
        receipt = reader.call(
            "cards/" + slug + "/feedback",
            {"version": 1, "submissionId": "accept-sub-1", "status": "done", "step": "", "note": "验收通过"},
        )["receipt"]
        assert receipt["submissionId"] == "accept-sub-1"
        again = reader.call(
            "cards/" + slug + "/feedback",
            {"version": 1, "submissionId": "accept-sub-1", "status": "done", "step": "", "note": "验收通过"},
        )["receipt"]
        assert again == receipt, "同一 submissionId 产生了不同回执"

    def blocked_needs_step():
        third = Client(base)
        third.call(f"cards/{slug}")
        third.call(f"cards/{slug}/visit", {"version": 1})
        third.call(
            f"cards/{slug}/feedback",
            {"version": 1, "submissionId": "accept-sub-2", "status": "blocked", "step": "", "note": ""},
            expect=400,
        )

    def stats():
        block = next(v for v in owner.call(f"projects/{state['pid']}/results")["versions"] if v["slug"] == slug)
        assert block["visits"] == 2, f"visits={block['visits']}，期望 2"
        assert block["opened"] == 1, f"opened={block['opened']}，期望 1"
        assert block["completed"] == 1, f"completed={block['completed']}，期望 1"
        assert len(block["feedback"]) == 1, f"feedback={len(block['feedback'])}，期望 1"
        return "visits=2 opened=1 completed=1"

    def second_answer_rejected():
        reader.call(
            "cards/" + slug + "/feedback",
            {"version": 1, "submissionId": "accept-sub-9", "status": "blocked", "step": "第二步", "note": "改口"},
            expect=409,
        )

    def version_guard():
        reader.call(f"cards/{slug}/visit", {"version": 99}, expect=409)

    def withdraw():
        owner.call(f"projects/{state['pid']}/withdraw/{slug}", {})
        reader.call(f"cards/{slug}", expect=404)

    check("重复 visit/open 不重复计数", events)
    check("反馈提交幂等并返回回执", feedback)
    check("同会话换 submissionId → 409", second_answer_rejected)
    check("blocked 缺步骤 → 400", blocked_needs_step)
    check("看板四项统计对得上", stats)
    check("版本不符 → 409", version_guard)
    check("撤回后读者 404", withdraw)


def group_guards(base):
    print("\nE 输入与限流")
    client = Client(base)

    def host():
        assert raw_status(base, "/api/health", "evil.test") == 403, "伪造 Host 未被拒绝"
        return "伪造 Host → 403"

    def content_type():
        client.call("projects", PROJECT, headers={"Content-Type": "text/plain"}, expect=415)

    def oversize():
        client.call("projects", {"name": "x" * 30000}, expect=413)

    def bad_origin():
        client.call("projects", PROJECT, headers={"Origin": "https://evil.test"}, expect=403)

    check("Host 白名单拦截", host)
    check("非 JSON → 415", content_type)
    check("超 24 KB → 413", oversize)
    check("跨站 Origin → 403", bad_origin)


def group_recovery(base, pid, origin):
    print("\nF 恢复码")
    other = Client(base)
    state = {}

    def issue():
        code = origin.call("recovery", {})["code"]
        assert len(code) == 12 and code.startswith("ZY-"), "恢复码格式异常"
        state["code"] = code
        return "格式通过（不输出恢复码）"

    def claim():
        body = other.call("recovery/claim", {"code": state["code"].lower()})
        assert any(item["projectId"] == pid for item in body["projects"]), "认领后看不到原项目"
        other.call(f"projects/{pid}")
        return "换浏览器拿回项目"

    def rotate():
        fresh = origin.call("recovery", {})["code"]
        assert fresh != state["code"], "生成新码后旧码仍然相同"
        third = Client(base)
        third.call("recovery/claim", {"code": state["code"]}, expect=400)
        return "旧码已失效"

    def cap():
        probe = Client(base)
        seen = []
        for _ in range(8):
            status, _ = probe.call("recovery/claim", {"code": "ZY-AAAA-AAAA"}, expect=None)
            seen.append(status)
            if status == 429:
                break
        assert seen[-1] == 429, f"连续 {len(seen)} 次试错仍未限流：{seen}"
        return f"第 {len(seen)} 次 → 429"

    check("生成恢复码格式正确", issue)
    check("另一浏览器认领后可读项目", claim)
    check("轮换后旧码失效", rotate)
    check("连续试错触发限流", cap)


# --- 入口 ----------------------------------------------------------------

def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fail(message):
    """打印到 stdout 再退出，避免提示跑到 stderr 里被日志重定向丢掉。"""
    print(message, flush=True)
    raise SystemExit(1)


def check_base(base):
    """发请求前先拦掉占位符和中文域名。

    urllib 把非 ASCII 主机塞进 Host 头时抛 UnicodeEncodeError，traceback 有十几行，
    看不出真正原因。在这里先给一句人话。
    """
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        fail(
            f"地址不完整：{base}\n"
            "要带 http:// 或 https:// 和域名，例如 https://zhiyu.example.com"
        )
    host = parts.hostname
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        fail(
            f"域名里含中文：{host}\n"
            "这里要填真实的域名，不是文档里的占位符。例如 --base https://zhiyu.example.com\n"
            "还没部署就先别带 --base，直接跑本地自起实例。"
        )
    lowered = host.lower()
    for token in ("example.com", "example.org", "your-domain", "yourdomain", "xxx"):
        if token in lowered:
            fail(
                f"域名看起来仍是占位符：{host}\n"
                f"把 --base 换成你实际能访问的地址，或用本机实例：--base http://127.0.0.1:4173"
            )


def probe(base):
    """先确认这个地址后面确实是知遇，避免把部署问题报成 27 项功能失败。"""
    try:
        with opener_for(base).open(base + "/api/health", timeout=10) as response:
            raw = response.read()
    except UnicodeEncodeError:
        fail(f"地址里有无法直接发送的字符：{base}\n换成 ASCII 域名再试。")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:160]
        code = error.code
        error.close()
        if code == 503:
            fail(
                f"{base}/api/health 返回 503：{detail}\n"
                "进程在跑，但数据库不可用。先看 journalctl -u zhiyu -n 100，多半是 ZHIYU_DB 路径不可写。"
            )
        fail(
            f"{base}/api/health 返回 HTTP {code}。\n"
            "这个地址后面不是知遇，或反向代理没把请求转给应用。先按 docs/DEPLOY.md 完成部署。"
        )
    except OSError as error:
        fail(
            f"{base}/api/health 连不上：{error}\n"
            "检查域名解析、安全组 80/443，以及 systemd 是否在跑。代码没部署到这台机器时，这一步必然失败。"
        )
    try:
        body = json.loads(raw.decode() or "{}")
    except ValueError:
        fail(f"{base}/api/health 返回的不是 JSON，多半是反代默认页或站点首页。这个地址后面不是知遇。")
    if body.get("ok") is not True:
        fail(f"{base}/api/health 返回异常：{str(body)[:200]}")
    return body


def spawn():
    tmp = tempfile.mkdtemp(prefix="zhiyu-accept-")
    port = free_port()
    env = os.environ.copy()
    env.update(
        ZHIYU_DB=str(Path(tmp) / "accept.sqlite3"),
        ZHIYU_BUILD="accept-local",
        ZHIYU_RATE_DEV="100000",
        ZHIYU_RATE_READER="100000",
        ZHIYU_PUBLIC_ORIGIN="https://accept.test",
        ZHIYU_RATE_CLAIM="2",
    )
    env.pop("ZHIYU_DEMO", None)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            with opener_for(base).open(base + "/api/health", timeout=1) as response:
                if response.status == 200:
                    return base, process, tmp
        except OSError:
            time.sleep(0.25)
    process.kill()
    raise SystemExit("临时实例未能在 10 秒内启动：" + process.stderr.read().decode(errors="replace")[:400])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="验收一个已在运行的地址，例如 https://zhiyu.example.com")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    process = tmp = None
    if args.base:
        base = args.base.rstrip("/")
        check_base(base)
        print(f"验收目标：{base}")
        print("注意：会在这个地址上创建测试工作区、卡片和反馈，跑完不会自动删除。")
    else:
        base, process, tmp = spawn()
        print(f"验收目标：{base}（临时库，跑完销毁）")

    started = time.time()
    try:
        health = probe(base)
        print(f"目标确认：build={health.get('version')}，数据库 {health.get('db')}")
        group_available(base)
        group_isolation(base)
        group_publish(base)
        state = group_publish.state
        state["owner_jar"] = Client(base).jar
        group_events(base, state)
        group_guards(base)
        group_recovery(base, state["pid"], state["owner"])
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    total = len(PASS) + len(FAIL)
    print(f"\n{'=' * 52}")
    print(f"通过 {len(PASS)}/{total}，耗时 {time.time() - started:.1f}s")
    if FAIL:
        print("未通过：")
        for name, reason in FAIL:
            print(f"  - {name}：{reason}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
