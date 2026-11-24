"""Deployment behaviour tests: host allowlist, proxy IPs, secure cookies, origin,
recovery codes, health check, rate limits, and schema migration.

Each test runs against a temporary SQLite file and restores every environment
variable it touches, so the suite is safe to run on a developer machine.
"""
import http.client
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import server
from fixtures import EXAMPLE

ENV_KEYS = [
    "ZHIYU_HOSTS",
    "ZHIYU_PUBLIC_ORIGIN",
    "ZHIYU_TRUST_PROXY",
    "ZHIYU_SECURE_COOKIE",
    "ZHIYU_RATE_READER",
    "ZHIYU_RATE_DEV",
    "ZHIYU_RATE_READ",
    "ZHIYU_RATE_CLAIM",
    "ZHIYU_DEMO",
    "ZHIYU_BUILD",
]


class Client:
    def __init__(self, base, headers=None):
        self.base = base
        self.headers = headers or {}
        self.agent = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.last = None

    def call(self, path, data=None, headers=None, raw=False):
        merged = {"Content-Type": "application/json"}
        merged.update(self.headers)
        merged.update(headers or {})
        request = urllib.request.Request(
            self.base + "/api/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers=merged,
        )
        with self.agent.open(request, timeout=5) as response:
            self.last = response
            if raw:
                return response
            return json.load(response)


class Deploy(unittest.TestCase):
    def setUp(self):
        self.saved = {key: os.environ.get(key) for key in ENV_KEYS}
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = Path(self.tmp.name) / "test.sqlite3"
        server.READ_WINDOWS.clear()
        server.init()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.dev = Client(self.base)
        self.dev.call("workspace")
        self.pid = self.dev.call("projects", EXAMPLE)["projectId"]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        server.DB = self.old_db
        server.READ_WINDOWS.clear()
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    # --- helpers -----------------------------------------------------------

    def publish(self):
        run = self.dev.call(f"projects/{self.pid}/runs", {"requestId": "deploy-1"})
        draft = dict(
            runId=run["runId"],
            candidateId="interview",
            title="部署测试卡",
            material="脱敏逐字稿",
            steps="准备材料\n核对引文",
            criterion="定位到原文",
            question="哪一句有效？",
        )
        card = self.dev.call(f"projects/{self.pid}/publish", draft)
        return card["slug"]

    def status_of(self, fn, *args, **kwargs):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            fn(*args, **kwargs)
        caught.exception.close()
        return caught.exception.code

    def raw_get(self, path, host):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host)
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        code = response.status
        conn.close()
        return code

    # --- cases -------------------------------------------------------------

    def test_host_allowlist(self):
        os.environ["ZHIYU_HOSTS"] = "example.test,127.0.0.1"
        self.assertEqual(self.raw_get("/api/health", "example.test"), 200)
        self.assertEqual(self.raw_get("/api/health", "127.0.0.1"), 200)
        self.assertEqual(self.raw_get("/api/health", "evil.test"), 403)

    def test_secure_cookie_flag(self):
        os.environ["ZHIYU_SECURE_COOKIE"] = "1"
        fresh = Client(self.base)
        response = fresh.call("workspace", raw=True)
        cookies = response.headers.get_all("Set-Cookie") or []
        self.assertTrue(cookies)
        self.assertTrue(all("Secure" in item for item in cookies))

    def test_origin_allowlist(self):
        os.environ["ZHIYU_PUBLIC_ORIGIN"] = "https://zhiyu.test"
        payload = dict(EXAMPLE, name="来源测试")
        self.dev.call("projects", payload, headers={"Origin": "https://zhiyu.test"})
        self.assertEqual(
            self.status_of(self.dev.call, "projects", payload, headers={"Origin": "https://evil.test"}), 403
        )

    def test_proxy_forwarded_for_splits_buckets(self):
        os.environ["ZHIYU_TRUST_PROXY"] = "1"
        os.environ["ZHIYU_RATE_READER"] = "2"
        slug = self.publish()
        reader = Client(self.base)
        reader.call(f"cards/{slug}")
        for ip in ("198.51.100.1", "198.51.100.2"):
            self.assertEqual(
                reader.call(f"cards/{slug}/visit", {"version": 1}, headers={"X-Forwarded-For": ip}).get("receipt"),
                None,
            )
        reader.call(f"cards/{slug}/visit", {"version": 1}, headers={"X-Forwarded-For": "198.51.100.2"})
        self.assertEqual(
            self.status_of(reader.call, f"cards/{slug}/visit", {"version": 1}, headers={"X-Forwarded-For": "198.51.100.2"}),
            429,
        )
        reader.call(f"cards/{slug}/visit", {"version": 1}, headers={"X-Forwarded-For": "203.0.113.9"})

    def test_reader_rate_limit_without_proxy(self):
        os.environ["ZHIYU_RATE_READER"] = "2"
        slug = self.publish()
        reader = Client(self.base)
        reader.call(f"cards/{slug}")
        reader.call(f"cards/{slug}/visit", {"version": 1})
        reader.call(f"cards/{slug}/visit", {"version": 1})
        self.assertEqual(self.status_of(reader.call, f"cards/{slug}/visit", {"version": 1}), 429)

    def test_rejected_writes_still_consume_quota(self):
        """A 400 must not roll its own rate-limit counter back.

        The counter lives in a separate transaction on purpose: sharing the
        handler's transaction let every rejected request reset the count, so the
        limiter never fired for malformed input.
        """
        os.environ["ZHIYU_RATE_DEV"] = "2"
        bad = {"name": "只有一个字段"}
        self.assertEqual(self.status_of(self.dev.call, "projects", bad), 400)
        self.assertEqual(self.status_of(self.dev.call, "projects", bad), 429)

    def test_claim_attempts_are_capped(self):
        os.environ["ZHIYU_RATE_CLAIM"] = "2"
        bad = {"code": "ZY-AAAA-AAAA"}
        self.assertEqual(self.status_of(self.dev.call, "recovery/claim", bad), 400)
        self.assertEqual(self.status_of(self.dev.call, "recovery/claim", bad), 400)
        self.assertEqual(self.status_of(self.dev.call, "recovery/claim", bad), 429)

    def test_recovery_round_trip(self):
        code = self.dev.call("recovery", {})["code"]
        self.assertRegex(code, r"^ZY-[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")

        other = Client(self.base)
        other.call("workspace")
        self.assertEqual(self.status_of(other.call, "recovery/claim", {"code": "ZY-AAAA-AAAA"}), 400)

        restored = other.call("recovery/claim", {"code": code.lower()})
        self.assertEqual(len(restored["projects"]), 1)
        detail = other.call(f"projects/{self.pid}")
        self.assertEqual(detail["project"]["name"], EXAMPLE["name"])

    def test_health_needs_no_cookie(self):
        fresh = Client(self.base)
        body = fresh.call("health")
        self.assertTrue(body["ok"])
        self.assertEqual(body["db"], "ok")
        self.assertIn("version", body)

    def test_demo_seed_steps_carry_no_list_markers(self):
        """The card renderer wraps each line in <ol><li>; a prefix would show "1. 1. ..."."""
        server.seed_demo()
        with closing(sqlite3.connect(server.DB)) as conn:
            steps = json.loads(conn.execute("SELECT data FROM cards").fetchone()[0])["steps"]
        self.assertTrue(steps.strip())
        for line in steps.splitlines():
            self.assertNotRegex(
                line.strip(),
                r"^(?:\d+\s*[.、)]|[一二三四五六七八九十]+\s*[、.])",
                f"演示步骤自带序号，前端会再编一次号：{line!r}",
            )

    def test_old_schema_migrates(self):
        legacy = Path(self.tmp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY,owner TEXT NOT NULL,data TEXT NOT NULL)")
            conn.execute("INSERT INTO projects VALUES('p1','o1',?)", (json.dumps({"name": "旧项目"}),))
            # A card table from before provenance was recorded must not block startup.
            conn.execute(
                "CREATE TABLE cards(slug TEXT PRIMARY KEY,pid TEXT NOT NULL,version INTEGER NOT NULL,"
                "signature TEXT NOT NULL,data TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1)"
            )
        server.DB = legacy
        server.init()
        server.init()  # 幂等
        with closing(sqlite3.connect(legacy)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
            self.assertIn("created_at", columns)
            self.assertIn("updated_at", columns)
            self.assertEqual(json.loads(conn.execute("SELECT data FROM projects").fetchone()[0])["name"], "旧项目")
            self.assertEqual(conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0], "2")
            card_columns = {row[1] for row in conn.execute("PRAGMA table_info(cards)")}
            for name in ("published_at", "run_id", "candidate_id"):
                self.assertIn(name, card_columns)


class Retention(unittest.TestCase):
    """`runs` grows on every match request, so it is the one table needing a cap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = Path(self.tmp.name) / "retention.sqlite3"
        server.init()

    def tearDown(self):
        server.DB = self.old_db
        self.tmp.cleanup()

    def add_run(self, run_id, age_days):
        with closing(sqlite3.connect(server.DB)) as conn, conn:
            conn.execute(
                "INSERT INTO runs(id,pid,rid,data,created_at) VALUES(?,?,?,?,?)",
                (run_id, "p1", run_id, json.dumps({"status": "ok"}), server.now() - age_days * 86400),
            )

    def add_card(self, slug, run_id):
        with closing(sqlite3.connect(server.DB)) as conn, conn:
            conn.execute(
                "INSERT INTO cards(slug,pid,version,signature,data,active,published_at,run_id,candidate_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (slug, "p1", 1, "sig", "{}", 1, server.now(), run_id, "c1"),
            )

    def run_ids(self):
        with closing(sqlite3.connect(server.DB)) as conn:
            return sorted(row[0] for row in conn.execute("SELECT id FROM runs"))

    def test_old_unreferenced_runs_are_removed(self):
        self.add_run("old", 100)
        self.add_run("fresh", 1)
        report = server.prune(days=90)
        self.assertEqual(self.run_ids(), ["fresh"])
        self.assertEqual((report["candidates"], report["deleted"], report["remaining"]), (1, 1, 1))

    def test_runs_behind_a_card_are_kept_forever(self):
        self.add_run("old", 400)
        self.add_card("s1", "old")
        server.prune(days=90)
        self.assertEqual(self.run_ids(), ["old"], "已发布卡片的来源运行不能删，否则反馈追不回当时展示的内容")

    def test_dry_run_deletes_nothing(self):
        self.add_run("old", 100)
        report = server.prune(days=90, dry_run=True)
        self.assertEqual((report["candidates"], report["deleted"], report["dryRun"]), (1, 0, True))
        self.assertEqual(self.run_ids(), ["old"])

    def test_default_window_is_ninety_days(self):
        self.add_run("inside", 89)
        self.add_run("outside", 91)
        report = server.prune()
        self.assertEqual(report["days"], 90)
        self.assertEqual(self.run_ids(), ["inside"])

    def test_pruning_leaves_counts_alone(self):
        self.add_run("old", 200)
        with closing(sqlite3.connect(server.DB)) as conn, conn:
            conn.execute("INSERT INTO visits(slug,reader,opened,first_seen) VALUES('s1','r1',1,1)")
            conn.execute("INSERT INTO feedback VALUES('s1','r1','sid','done','','ok',1)")
        server.prune(days=90)
        with closing(sqlite3.connect(server.DB)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM visits").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM feedback").fetchone()[0], 1)


class ScheduledJobs(unittest.TestCase):
    """`deploy/crontab` is a call-by-path contract.

    A renamed script or a lost exec bit fails silently at 03:00 while the same
    command works by hand in `bash scripts/x.sh` — that is exactly how the
    backups went missing once. These two checks run before deployment instead of
    depending on someone remembering to run the preflight on the host.
    """

    ROOT = Path(__file__).resolve().parent

    def crontab_refs(self):
        text = (self.ROOT / "deploy" / "crontab").read_text(encoding="utf-8")
        return sorted(set(re.findall(r"/srv/zhiyu/app/((?:scripts|deploy)/[\w.-]+)", text)))

    def test_crontab_calls_only_files_that_exist(self):
        refs = self.crontab_refs()
        self.assertIn("scripts/backup.sh", refs, "crontab 里至少要有一条备份任务")
        missing = [r for r in refs if not (self.ROOT / r).is_file()]
        self.assertEqual(missing, [], f"crontab 引用了不存在的文件：{missing}")

    def test_scheduled_scripts_are_executable_in_git(self):
        refs = [r for r in self.crontab_refs() if r.endswith(".sh") or r.endswith(".py")]
        self.assertTrue(refs, "crontab 里没有可执行脚本")
        try:
            out = subprocess.run(
                ["git", "ls-files", "-s", *refs],
                cwd=self.ROOT, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            self.skipTest("没有 git，跳过执行位检查")
        if out.returncode != 0 or not out.stdout.strip():
            self.skipTest("不在 git 仓库里，跳过执行位检查")
        modes = {line.split("\t")[-1]: line.split()[0] for line in out.stdout.strip().splitlines()}
        not_exec = [name for name, mode in modes.items() if mode != "100755"]
        self.assertEqual(not_exec, [], f"这些脚本在 git 里不是 755，cron 按路径调用会静默失败：{not_exec}")


class WatchScript(unittest.TestCase):
    """`scripts/watch.sh` runs unattended every five minutes; each check here
    covers a way it used to fail quietly.

    * A trailing option with no value left `$1` in place and the parse loop spun
      forever — under cron that is a new hung process every five minutes.
    * A state file that cannot be written restarts the failure count on every
      run, so the threshold is never reached and a dead service never alerts.
    * A proxy in the environment answers a loopback probe with its own page, so
      a healthy service reads as down (and a dead one can read as up). That one
      is a source-level contract: the test runner and the shell it spawns do not
      necessarily share a loopback interface.

    Every path handed to the shell is relative to the repository. An absolute
    Windows path is not a path the shell sees, and passing one is how a scratch
    directory named `C:` ended up inside the checkout.
    """

    ROOT = Path(__file__).resolve().parent
    WATCH = "scripts/watch.sh"

    def run_watch(self, *args, timeout=30):
        env = {k: v for k, v in os.environ.items() if not k.lower().startswith("http_proxy")}
        return subprocess.run(
            ["bash", self.WATCH, *args],
            cwd=self.ROOT, capture_output=True, text=True, timeout=timeout, env=env,
        )

    def setUp(self):
        try:
            probe = subprocess.run(["bash", "--version"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("没有 bash，跳过 shell 脚本行为检查")
        if probe.returncode != 0:
            self.skipTest("bash 不可用")
        self.rel = Path("var") / f"wbtest-{os.getpid()}"
        (self.ROOT / self.rel).mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.ROOT / self.rel, True)

    def test_option_without_value_fails_fast(self):
        res = self.run_watch("--url", timeout=15)
        self.assertEqual(res.returncode, 2, "缺值应当立即退出 2，而不是挂起")
        self.assertIn("needs a value", res.stderr)

    def test_unwritable_state_is_loud_not_silent(self):
        state = self.rel / "state"
        (self.ROOT / state).mkdir()          # a directory cannot be replaced by a file
        res = self.run_watch(
            "--url", "http://127.0.0.1:9/api/health",
            "--state", state.as_posix(),
            "--disk-min", "0",
            timeout=30,
        )
        self.assertEqual(res.returncode, 2, "状态写不下去时必须以 2 退出，不能静默当作正常")
        self.assertIn("状态", res.stderr)

    def test_loopback_curl_calls_bypass_the_proxy(self):
        offenders = []
        for name in ("scripts/watch.sh", "scripts/preflight.sh", "scripts/restore-check.sh"):
            for line in (self.ROOT / name).read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or "curl " not in stripped:
                    continue
                if "127.0.0.1" in stripped and "--noproxy" not in stripped:
                    offenders.append(f"{name}: {stripped}")
        self.assertEqual(offenders, [], f"回环 curl 必须带 --noproxy，否则代理会替服务答话：{offenders}")


class PreflightDocs(unittest.TestCase):
    """The preflight's item count is quoted in four documents, and it was wrong
    in all four: the group tallies add up to 43, the docs said 44. Nobody counts
    43 rows by hand, so the number is derived from the script here and compared
    with the sentence that publishes it.
    """

    ROOT = Path(__file__).resolve().parent
    PATTERNS = {
        "docs/DEPLOY.md": r"满配共 (\d+) 项",
        "docs/RUNBOOK.md": r"(\d+) 项分七组",
        "README.md": r"一条命令查 (\d+) 项",
        "docs/进度与交接.md": r"预检 (\d+) 项",
    }

    def test_documents_quote_the_real_item_count(self):
        text = (self.ROOT / "scripts" / "preflight.sh").read_text(encoding="utf-8")
        count = len(set(re.findall(r'(?:pass|fail|warn|skip)\s+"([A-G]\d+)', text)))
        self.assertGreater(count, 30, "没解析到检查项编号，preflight.sh 的标签格式变了")
        for name, pattern in self.PATTERNS.items():
            body = (self.ROOT / name).read_text(encoding="utf-8")
            found = re.findall(pattern, body)
            self.assertTrue(found, f"{name} 里找不到「{pattern}」，这句话是项数的唯一来源，别删")
            for value in found:
                self.assertEqual(int(value), count, f"{name} 写的项数与 preflight.sh 实际的 {count} 项不一致")


class PreflightPlaintextOrigin(unittest.TestCase):
    """The preflight is the last gate before a URL goes public. Two checks
    misjudged a plaintext-IP deployment:

    * B6 compared the whole `host:port` from PUBLIC_ORIGIN against a HOSTS
      allowlist that (correctly) holds bare hosts, so every IP+port origin
      failed with a bogus 403 warning.
    * A4 demanded a reverse proxy unconditionally. With no domain there is no
      TLS to terminate, so the missing proxy is intentional, not a failure.

    Both are exercised by running preflight against a fake env file; no real
    host or network is involved.
    """

    ROOT = Path(__file__).resolve().parent
    PREFLIGHT = "scripts/preflight.sh"

    def setUp(self):
        try:
            subprocess.run(["bash", "--version"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("没有 bash，跳过 preflight 行为检查")
        self.rel = Path("var") / f"pfplain-{os.getpid()}"
        (self.ROOT / self.rel).mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.ROOT / self.rel, True)

    def run_preflight(self, env_lines, with_fake_systemctl=False):
        envf = self.rel / "env"
        (self.ROOT / envf).write_bytes(("\n".join(env_lines) + "\n").encode("utf-8"))
        environ = {k: v for k, v in os.environ.items() if not k.lower().startswith("http_proxy")}
        bindir = self.rel / "bin"
        bindir.mkdir(exist_ok=True)
        # A fake systemctl that reports no active caddy/nginx, so A4 reaches
        # its plaintext-vs-https branch instead of skipping for lack of systemctl.
        fake = bindir / "systemctl"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "case \"$1\" in is-active) exit 1 ;; *) exit 0 ;; esac\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        environ["PATH"] = bindir.as_posix() + os.pathsep + environ.get("PATH", "")
        cmd = ["bash", self.PREFLIGHT, "--skip-net",
               "--env-file", envf.as_posix(), "--app-dir", self.rel.as_posix()]
        return subprocess.run(cmd, cwd=self.ROOT, capture_output=True, text=True,
                              timeout=30, env=environ)

    def test_b6_accepts_origin_with_port_when_bare_host_is_allowed(self):
        out = self.run_preflight([
            "ZHIYU_DB=/srv/zhiyu/var/zhiyu.sqlite3",
            "ZHIYU_PORT=4173",
            "ZHIYU_HOSTS=localhost,47.238.166.125",
            "ZHIYU_PUBLIC_ORIGIN=http://47.238.166.125:4173",
            "ZHIYU_SECURE_COOKIE=0",
        ]).stdout
        self.assertIn("[PASS] B6", out, out)
        self.assertNotIn("[FAIL] B6", out, "带端口的明文 origin 不该再被判成白名单不符")

    def test_a4_plaintext_origin_skips_proxy_but_https_requires_it(self):
        plain = self.run_preflight([
            "ZHIYU_DB=/srv/zhiyu/var/zhiyu.sqlite3",
            "ZHIYU_HOSTS=localhost,47.238.166.125",
            "ZHIYU_PUBLIC_ORIGIN=http://47.238.166.125:4173",
            "ZHIYU_SECURE_COOKIE=0",
        ], with_fake_systemctl=True).stdout
        self.assertIn("[SKIP] A4", plain, plain)
        self.assertNotIn("[FAIL] A4", plain)

        secure = self.run_preflight([
            "ZHIYU_DB=/srv/zhiyu/var/zhiyu.sqlite3",
            "ZHIYU_HOSTS=localhost,zhiyu.example.com",
            "ZHIYU_PUBLIC_ORIGIN=https://zhiyu.example.com",
            "ZHIYU_SECURE_COOKIE=1",
        ], with_fake_systemctl=True).stdout
        self.assertIn("[FAIL] A4", secure, "HTTPS 没有反代仍必须报失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
