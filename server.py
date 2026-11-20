"""Zhiyu prototype server. Python 3.10+, standard library only.

Runtime knobs come from environment variables; see .env.example and docs/DEPLOY.md.
The process binds to loopback by default; put a TLS reverse proxy in front for public use.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import closing
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from fixtures import EXAMPLE, SOURCES
from matcher import run as match_run

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("ZHIYU_DB", ROOT / "var" / "zhiyu.sqlite3"))

CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
READ_WINDOWS = {}  # in-memory read counter: {(ip, minute): count}
WRITE_LOCK = threading.Lock()  # SQLite allows one writer; serialize writes in-process


# --- configuration ---------------------------------------------------------

def env(key, default=""):
    return os.environ.get(key, default)


def env_int(key, default):
    try:
        return max(1, int(env(key, str(default))))
    except ValueError:
        return default


def env_flag(key):
    return env(key, "0").strip().lower() in ("1", "true", "yes", "on")


def allowed_hosts():
    raw = env("ZHIYU_HOSTS", "localhost,127.0.0.1")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def public_origin():
    return env("ZHIYU_PUBLIC_ORIGIN").rstrip("/")


def build_id():
    return env("ZHIYU_BUILD", "dev")


def rate_cap(bucket):
    defaults = {"reader": 30, "dev": 120, "read": 600, "claim": 5}
    return env_int("ZHIYU_RATE_" + bucket.upper(), defaults[bucket])


# --- small helpers ---------------------------------------------------------

def uid():
    return secrets.token_urlsafe(18)


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def now():
    return int(time.time())


def make_code():
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return "ZY-" + body[:4] + "-" + body[4:]


def normalize_code(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def code_hash(code):
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


class Problem(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg


def ck(condition, code, msg):
    if not condition:
        raise Problem(code, msg)


# --- storage ---------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY,
  pid TEXT NOT NULL,
  rid TEXT NOT NULL,
  data TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0,
  UNIQUE(pid, rid)
);
CREATE TABLE IF NOT EXISTS cards(
  slug TEXT PRIMARY KEY,
  pid TEXT NOT NULL,
  version INTEGER NOT NULL,
  signature TEXT NOT NULL,
  data TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  published_at INTEGER NOT NULL DEFAULT 0,
  run_id TEXT NOT NULL DEFAULT '',
  candidate_id TEXT NOT NULL DEFAULT '',
  UNIQUE(pid, version)
);
CREATE TABLE IF NOT EXISTS visits(
  slug TEXT NOT NULL,
  reader TEXT NOT NULL,
  opened INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER NOT NULL DEFAULT 0,
  opened_at INTEGER,
  PRIMARY KEY(slug, reader)
);
CREATE TABLE IF NOT EXISTS feedback(
  slug TEXT NOT NULL,
  reader TEXT NOT NULL,
  sid TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('done','blocked')),
  step TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  at INTEGER NOT NULL,
  PRIMARY KEY(slug, reader),
  UNIQUE(slug, sid)
);
CREATE TABLE IF NOT EXISTS limits(
  k TEXT PRIMARY KEY,
  window INTEGER NOT NULL,
  n INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery(
  code_hash TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  created_at INTEGER NOT NULL DEFAULT 0,
  last_used INTEGER
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

EXTRA_COLUMNS = {
    "projects": [("created_at", "INTEGER NOT NULL DEFAULT 0"), ("updated_at", "INTEGER NOT NULL DEFAULT 0")],
    "runs": [("created_at", "INTEGER NOT NULL DEFAULT 0")],
    "cards": [
        ("published_at", "INTEGER NOT NULL DEFAULT 0"),
        ("run_id", "TEXT NOT NULL DEFAULT ''"),
        ("candidate_id", "TEXT NOT NULL DEFAULT ''"),
    ],
    "visits": [("first_seen", "INTEGER NOT NULL DEFAULT 0"), ("opened_at", "INTEGER")],
}


def migrate(conn):
    """Additive migration from schema v1. Safe to run on every start."""
    for table, columns in EXTRA_COLUMNS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    stamp = now()
    for table, column in (
        ("projects", "created_at"),
        ("projects", "updated_at"),
        ("runs", "created_at"),
        ("cards", "published_at"),
        ("visits", "first_seen"),
    ):
        conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=0", (stamp,))
    conn.execute("INSERT INTO meta(k,v) VALUES('schema_version','2') ON CONFLICT(k) DO UPDATE SET v=excluded.v")
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner, updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_runs_pid ON runs(pid, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_cards_pid ON cards(pid, version DESC)",
        "CREATE INDEX IF NOT EXISTS idx_visits_slug ON visits(slug)",
        "CREATE INDEX IF NOT EXISTS idx_feedback_slug ON feedback(slug, at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_limits_window ON limits(window)",
        "CREATE INDEX IF NOT EXISTS idx_recovery_owner ON recovery(owner)",
    ):
        conn.execute(statement)


def prune(days=None, dry_run=False):
    """Drop match runs that are old and not referenced by any published card.

    `runs` is the only table that grows without a deliberate user action: every
    match request stores a full candidate list plus a project snapshot. Runs that
    a card points at are kept forever, because `cards.run_id` is the only path
    from a reader's feedback back to the candidates they were shown.

    Visits and feedback are never pruned: they are the numbers the results board
    reports, and deleting rows would silently rewrite past totals.
    """
    limit = max(1, int(days if days is not None else env_int("ZHIYU_RUN_RETENTION_DAYS", 90)))
    cutoff = now() - limit * 86400
    stale = "created_at<? AND id NOT IN (SELECT run_id FROM cards WHERE run_id<>'')"
    with closing(sqlite3.connect(DB, timeout=8)) as conn:
        candidates = conn.execute(f"SELECT count(*) FROM runs WHERE {stale}", (cutoff,)).fetchone()[0]
        total = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        if dry_run or not candidates:
            return dict(days=limit, cutoff=cutoff, candidates=candidates, deleted=0, remaining=total, dryRun=dry_run)
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(f"DELETE FROM runs WHERE {stale}", (cutoff,)).rowcount
        conn.commit()
    return dict(days=limit, cutoff=cutoff, candidates=candidates, deleted=deleted, remaining=total - deleted, dryRun=False)


def init():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB)) as conn, conn:
        conn.executescript(SCHEMA)
        migrate(conn)


def connect_live():
    """A missing database must return unavailable instead of creating an empty file."""
    return sqlite3.connect(DB.resolve().as_uri() + "?mode=rw", uri=True, timeout=8)


def seed_demo():
    """Create one demo workspace with a published card and two feedback rows.

    The recovery code is written to var/DEMO_CODE (mode 600) and never returned by an API.
    """
    with closing(connect_live()) as conn, conn:
        if conn.execute("SELECT 1 FROM meta WHERE k='demo_owner'").fetchone():
            return
        owner, pid, slug, run_id = uid(), uid(), uid(), uid()
        stamp = now()
        conn.execute(
            "INSERT INTO projects(id,owner,data,created_at,updated_at) VALUES(?,?,?,?,?)",
            (pid, owner, pack(EXAMPLE), stamp, stamp),
        )
        # A demo card must trace back to a run, otherwise the dashboard shows a card
        # whose origin is empty and the match page has nothing to open.
        outcome = match_run(EXAMPLE, "synthetic")
        conn.execute(
            "INSERT INTO runs(id,pid,rid,data,created_at) VALUES(?,?,?,?,?)",
            (
                run_id,
                pid,
                "demo-1",
                pack(
                    dict(
                        runId=run_id,
                        requestId="demo-1",
                        mode="synthetic",
                        status=outcome["status"],
                        errorCode=outcome["errorCode"],
                        candidates=outcome["candidates"],
                        projectSnapshot=EXAMPLE,
                    )
                ),
                stamp,
            ),
        )
        chosen = next((item for item in outcome["candidates"] if item["judgment"] == "适配"), None)
        card = dict(
            slug=slug,
            version=1,
            title="用拾句核对访谈原话",
            material="一段脱敏的中文访谈文字稿",
            steps="打开示例工具\n找出三句候选原话\n回到逐字稿逐句核对",
            criterion="三句都能在原稿定位，并能说明哪一句不能支持结论",
            question="哪一句原话不能支持你的结论？",
            tool=EXAMPLE["name"],
            url=EXAMPLE["url"],
            scene=chosen["title"] if chosen else "整理访谈时，怎样找到能支撑结论的原话？",
            mode="synthetic",
        )
        signature = hashlib.sha256(pack([pid, "demo"]).encode()).hexdigest()
        conn.execute(
            "INSERT INTO cards(slug,pid,version,signature,data,active,published_at,run_id,candidate_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (slug, pid, 1, signature, pack(card), 1, stamp, run_id, chosen["id"] if chosen else ""),
        )
        rows = [
            ("done", "", "三句都找到了。第二句讲的是时间安排，不能支持结论。"),
            ("blocked", "核对第三句", "候选句来自另一段访谈，找不到上下文。"),
        ]
        for index, (status, step, note) in enumerate(rows):
            reader = uid()
            conn.execute("INSERT INTO visits VALUES(?,?,?,?,?)", (slug, reader, 1, stamp, stamp))
            conn.execute(
                "INSERT INTO feedback VALUES(?,?,?,?,?,?,?)",
                (slug, reader, f"demo-{index}", status, step, note, stamp),
            )
        conn.execute("INSERT INTO meta(k,v) VALUES('demo_owner',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (owner,))
        code = make_code()
        conn.execute("INSERT INTO recovery(code_hash,owner,created_at) VALUES(?,?,?)", (code_hash(code), owner, stamp))
        target = DB.parent / "DEMO_CODE"
        target.write_text(code + "\n", encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass


# --- request validation ----------------------------------------------------

def fields(payload, keys, maxlen=2000, optional=()):
    out = {}
    for key in keys:
        value = payload.get(key, "")
        ok = isinstance(value, str) and len(value.strip()) <= maxlen and (value.strip() or key in optional)
        ck(ok, 400, "请检查字段：" + key)
        out[key] = value.strip()
    return out


def tool_url(value):
    if value == "/sample":
        return
    try:
        parsed = urlsplit(value)
        ok = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not any(ord(ch) < 33 or ch in '<>"\\' for ch in value)
        )
    except ValueError:
        ok = False
    ck(ok, 400, "工具入口须为 http(s) 链接，或保留本机示例 /sample。")


class Handler(BaseHTTPRequestHandler):
    server_version = "Zhiyu/1"

    def version_string(self):
        # Do not advertise the interpreter version in every response.
        return self.server_version

    def log_message(self, *args):
        pass

    # --- responses ---------------------------------------------------------

    def cookie(self, name, value):
        attrs = "Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
        if env_flag("ZHIYU_SECURE_COOKIE"):
            attrs += "; Secure"
        return f"{name}={value}; {attrs}"

    def respond(self, code, data, typ="application/json; charset=utf-8", cache="no-store"):
        raw = pack(data).encode() if typ.startswith("application/json") else data
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
        )
        for value in self.pending:
            self.send_header("Set-Cookie", value)
        self.end_headers()
        self.wfile.write(raw)
        if env_flag("ZHIYU_ACCESS_LOG"):
            elapsed = int((time.time() - getattr(self, "started", time.time())) * 1000)
            print(f"{self.command} {urlsplit(self.path).path} {code} {elapsed}ms {self.client_ip()}", file=sys.stderr, flush=True)

    # --- identity ----------------------------------------------------------

    def ident(self, key, new=False):
        name = "zhiyu_" + key
        jar = SimpleCookie()
        jar.load(self.headers.get("Cookie", ""))
        morsel = jar.get(name)
        value = morsel.value if morsel else ""
        if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", value):
            return value
        ck(new, 403, "会话已失效，请刷新后重试。")
        value = secrets.token_urlsafe(32)
        self.pending.append(self.cookie(name, value))
        return value

    def client_ip(self):
        if env_flag("ZHIYU_TRUST_PROXY"):
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return self.client_address[0]

    # --- authorization -----------------------------------------------------

    def own(self, conn, pid):
        row = conn.execute("SELECT data FROM projects WHERE id=? AND owner=?", (pid, self.ident("dev"))).fetchone()
        ck(row, 404, "项目不存在或不属于当前工作区。")
        return json.loads(row[0])

    def card(self, conn, slug):
        row = conn.execute("SELECT data FROM cards WHERE slug=? AND active=1", (slug,)).fetchone()
        ck(row, 404, "试用卡不存在或已撤回。")
        return json.loads(row[0])

    # --- rate limits -------------------------------------------------------

    def limit(self, bucket):
        """Count one write against this IP, in its own transaction.

        The counter must not share the handler's transaction: a rejected request
        (400/409/429) rolls that transaction back, which would erase the count and
        let an attacker retry forever. Commit first, then decide.
        """
        minute = now() // 60
        key = bucket + ":" + self.client_ip()
        with closing(connect_live()) as conn, conn:
            conn.execute(
                "INSERT INTO limits VALUES(?,?,1) ON CONFLICT(k) DO UPDATE SET "
                "n=CASE WHEN window=excluded.window THEN n+1 ELSE 1 END, window=excluded.window",
                (key, minute),
            )
            count = conn.execute("SELECT n FROM limits WHERE k=?", (key,)).fetchone()[0]
            if count == 1:  # first write of a new window: prune windows older than 10 minutes
                conn.execute("DELETE FROM limits WHERE window<?", (minute - 10,))
        ck(count <= rate_cap(bucket), 429, "提交过于频繁，请稍后重试。")

    def read_limit(self):
        minute = int(time.time()) // 60
        key = (self.client_ip(), minute)
        READ_WINDOWS[key] = READ_WINDOWS.get(key, 0) + 1
        if len(READ_WINDOWS) > 2048:
            for stale in [k for k in READ_WINDOWS if k[1] < minute]:
                READ_WINDOWS.pop(stale, None)
        ck(READ_WINDOWS[key] <= rate_cap("read"), 429, "请求过于频繁，请稍后重试。")

    def drain_body(self):
        """Consume an unread request body before replying with an early 4xx.

        Without this the server answers while the client is still writing a
        large body. On real networks that socket can be reset before the
        response lands, so the client sees IncompleteRead instead of the
        intended 413/415/403. Read at most 1 MiB so a bogus Content-Length
        cannot pin a worker; give up and close the connection past that.
        """
        if getattr(self, "_body_consumed", True):
            return
        self._body_consumed = True
        try:
            remaining = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            return
        if remaining <= 0:
            return
        target = min(remaining, 1024 * 1024)
        previous = self.connection.gettimeout()
        try:
            self.connection.settimeout(2)
            while target > 0:
                block = self.rfile.read(min(64 * 1024, target))
                if not block:
                    break
                target -= len(block)
        except OSError:
            self.close_connection = True
        finally:
            try:
                self.connection.settimeout(previous)
            except OSError:
                pass
        if target > 0:
            self.close_connection = True

    # --- entry points ------------------------------------------------------

    def handle_req(self, post=False):
        self.pending = []
        self.started = time.time()
        self._body_consumed = not post
        try:
            host = self.headers.get("Host", "")
            ck(host.split(":")[0].lower() in allowed_hosts(), 403, "当前访问地址不在允许的域名列表内。")
            path = urlsplit(self.path).path
            payload = {}
            if post:
                origin = self.headers.get("Origin")
                expected = public_origin()
                same_host = origin in ("http://" + host, "https://" + host)
                ck(not origin or origin.rstrip("/") == expected or (not expected and same_host), 403, "来源不匹配。")
                ck(self.headers.get_content_type() == "application/json", 415, "需要 JSON 请求。")
                length = int(self.headers.get("Content-Length", "0"))
                ck(0 < length <= 24000, 413, "请求内容过大或为空。")
                self._body_consumed = True
                payload = json.loads(self.rfile.read(length))
                ck(isinstance(payload, dict), 400, "请求格式不正确。")
            elif not path.startswith("/api/"):
                return self.static(path)
            if post:
                if path == "/api/recovery/claim":
                    bucket = "claim"
                elif path.startswith("/api/cards/"):
                    bucket = "reader"
                else:
                    bucket = "dev"
                with WRITE_LOCK:
                    self.limit(bucket)
                    with closing(connect_live()) as conn, conn:
                        conn.execute("BEGIN IMMEDIATE")
                        data = self.api(conn, path, payload, post)
            else:
                with closing(connect_live()) as conn:
                    if path != "/api/health":
                        self.read_limit()
                    data = self.api(conn, path, payload, post)
            self.respond(200, data)
        except Problem as exc:
            self.drain_body()
            self.respond(exc.code, {"error": exc.msg})
        except (ValueError, UnicodeError):
            self.respond(400, {"error": "请求格式不正确。"})
        except (sqlite3.Error, OSError) as exc:
            print(f"storage error: {exc!r}", file=sys.stderr, flush=True)
            self.respond(503, {"error": "本地存储暂不可用，请稍后重试。"})

    def static(self, path):
        assets = {
            "/styles.css": ("styles.css", "text/css"),
            "/app.js": ("app.js", "text/javascript"),
            "/mark.svg": ("mark.svg", "image/svg+xml"),
            "/recover.css": ("recover.css", "text/css"),
            "/recover.js": ("recover.js", "text/javascript"),
        }
        if path in assets:
            name, typ = assets[path]
        elif path in ("/", "/sample", "/recover"):
            name = "recover.html" if path == "/recover" else "index.html"
            typ = "text/html"
        else:
            ck(bool(re.fullmatch(r"/(?:t|source)/[A-Za-z0-9_-]+", path)), 404, "页面不存在。")
            name, typ = "index.html", "text/html"
        cache = "public, max-age=300" if path.endswith((".css", ".js", ".svg")) else "no-store"
        return self.respond(200, (ROOT / name).read_bytes(), typ + "; charset=utf-8", cache)

    # --- api ---------------------------------------------------------------

    def api(self, conn, path, body, post):
        seg = path.strip("/").split("/")
        if path == "/api/health" and not post:
            ck(conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone() == ("2",),
               503, "数据库结构尚未就绪。")
            conn.execute("SELECT id FROM projects LIMIT 0")
            return dict(ok=True, version=build_id(), db="ok", time=now())

        if path == "/api/workspace" and not post:
            owner = self.ident("dev", True)
            projects = [
                dict(projectId=row[0], **json.loads(row[1]))
                for row in conn.execute("SELECT id,data FROM projects WHERE owner=? ORDER BY rowid DESC", (owner,))
            ]
            demo = conn.execute("SELECT 1 FROM meta WHERE k='demo_owner' AND v=?", (owner,)).fetchone()
            return dict(projects=projects, example=EXAMPLE, mode="synthetic", liveStatus="not_configured", demoWorkspace=bool(demo))

        if path == "/api/recovery" and post:
            owner = self.ident("dev", True)  # first visit to /recover must still work
            code = make_code()
            stamp = now()
            conn.execute("DELETE FROM recovery WHERE owner=?", (owner,))
            conn.execute("INSERT INTO recovery(code_hash,owner,created_at) VALUES(?,?,?)", (code_hash(code), owner, stamp))
            return dict(code=code)

        if path == "/api/recovery/claim" and post:
            normalized = normalize_code(body.get("code", ""))
            ck(bool(re.fullmatch(r"ZY[2-9A-HJ-NP-Z]{8}", normalized)), 400, "恢复码格式不正确。")
            row = conn.execute("SELECT owner FROM recovery WHERE code_hash=?", (code_hash(normalized),)).fetchone()
            ck(row, 400, "恢复码无效。")
            owner = row[0]
            conn.execute("UPDATE recovery SET last_used=? WHERE code_hash=?", (now(), code_hash(normalized)))
            self.pending.append(self.cookie("zhiyu_dev", owner))
            projects = [
                dict(projectId=r[0], **json.loads(r[1]))
                for r in conn.execute("SELECT id,data FROM projects WHERE owner=? ORDER BY rowid DESC", (owner,))
            ]
            return dict(ok=True, projects=projects)

        if path == "/api/projects" and post:
            project = fields(body, EXAMPLE.keys(), optional=("limits",))
            tool_url(project["url"])
            owner = self.ident("dev")
            pid = body.get("projectId")
            stamp = now()
            if pid:
                self.own(conn, pid)
                conn.execute("UPDATE projects SET data=?,updated_at=? WHERE id=?", (pack(project), stamp, pid))
            else:
                pid = uid()
                conn.execute(
                    "INSERT INTO projects(id,owner,data,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (pid, owner, pack(project), stamp, stamp),
                )
            return dict(projectId=pid, **project)

        if len(seg) >= 3 and seg[:2] == ["api", "projects"]:
            pid = seg[2]
            project = self.own(conn, pid)

            if len(seg) == 3 and not post:
                row = conn.execute(
                    "SELECT data FROM runs WHERE pid=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (pid,)
                ).fetchone()
                cards = [
                    dict(
                        **json.loads(data),
                        active=bool(active),
                        publishedAt=published,
                        runId=run_id,
                        candidateId=candidate_id,
                    )
                    for data, active, published, run_id, candidate_id in conn.execute(
                        "SELECT data,active,published_at,run_id,candidate_id FROM cards "
                        "WHERE pid=? ORDER BY version DESC",
                        (pid,),
                    )
                ]
                return dict(project=project, run=json.loads(row[0]) if row else None, cards=cards)

            if seg[3:] == ["runs"] and not post:
                # Version history of the drafts, newest first. Candidate bodies are omitted:
                # a list is for picking a run to look at, not for shipping them all.
                items = []
                for run_id, data, created in conn.execute(
                    "SELECT id,data,created_at FROM runs WHERE pid=? ORDER BY created_at DESC,rowid DESC LIMIT 50",
                    (pid,),
                ):
                    run = json.loads(data)
                    items.append(
                        dict(
                            runId=run_id,
                            requestId=run.get("requestId"),
                            mode=run.get("mode"),
                            status=run.get("status"),
                            errorCode=run.get("errorCode"),
                            candidateCount=len(run.get("candidates") or []),
                            createdAt=created,
                        )
                    )
                return dict(runs=items)

            if seg[3:] == ["runs"] and post:
                rid = fields(body, ["requestId"], 100)["requestId"]
                mode = body.get("mode", "synthetic")
                ck(mode in ("synthetic", "live"), 400, "未知模式。")
                row = conn.execute("SELECT data FROM runs WHERE pid=? AND rid=?", (pid, rid)).fetchone()
                if row:
                    cached = json.loads(row[0])
                    # Replaying a requestId must not change the answer: otherwise switching
                    # to live mode would silently return the older synthetic result.
                    ck(
                        cached.get("mode") == mode,
                        409,
                        "该 requestId 已用于另一种模式，请更换 requestId 后重试。",
                    )
                    return cached
                outcome = match_run(project, mode)
                run = dict(
                    runId=uid(),
                    requestId=rid,
                    mode=mode,
                    status=outcome["status"],
                    errorCode=outcome["errorCode"],
                    candidates=outcome["candidates"],
                    projectSnapshot=project,
                )
                conn.execute(
                    "INSERT INTO runs(id,pid,rid,data,created_at) VALUES(?,?,?,?,?)",
                    (run["runId"], pid, rid, pack(run), now()),
                )
                return run

            if len(seg) == 5 and seg[3] == "runs" and not post:
                row = conn.execute("SELECT data FROM runs WHERE id=? AND pid=?", (seg[4], pid)).fetchone()
                ck(row, 404, "找不到匹配记录。")
                return json.loads(row[0])

            if seg[3:] == ["publish"] and post:
                draft = fields(body, ["runId", "candidateId", "title", "material", "steps", "criterion", "question"])
                row = conn.execute("SELECT data FROM runs WHERE id=? AND pid=?", (draft["runId"], pid)).fetchone()
                ck(row, 400, "无效的匹配记录。")
                run = json.loads(row[0])
                ck(run["projectSnapshot"] == project, 409, "工具登记已变更，请重新匹配后发布。")
                selected = next(
                    (c for c in run["candidates"] if c["id"] == draft["candidateId"] and c["judgment"] == "适配"),
                    None,
                )
                ck(selected, 400, "只能发布有明确依据的适配场景。")
                signature = hashlib.sha256(pack([draft, project]).encode()).hexdigest()
                last = conn.execute(
                    "SELECT version,signature,data,active FROM cards WHERE pid=? ORDER BY version DESC LIMIT 1", (pid,)
                ).fetchone()
                if last and last[1] == signature and last[3]:
                    return json.loads(last[2])
                snapshot = {key: draft[key] for key in ("title", "material", "steps", "criterion", "question")}
                snapshot.update(
                    slug=uid(),
                    version=1 + (last[0] if last else 0),
                    tool=project["name"],
                    url=project["url"],
                    scene=selected["title"],
                    mode=run["mode"],
                )
                conn.execute(
                    "INSERT INTO cards(slug,pid,version,signature,data,active,published_at,run_id,candidate_id) "
                    "VALUES(?,?,?,?,?,1,?,?,?)",
                    (
                        snapshot["slug"],
                        pid,
                        snapshot["version"],
                        signature,
                        pack(snapshot),
                        now(),
                        run["runId"],
                        draft["candidateId"],
                    ),
                )
                return snapshot

            if len(seg) == 5 and seg[3] == "withdraw" and post:
                changed = conn.execute("UPDATE cards SET active=0 WHERE pid=? AND slug=?", (pid, seg[4]))
                ck(changed.rowcount, 404, "卡片不存在。")
                return dict(ok=True)

            if seg[3:] == ["results"] and not post:
                blocks = []
                for slug, version, active, published in conn.execute(
                    "SELECT slug,version,active,published_at FROM cards WHERE pid=? ORDER BY version DESC", (pid,)
                ):
                    visits, opened = conn.execute(
                        "SELECT count(*),coalesce(sum(opened),0) FROM visits WHERE slug=?", (slug,)
                    ).fetchone()
                    rows = [
                        dict(status=status, step=step, note=note, at=at)
                        for status, step, note, at in conn.execute(
                            "SELECT status,step,note,at FROM feedback WHERE slug=? ORDER BY at DESC,rowid DESC",
                            (slug,),
                        )
                    ]
                    blocks.append(
                        dict(
                            slug=slug,
                            version=version,
                            active=bool(active),
                            publishedAt=published,
                            visits=visits,
                            opened=opened,
                            completed=sum(item["status"] == "done" for item in rows),
                            feedback=rows,
                        )
                    )
                return dict(versions=blocks)

        if len(seg) == 3 and seg[:2] == ["api", "sources"] and not post:
            source = next((s for s in SOURCES if s["id"] == seg[2]), None)
            ck(source, 404, "样例不存在。")
            return {key: source[key] for key in ["title", "body", "evidence", "author"]}

        if len(seg) >= 3 and seg[:2] == ["api", "cards"]:
            slug = seg[2]
            card = self.card(conn, slug)
            if len(seg) == 3 and not post:
                self.ident("reader", True)
                return card
            if post and seg[3:] in (["visit"], ["open"], ["feedback"]):
                reader = self.ident("reader")
                ck(type(body.get("version")) is int and body["version"] == card["version"], 409, "卡片版本不匹配。")
                conn.execute("INSERT OR IGNORE INTO visits(slug,reader,opened,first_seen) VALUES(?,?,0,?)", (slug, reader, now()))
                if seg[3] == "open":
                    conn.execute(
                        "UPDATE visits SET opened=1,opened_at=? WHERE slug=? AND reader=?", (now(), slug, reader)
                    )
                if seg[3] == "feedback":
                    item = fields(body, ["submissionId", "status", "step", "note"], 1000, optional=("step",))
                    ck(
                        item["status"] in ("done", "blocked") and (item["status"] == "done" or item["step"]),
                        400,
                        "请注明卡住的步骤。",
                    )
                    previous = conn.execute(
                        "SELECT sid,status FROM feedback WHERE slug=? AND reader=?", (slug, reader)
                    ).fetchone()
                    # A second answer under a new submissionId must not be dropped
                    # silently: inserting would be ignored by the primary key while
                    # the caller still receives 200, so the reader believes the new
                    # answer was recorded and the board keeps showing the old one.
                    ck(
                        previous is None or previous[0] == item["submissionId"],
                        409,
                        "这张卡片已提交过反馈，不能再次提交。",
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?,?,?)",
                        (slug, reader, item["submissionId"], item["status"], item["step"], item["note"], now()),
                    )
                receipt = conn.execute("SELECT sid,status FROM feedback WHERE slug=? AND reader=?", (slug, reader)).fetchone()
                if seg[3] == "feedback":
                    # Reaching here without a row means the id collided with another
                    # reader's submission; the insert was ignored, so say so instead
                    # of returning an empty receipt as success.
                    ck(receipt, 409, "提交 ID 已被其他会话使用，请刷新后重试。")
                return dict(receipt=dict(submissionId=receipt[0], status=receipt[1]) if receipt else None)

        raise Problem(404, "接口不存在。")

    def do_GET(self):
        self.handle_req()

    def do_POST(self):
        self.handle_req(True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=env_int("ZHIYU_PORT", 4173))
    parser.add_argument("--host", default=env("ZHIYU_HOST", "127.0.0.1"),
                        help="监听地址。默认只听回环，前面由 Caddy/nginx 反代；"
                             "需要同网段直连时改成 0.0.0.0，并把该网段写进 ZHIYU_HOSTS。")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    init()
    if env_flag("ZHIYU_DEMO"):
        seed_demo()
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        if args.open:
            import urllib.request
            import webbrowser

            try:
                base = f"http://{shown}:{args.port}"
                with urllib.request.urlopen(base + "/api/workspace", timeout=2) as resp:
                    same = json.load(resp).get("example") == EXAMPLE
                if same:
                    webbrowser.open(base)
                    raise SystemExit(0)
            except (OSError, ValueError):
                pass
        raise SystemExit(f"Port unavailable: {exc}. Choose --port 4180.")
    if args.open:
        import webbrowser

        webbrowser.open(f"http://{shown}:{server.server_port}/")
    print(f"Zhiyu: http://{shown}:{server.server_port}/ build={build_id()}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
