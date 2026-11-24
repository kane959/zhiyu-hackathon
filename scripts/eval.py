#!/usr/bin/env python3
"""Match-quality evaluation against human labels (zhiyu-eval/v1).

B1-4 asks: compare search order vs task order, and count how many of the top
three candidates are relevant. P supplies human labels as qrels.json; B1
supplies each ordering as a run JSON. This tool scores them and prints a
side-by-side comparison.

Metric definitions follow ranx (Precision / MRR / MAP / NDCG with linear
gain and a 1/log2(rank+1) discount). test_eval.py cross-checks the numbers
against the published example in ranx's README, so this is a verified
reimplementation -- not a homebrew metric. Pure standard library: no numpy,
numba or pandas enter the deployment stack.

Input contract (see docs/DATA.md):

  qrels.json   {"schema":"zhiyu-eval/v1","kind":"qrels","projects":{
                 "<pid>":{"<candidateId>":2}}}
  run-*.json   {"schema":"zhiyu-eval/v1","kind":"run","label":"task-order",
                "projects":{"<pid>":["<candidateId>", ...]}}

Relevance levels: 2=适配, 1=待核实, 0=排除 (matching candidate.judgment).
Candidates not present in qrels are treated as relevance 0 and reported as
unlabeled so coverage gaps are visible.
"""
import argparse
import json
import math
import sys

SCHEMA = "zhiyu-eval/v1"
METRICS = ("precision", "mrr", "map", "ndcg", "hit")


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def check_schema(doc, kind, path):
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level must be an object")
    if doc.get("schema") != SCHEMA:
        raise ValueError(f"{path}: schema must be {SCHEMA}, got {doc.get('schema')!r}")
    if doc.get("kind") != kind:
        raise ValueError(f"{path}: kind must be {kind}, got {doc.get('kind')!r}")
    projects = doc.get("projects")
    if not isinstance(projects, dict):
        raise ValueError(f"{path}: projects must be an object")
    for pid, value in projects.items():
        if kind == "qrels" and not isinstance(value, dict):
            raise ValueError(f"{path}: projects[{pid}] must be an object of candidate:level")
        if kind == "run" and not isinstance(value, list):
            raise ValueError(f"{path}: projects[{pid}] must be an array of candidate ids")
    return projects


def _dcg(gains):
    return sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))


def precision_at_k(qrel, ranked, k, min_rel):
    if k <= 0:
        return 0.0
    top = ranked[:k]
    return sum(1 for cid in top if qrel.get(cid, 0) >= min_rel) / k


def reciprocal_rank(qrel, ranked, min_rel):
    for rank, cid in enumerate(ranked, 1):
        if qrel.get(cid, 0) >= min_rel:
            return 1.0 / rank
    return 0.0


def average_precision_at_k(qrel, ranked, k, min_rel):
    hits = [rank for rank, cid in enumerate(ranked[:k], 1)
            if qrel.get(cid, 0) >= min_rel]
    total = sum(1 for level in qrel.values() if level >= min_rel)
    total = min(total, k)
    if not hits or total == 0:
        return 0.0
    return sum(hit / rank for hit, rank in enumerate(hits, 1)) / total


def ndcg_at_k(qrel, ranked, k):
    gains = [qrel.get(cid, 0) for cid in ranked[:k]]
    ideal = sorted(qrel.values(), reverse=True)[:k]
    idcg = _dcg(ideal)
    return _dcg(gains) / idcg if idcg > 0 else 0.0


def hit_at_k(qrel, ranked, k, min_rel):
    return 1.0 if any(qrel.get(cid, 0) >= min_rel for cid in ranked[:k]) else 0.0


def evaluate(qrels_doc, run_doc, k=3, min_rel=2):
    qrels = qrels_doc["projects"]
    rows = []
    for pid, ranked in run_doc["projects"].items():
        qrel = qrels.get(pid, {})
        rows.append({
            "project": pid,
            "precision": precision_at_k(qrel, ranked, k, min_rel),
            "mrr": reciprocal_rank(qrel, ranked, min_rel),
            "map": average_precision_at_k(qrel, ranked, k, min_rel),
            "ndcg": ndcg_at_k(qrel, ranked, k),
            "hit": hit_at_k(qrel, ranked, k, min_rel),
            "unlabeled": sum(1 for cid in ranked if cid not in qrel),
            "top": ranked[:k],
        })
    count = len(rows)
    aggregate = {m: (sum(row[m] for row in rows) / count if count else 0.0)
                 for m in METRICS}
    return {"label": run_doc.get("label", "(unlabeled)"), "k": k,
            "min_rel": min_rel, "projects": rows, "aggregate": aggregate}


def _print_report(results):
    for result in results:
        agg = result["aggregate"]
        print(f"[{result['label']}]  k={result['k']}  适配阈值≥{result['min_rel']}")
        print(f"  Precision@{result['k']} {agg['precision']:.4f}   "
              f"MRR {agg['mrr']:.4f}   MAP {agg['map']:.4f}   "
              f"NDCG@{result['k']} {agg['ndcg']:.4f}   Hit {agg['hit']:.4f}")
        for row in result["projects"]:
            note = f"  unlabeled={row['unlabeled']}" if row["unlabeled"] else ""
            print(f"    {row['project']}: P={row['precision']:.2f} "
                  f"hit={row['hit']:.0f} top={','.join(row['top'])}{note}")
    if len(results) == 2:
        a, b = results[0]["aggregate"], results[1]["aggregate"]
        print(f"\n对比（{results[1]['label']} − {results[0]['label']}）:")
        for metric in METRICS:
            delta = b[metric] - a[metric]
            print(f"  {metric:9s} {delta:+.4f}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--qrels", required=True, help="human labels JSON")
    parser.add_argument("--runs", nargs="+", required=True, help="one or more run JSON files")
    parser.add_argument("--k", type=int, default=3, help="cutoff (default 3)")
    parser.add_argument("--min-rel", type=int, default=2,
                        help="relevance threshold counted as relevant (default 2=适配)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    if args.k < 1:
        parser.error("--k must be >= 1")
    try:
        qrels = load(args.qrels)
        check_schema(qrels, "qrels", args.qrels)
        results = []
        for path in args.runs:
            run = load(path)
            check_schema(run, "run", path)
            results.append(evaluate(qrels, run, args.k, args.min_rel))
    except (OSError, ValueError, TypeError) as exc:
        print(f"eval error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
