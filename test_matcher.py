"""匹配接缝的契约测试。

这一组不测业务逻辑，只钉住 matcher.run() 的输入输出形状。
B1 把本地样例换成真实检索后，本文件必须仍然全绿；红了说明契约被改了，
前端与 server.py 需要同步，不能靠改测试绕过。
"""
import unittest

import matcher
import server
from fixtures import EXAMPLE

CANDIDATE_KEYS = {
    "id", "title", "author", "url", "sourceType",
    "evidence", "judgment", "reason", "unknown",
}


class MatcherContract(unittest.TestCase):
    def setUp(self):
        self.project = dict(EXAMPLE)

    def test_synthetic_returns_ranked_candidates(self):
        out = matcher.run(self.project)
        self.assertEqual(set(out), {"status", "errorCode", "candidates"})
        self.assertEqual(out["status"], "success")
        self.assertIsNone(out["errorCode"])
        self.assertTrue(1 <= len(out["candidates"]) <= 3, out["candidates"])
        order = [matcher.JUDGMENTS.index(c["judgment"]) for c in out["candidates"]]
        self.assertEqual(order, sorted(order), "候选必须按 适配 → 待核实 → 排除 排序")

    def test_candidate_shape_is_fixed(self):
        candidates = matcher.run(self.project)["candidates"]
        self.assertTrue(candidates)
        for item in candidates:
            self.assertEqual(set(item), CANDIDATE_KEYS, item)
            self.assertIn(item["judgment"], matcher.JUDGMENTS)
            self.assertTrue(
                item["url"].startswith("/") or item["url"].startswith("http"),
                "url 必须是站内路径或完整外链，前端不再拼接",
            )
            self.assertIsInstance(item["evidence"], str)
            self.assertTrue(item["evidence"], "候选必须带原文证据")

    def test_empty_result_is_not_an_error(self):
        project = dict(self.project, material="盆栽照片", task="辨认花卉", output="花名", limits="")
        out = matcher.run(project)
        self.assertEqual(out["status"], "empty")
        self.assertIsNone(out["errorCode"])
        self.assertEqual(out["candidates"], [])

    def test_live_mode_reports_not_configured(self):
        out = matcher.run(self.project, "live")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["errorCode"], "not_configured")
        self.assertEqual(out["candidates"], [])

    def test_project_is_not_mutated(self):
        snapshot = dict(self.project)
        matcher.run(self.project)
        self.assertEqual(self.project, snapshot, "matcher 不得改写入参")

    def test_declared_enums_are_the_documented_ones(self):
        self.assertEqual(set(matcher.STATUSES), {"success", "empty", "failed"})
        self.assertEqual(
            set(matcher.ERROR_CODES),
            {"not_configured", "upstream_error", "rate_limited", "timeout"},
        )
        self.assertEqual(set(matcher.JUDGMENTS), {"适配", "待核实", "排除"})

    def test_server_goes_through_this_seam(self):
        self.assertIs(server.match_run, matcher.run, "server.py 不得绕过 matcher 直接调用 fixtures")


if __name__ == "__main__":
    unittest.main(verbosity=2)
