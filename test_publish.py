"""B2-2 contract tests: ownership, version history, provenance and run replay.

These cover the parts of the publish flow that the older tests only touched in
passing: who may read a draft, what a card records about where it came from,
and what happens to the previous version when a new one is published.
"""
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
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
        with self.agent.open(request, timeout=5) as response:
            return json.load(response)

    def status(self, path, data=None):
        try:
            self.call(path, data)
        except urllib.error.HTTPError as error:
            error.close()
            return error.code
        return 200


class Publish(unittest.TestCase):
    def setUp(self):
        self.saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["ZHIYU_RATE_DEV"] = "100000"
        os.environ["ZHIYU_RATE_READER"] = "100000"
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = server.DB
        server.DB = Path(self.tmp.name) / "publish.sqlite3"
        server.init()
        self.host = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.host.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.host.server_port}"
        self.owner = Client(self.base)
        self.other = Client(self.base)
        self.reader = Client(self.base)
        self.owner.call("workspace")
        self.other.call("workspace")
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

    def match(self, rid="r1", mode=None):
        body = {"requestId": rid}
        if mode:
            body["mode"] = mode
        return self.owner.call(self.path + "/runs", body)

    def publish(self, run_id, title="第一版", candidate="interview"):
        return self.owner.call(
            self.path + "/publish",
            dict(
                runId=run_id,
                candidateId=candidate,
                title=title,
                material="脱敏逐字稿",
                steps="准备材料\n查找引文",
                criterion="定位到原文",
                question="哪一句有效？",
            ),
        )

    def assertStatus(self, expected, client, path, data=None):
        self.assertEqual(client.status(path, data), expected, f"{path} 应为 {expected}")

    # --- drafts stay private ------------------------------------------------

    def test_other_workspace_cannot_reach_drafts_or_cards(self):
        first = self.match("draft-1")
        card = self.publish(first["runId"])
        for path in (
            self.path,
            self.path + "/runs",
            self.path + "/runs/" + first["runId"],
            self.path + "/results",
        ):
            self.assertStatus(404, self.other, path)
        self.assertStatus(404, self.other, self.path + "/publish", {"runId": first["runId"]})
        self.assertStatus(404, self.other, self.path + "/withdraw/" + card["slug"], {})
        # The card itself is public by design, and must stay readable by anyone.
        self.assertEqual(self.reader.call("cards/" + card["slug"])["title"], "第一版")

    def test_draft_is_not_public_before_publish(self):
        self.match("no-card-yet")
        self.assertEqual(self.owner.call(self.path)["cards"], [])
        self.assertStatus(404, self.reader, "cards/never-published")

    def test_runs_of_another_project_are_rejected(self):
        mine = self.match("mine")
        second = self.owner.call("projects", EXAMPLE)["projectId"]
        self.assertStatus(400, self.owner, "projects/" + second + "/publish", {"runId": mine["runId"]})
        # A run id that does not exist is rejected the same way, not by 404.
        self.assertStatus(400, self.owner, self.path + "/publish", {"runId": "made-up"})

    # --- version history ----------------------------------------------------

    def test_new_version_keeps_the_old_link_working(self):
        first = self.match("v1")
        one = self.publish(first["runId"], title="第一版")
        two = self.publish(first["runId"], title="第二版")
        self.assertEqual((one["version"], two["version"]), (1, 2))

        cards = self.owner.call(self.path)["cards"]
        self.assertEqual([card["version"] for card in cards], [2, 1])
        self.assertTrue(all(card["active"] for card in cards))

        # Links already shared must keep returning what the reader saw.
        self.assertEqual(self.reader.call("cards/" + one["slug"])["title"], "第一版")
        self.assertEqual(self.reader.call("cards/" + two["slug"])["title"], "第二版")

        versions = self.owner.call(self.path + "/results")["versions"]
        self.assertEqual([block["version"] for block in versions], [2, 1])
        self.assertTrue(all(block["publishedAt"] > 0 for block in versions))

    def test_withdraw_affects_only_that_version(self):
        first = self.match("w")
        one = self.publish(first["runId"], title="第一版")
        two = self.publish(first["runId"], title="第二版")

        self.assertEqual(self.owner.call(self.path + "/withdraw/" + two["slug"], {}), {"ok": True})
        self.assertStatus(404, self.reader, "cards/" + two["slug"])
        self.assertEqual(self.reader.call("cards/" + one["slug"])["title"], "第一版")

        versions = {block["version"]: block for block in self.owner.call(self.path + "/results")["versions"]}
        self.assertFalse(versions[2]["active"])
        self.assertTrue(versions[1]["active"])

        self.owner.call(self.path + "/withdraw/" + one["slug"], {})
        self.assertStatus(404, self.reader, "cards/" + one["slug"])
        self.assertStatus(404, self.owner, self.path + "/withdraw/unknown-slug", {})

    # --- provenance ---------------------------------------------------------

    def test_card_records_which_run_and_candidate_it_came_from(self):
        run = self.match("trace")
        card = self.publish(run["runId"])
        stored = self.owner.call(self.path)["cards"][0]
        self.assertEqual(stored["runId"], run["runId"])
        self.assertEqual(stored["candidateId"], "interview")
        self.assertGreater(stored["publishedAt"], 0)

        public = self.reader.call("cards/" + card["slug"])
        self.assertNotIn("runId", public)
        self.assertNotIn("candidateId", public)
        self.assertNotIn("projectSnapshot", public)

    # --- run replay ---------------------------------------------------------

    def test_request_id_cannot_switch_mode(self):
        synthetic = self.match("same-id")
        self.assertEqual(synthetic["mode"], "synthetic")
        self.assertStatus(409, self.owner, self.path + "/runs", {"requestId": "same-id", "mode": "live"})

        live = self.match("live-id", "live")
        self.assertEqual((live["status"], live["errorCode"]), ("failed", "not_configured"))
        self.assertEqual(self.match("live-id", "live")["runId"], live["runId"])

    def test_run_list_is_compact_and_newest_first(self):
        first = self.match("older")
        second = self.match("newer", "live")
        runs = self.owner.call(self.path + "/runs")["runs"]
        self.assertEqual([item["requestId"] for item in runs], ["newer", "older"])
        self.assertEqual(runs[0]["runId"], second["runId"])
        self.assertEqual(runs[0]["candidateCount"], 0)
        self.assertEqual(runs[1]["candidateCount"], len(first["candidates"]))
        for item in runs:
            self.assertNotIn("candidates", item)
            self.assertNotIn("projectSnapshot", item)
        # The full run is still available one level down.
        self.assertEqual(
            self.owner.call(self.path + "/runs/" + first["runId"])["runId"], first["runId"]
        )

    def test_publish_rejects_candidate_without_evidence(self):
        run = self.match("cand")
        self.assertStatus(400, self.owner, self.path + "/publish", {"runId": run["runId"], "candidateId": "transcribe"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
