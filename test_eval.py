"""Tests for scripts/eval.py.

The cross-check block uses the published example from ranx's README:
  qrels = {"q_1":{"d_12":5,"d_25":3}, "q_2":{"d_11":6,"d_22":1}}
  run   = ranked by score (d_12,d_23,d_25,d_36,d_32 / d_12,d_11,d_25,d_36,d_22)
  ranx reports: ndcg@5=0.7861, map@5=0.6416, mrr=0.75
If our metric code drifts, these numbers fail -- that is the point of the
cross-check: a verified reimplementation, not self-confirming assertions.
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
import eval as ev

RANX_QRELS = {"q_1": {"d_12": 5, "d_25": 3}, "q_2": {"d_11": 6, "d_22": 1}}
RANX_RUN = {
    "q_1": ["d_12", "d_23", "d_25", "d_36", "d_32", "d_35"],
    "q_2": ["d_12", "d_11", "d_25", "d_36", "d_22", "d_35"],
}
ZHIYU_QRELS = {"p1": {"a": 2, "b": 0, "c": 1}}
ZHIYU_RUN_SEARCH = {"p1": ["b", "c", "a"]}
ZHIYU_RUN_TASK = {"p1": ["a", "c", "b"]}


def as_qrels(projects):
    return {"schema": ev.SCHEMA, "kind": "qrels", "projects": projects}


def as_run(label, projects):
    return {"schema": ev.SCHEMA, "kind": "run", "label": label, "projects": projects}


class RanxCrossCheck(unittest.TestCase):
    """Verify our metrics reproduce ranx's published numbers (round to 4dp)."""

    def test_ndcg_at_5_matches_ranx(self):
        total = 0.0
        for qid, ranked in RANX_RUN.items():
            total += ev.ndcg_at_k(RANX_QRELS[qid], ranked, 5)
        self.assertAlmostEqual(total / 2, 0.7861, places=4)

    def test_map_at_5_matches_ranx(self):
        total = 0.0
        for qid, ranked in RANX_RUN.items():
            total += ev.average_precision_at_k(RANX_QRELS[qid], ranked, 5, 1)
        # ranx README prints 0.6416 (4dp display); exact hand calc is
        # (0.83333 + 0.45)/2 = 0.6416667. Compare to 3dp to absorb the
        # rounding difference while still catching real metric drift.
        self.assertAlmostEqual(total / 2, 0.6416, places=3)

    def test_mrr_matches_ranx(self):
        total = 0.0
        for qid, ranked in RANX_RUN.items():
            total += ev.reciprocal_rank(RANX_QRELS[qid], ranked, 1)
        self.assertAlmostEqual(total / 2, 0.75, places=4)


class Metrics(unittest.TestCase):
    def test_precision_and_hit_count_only_fit_at_k(self):
        q = ZHIYU_QRELS["p1"]
        # 适配阈值 2: only candidate 'a' qualifies and it is rank 3.
        self.assertAlmostEqual(ev.precision_at_k(q, ZHIYU_RUN_SEARCH["p1"], 3, 2), 1 / 3)
        self.assertAlmostEqual(ev.hit_at_k(q, ZHIYU_RUN_SEARCH["p1"], 3, 2), 1.0)
        self.assertAlmostEqual(ev.hit_at_k(q, ZHIYU_RUN_SEARCH["p1"], 2, 2), 0.0)

    def test_task_order_beats_search_order(self):
        qrels = as_qrels(ZHIYU_QRELS)
        search = ev.evaluate(qrels, as_run("search-order", ZHIYU_RUN_SEARCH))
        task = ev.evaluate(qrels, as_run("task-order", ZHIYU_RUN_TASK))
        for metric in ("precision", "mrr", "map", "ndcg", "hit"):
            self.assertGreaterEqual(task["aggregate"][metric], search["aggregate"][metric])
        # Only candidate 'a' is 适配, so precision@3 is 1/3 either way; the
        # win shows up in rank-sensitive metrics (a moves from rank 3 to 1).
        self.assertAlmostEqual(task["aggregate"]["precision"], 1 / 3)
        self.assertEqual(task["aggregate"]["mrr"], 1.0)
        self.assertEqual(search["aggregate"]["mrr"], 1 / 3)
        self.assertEqual(task["aggregate"]["ndcg"], 1.0)

    def test_unlabeled_candidates_are_reported(self):
        qrels = as_qrels({"p1": {"a": 2}})
        run = as_run("partial", {"p1": ["a", "ghost", "phantom"]})
        result = ev.evaluate(qrels, run)["projects"][0]
        self.assertEqual(result["unlabeled"], 2)

    def test_empty_qrels_and_empty_projects_do_not_crash(self):
        result = ev.evaluate(as_qrels({}), as_run("none", {"p1": []}))
        self.assertEqual(result["aggregate"]["precision"], 0.0)
        self.assertEqual(ev.evaluate(as_qrels({}), as_run("none", {}))["aggregate"]["mrr"], 0.0)


class Contract(unittest.TestCase):
    def test_check_schema_accepts_valid_docs(self):
        ev.check_schema(as_qrels(ZHIYU_QRELS), "qrels", "q.json")
        ev.check_schema(as_run("task-order", ZHIYU_RUN_TASK), "run", "r.json")

    def test_check_schema_rejects_wrong_shape(self):
        bad = as_run("x", {"p1": {"not": "an array"}})
        with self.assertRaises(ValueError):
            ev.check_schema(bad, "run", "r.json")
        with self.assertRaises(ValueError):
            ev.check_schema({"schema": "wrong", "kind": "run", "projects": {}}, "run", "r.json")


class Cli(unittest.TestCase):
    def test_cli_runs_two_runs_and_prints_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            q = root / "qrels.json"
            q.write_text(json.dumps(as_qrels(ZHIYU_QRELS)), encoding="utf-8")
            runs = []
            for label, payload in (("search-order", ZHIYU_RUN_SEARCH),
                                   ("task-order", ZHIYU_RUN_TASK)):
                path = root / f"run-{label}.json"
                path.write_text(json.dumps(as_run(label, payload)), encoding="utf-8")
                runs.append(str(path))
            out = io.StringIO()
            with redirect_stdout(out):
                code = ev.main(["--qrels", str(q), "--runs"] + runs)
            text = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("[search-order]", text)
            self.assertIn("[task-order]", text)
            self.assertIn("对比", text)

    def test_cli_emits_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            q = root / "q.json"
            r = root / "r.json"
            q.write_text(json.dumps(as_qrels(ZHIYU_QRELS)), encoding="utf-8")
            r.write_text(json.dumps(as_run("task-order", ZHIYU_RUN_TASK)), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = ev.main(["--qrels", str(q), "--runs", str(r), "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload[0]["label"], "task-order")


if __name__ == "__main__":
    unittest.main()
