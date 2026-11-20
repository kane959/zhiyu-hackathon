"""B2-3 contract tests: reader sessions, visit/open/feedback events, and counting.

The planning doc states three acceptance criteria for this task:

1. A double submission stores one row.
2. A forged version is rejected with 409.
3. The visit, open and self-reported-complete columns are each correct.

`test_flow` already walks the happy path once. This file applies pressure where
that walk cannot: concurrent double clicks, rejected events that must leave no
trace, per-version isolation, and a withdrawn card that must stop accepting
events without losing the history already collected.
"""
import http.cookiejar
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import server
from fixtures import EXAMPLE

ENV_KEYS = ("ZHIYU_RATE_READER", "ZHIYU_RATE_DEV", "ZHIYU_RATE_READ", "ZHIYU_RATE_CLAIM")


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.agent = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, path, data=None):
        request = urllib.request.Request(
            self.base + "/api/" + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with self.agent.open(request, timeout=10) as response:
            return json.load(response)

    def status(self, path, data=None):
        try:
            self.call(path, data)
        except urllib.error.HTTPError as error:
            error.close()
            return error.code
        return 200


def parallel(calls):
    """Run callables in parallel and report ("ok", value) or ("err", status)."""
    results = [None] * len(calls)

    def run(index, fn):
        try:
            results[index] = ("ok", fn())
        except urllib.error.HTTPError as error:
            error.close()
            results[index] = ("err", error.code)
        except Exception as error:  # surfaced by the assertion in the caller
            results[index] = ("err", repr(error))

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


class Events(unittest.TestCase):
    def setUp(self):
        self.saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        # Concurrency cases fire more events per minute than the production reader
        # cap allows from a single address; the cap itself is proven in test_deploy.
        os.environ["ZHIYU_RATE_DEV"] = "100000"
        os.environ["ZHIYU_RATE_READER"] = "100000"
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = Path(self.tmp.name) / "events.sqlite3"
        server.init()
        self.host = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.host.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.host.server_port}"
        self.owner = Client(self.base)
        self.owner.call("workspace")
        self.pid = self.owner.call("projects", EXAMPLE)["projectId"]
        self.path = "projects/" + self.pid

    def tearDown(self):
        self.host.shutdown()
        self.host.server_close()
        self.thread.join()
        server.DB = self.old_db
        self.tmp.cleanup()
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # --- helpers -----------------------------------------------------------

    def publish(self, title="第一版", request_id="r1"):
        run = self.owner.call(self.path + "/runs", {"requestId": request_id})
        return self.owner.call(
            self.path + "/publish",
            dict(
                runId=run["runId"],
                candidateId="interview",
                title=title,
                material="脱敏逐字稿",
                steps="准备材料\n查找引文",
                criterion="定位到原文",
                question="哪一句有效？",
            ),
        )

    def reader(self, slug):
        """A reader must hold a session cookie before it can post an event."""
        client = Client(self.base)
        client.call("cards/" + slug)
        return client

    def stats(self, slug):
        block = next(item for item in self.owner.call(self.path + "/results")["versions"] if item["slug"] == slug)
        return block["visits"], block["opened"], block["completed"]

    def rows(self, sql, args=()):
        # Windows keeps the file locked while a connection is open, which then
        # breaks TemporaryDirectory cleanup. Close explicitly, do not rely on GC.
        with closing(sqlite3.connect(server.DB)) as conn:
            return conn.execute(sql, args).fetchone()[0]

    def feedback(self, version=1, sid="try-1", status="done", step="", note="跑通了"):
        return {"version": version, "submissionId": sid, "status": status, "step": step, "note": note}

    def assertStatus(self, expected, client, path, data=None):
        self.assertEqual(client.status(path, data), expected, f"{path} 应为 {expected}")

    # --- criterion 3: three columns stay independent ------------------------

    def test_three_columns_move_independently(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug
        self.assertEqual(self.stats(slug), (0, 0, 0))
        reader.call(path + "/visit", {"version": 1})
        self.assertEqual(self.stats(slug), (1, 0, 0), "访问不应顺带记成进入工具")
        reader.call(path + "/open", {"version": 1})
        self.assertEqual(self.stats(slug), (1, 1, 0), "进入工具不应顺带记成完成")
        reader.call(path + "/feedback", self.feedback())
        self.assertEqual(self.stats(slug), (1, 1, 1))

    def test_repeated_visit_and_open_do_not_inflate_counts(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug
        for _ in range(3):
            reader.call(path + "/visit", {"version": 1})
            reader.call(path + "/open", {"version": 1})
        self.assertEqual(self.stats(slug), (1, 1, 0))
        self.assertEqual(self.rows("SELECT count(*) FROM visits WHERE slug=?", (slug,)), 1)

    def test_blocked_feedback_is_not_counted_as_completed(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        reader.call("cards/" + slug + "/feedback", self.feedback(status="blocked", step="核对第三句"))
        block = self.owner.call(self.path + "/results")["versions"][0]
        self.assertEqual((block["visits"], block["opened"], block["completed"]), (1, 0, 0))
        self.assertEqual([item["status"] for item in block["feedback"]], ["blocked"])
        self.assertEqual(block["feedback"][0]["step"], "核对第三句")

    def test_reader_who_only_opens_still_counts_as_a_visit(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        reader.call("cards/" + slug + "/open", {"version": 1})
        self.assertEqual(self.stats(slug), (1, 1, 0))

    # --- criterion 1: a double submission stores one row -------------------

    def test_same_submission_id_twice_returns_the_same_receipt(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug + "/feedback"
        first = reader.call(path, self.feedback())
        second = reader.call(path, self.feedback())
        self.assertEqual(first, second)
        self.assertEqual(self.stats(slug), (1, 0, 1))
        self.assertEqual(self.rows("SELECT count(*) FROM feedback WHERE slug=?", (slug,)), 1)

    def test_concurrent_double_submit_stores_one_row(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug + "/feedback"
        results = parallel([lambda: reader.call(path, self.feedback())] * 2)
        self.assertEqual([state for state, _ in results], ["ok", "ok"], "双击应双双成功并返回同一回执")
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.rows("SELECT count(*) FROM feedback WHERE slug=?", (slug,)), 1)

    def test_different_submission_id_from_same_reader_is_rejected(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug + "/feedback"
        reader.call(path, self.feedback(status="blocked", step="核对第三句"))
        self.assertStatus(409, reader, path, self.feedback(sid="try-2"))
        self.assertEqual(self.stats(slug), (1, 0, 0), "被拒的第二份不应改写状态")
        self.assertEqual(self.rows("SELECT count(*) FROM feedback WHERE slug=?", (slug,)), 1)

    def test_concurrent_distinct_ids_keep_exactly_one(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug + "/feedback"
        results = parallel([lambda: reader.call(path, self.feedback(sid="a")), lambda: reader.call(path, self.feedback(sid="b"))])
        codes = sorted("200" if state == "ok" else str(value) for state, value in results)
        self.assertEqual(codes, ["200", "409"])
        self.assertEqual(self.rows("SELECT count(*) FROM feedback WHERE slug=?", (slug,)), 1)

    def test_two_readers_each_keep_their_own_row(self):
        slug = self.publish()["slug"]
        path = "cards/" + slug + "/feedback"
        self.reader(slug).call(path, self.feedback(sid="a", status="blocked", step="第三步"))
        self.reader(slug).call(path, self.feedback(sid="b"))
        self.assertEqual(self.stats(slug), (2, 0, 1))

    # --- criterion 2: a forged version is rejected and writes nothing ------

    def test_forged_version_is_rejected_without_side_effects(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug
        self.assertStatus(409, reader, path + "/visit", {"version": 2})
        self.assertStatus(409, reader, path + "/open", {"version": 2})
        self.assertStatus(409, reader, path + "/feedback", self.feedback(version=2))
        self.assertEqual(self.rows("SELECT count(*) FROM visits WHERE slug=?", (slug,)), 0, "被拒的事件不应留下访问行")
        self.assertEqual(self.rows("SELECT count(*) FROM feedback WHERE slug=?", (slug,)), 0)

    def test_missing_or_non_integer_version_is_rejected(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug + "/visit"
        self.assertStatus(409, reader, path, {})
        self.assertStatus(409, reader, path, {"version": "1"})
        self.assertStatus(409, reader, path, {"version": True})

    # --- scope: versions, withdrawal, unknown slugs ------------------------

    def test_versions_count_independently(self):
        first = self.publish(title="第一版")
        self.reader(first["slug"]).call("cards/" + first["slug"] + "/visit", {"version": 1})
        second = self.publish(title="第二版", request_id="r2")
        self.reader(second["slug"]).call("cards/" + second["slug"] + "/visit", {"version": 2})
        self.assertEqual(self.stats(first["slug"]), (1, 0, 0))
        self.assertEqual(self.stats(second["slug"]), (1, 0, 0))

    def test_withdrawn_card_takes_no_new_events_but_keeps_history(self):
        card = self.publish()
        slug = card["slug"]
        reader = self.reader(slug)
        reader.call("cards/" + slug + "/feedback", self.feedback())
        self.owner.call(self.path + "/withdraw/" + slug, {})
        self.assertStatus(404, reader, "cards/" + slug)
        self.assertStatus(404, reader, "cards/" + slug + "/visit", {"version": 1})
        self.assertStatus(404, reader, "cards/" + slug + "/feedback", self.feedback(sid="try-2"))
        self.assertEqual(self.stats(slug), (1, 0, 1), "撤回只关入口，不清已收到的数据")

    def test_unknown_card_is_404_not_400(self):
        reader = self.reader(self.publish()["slug"])
        self.assertStatus(404, reader, "cards/nope/visit", {"version": 1})
        self.assertStatus(404, reader, "cards/nope/feedback", self.feedback())

    def test_feedback_input_is_validated(self):
        slug = self.publish()["slug"]
        reader = self.reader(slug)
        path = "cards/" + slug + "/feedback"
        self.assertStatus(400, reader, path, self.feedback(status="blocked", step=""))
        self.assertStatus(400, reader, path, self.feedback(status="unknown"))
        self.assertStatus(400, reader, path, self.feedback(note="字" * 1001))

    # --- load: many readers, exact counts ----------------------------------

    def test_twelve_concurrent_readers_are_counted_exactly(self):
        slug = self.publish()["slug"]
        readers = [self.reader(slug) for _ in range(12)]
        path = "cards/" + slug
        calls = []
        for reader in readers:
            calls.append(lambda r=reader: r.call(path + "/visit", {"version": 1}))
            calls.append(lambda r=reader: r.call(path + "/open", {"version": 1}))
        results = parallel(calls)
        self.assertEqual({state for state, _ in results}, {"ok"})
        self.assertEqual(self.stats(slug), (12, 12, 0))

    def test_feedback_count_never_exceeds_visit_count(self):
        slug = self.publish()["slug"]
        readers = [self.reader(slug) for _ in range(6)]
        # Distinct submission ids: the card's unique index is (slug, sid), so one
        # id shared by six readers would be a collision test, not a counting test.
        parallel(
            [
                lambda r=reader, i=index: r.call("cards/" + slug + "/feedback", self.feedback(sid=f"s{i}"))
                for index, reader in enumerate(readers)
            ]
        )
        visits, opened, completed = self.stats(slug)
        self.assertEqual(visits, 6, "提交反馈前必须先落访问行，否则访问数会少于反馈数")
        self.assertEqual(completed, 6)
        self.assertGreaterEqual(visits, completed)
        self.assertGreaterEqual(opened, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
