#!/usr/bin/env python3
"""Drop match runs that are old and not behind a published card.

`runs` is the only table that grows on its own: every match request stores the
full candidate list plus a project snapshot. Nothing else in the database grows
without a deliberate action, so this is the only table that needs a retention
policy.

Safety rules baked into `server.prune`:

- A run referenced by a card is never deleted, however old it is. `cards.run_id`
  is how a reader's feedback is traced back to what they were shown.
- Visits and feedback are never touched. They are the numbers the results board
  reports; deleting them would rewrite past totals.
- Dry run by default at the CLI level, so the first invocation on a real host
  cannot surprise anyone.

Usage:

    python scripts/prune.py                  # report only
    python scripts/prune.py --apply          # actually delete
    python scripts/prune.py --apply --days 30

Cron (weekly, after the nightly backup):

    30 4 * * 0 /srv/zhiyu/app/scripts/prune.py --apply >> /var/log/zhiyu-prune.log 2>&1

Set ZHIYU_DB or pass --db when the database is not at the deployed default.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402  (path must be set first)


def main():
    parser = argparse.ArgumentParser(description="Prune old match runs.")
    parser.add_argument("--days", type=int, default=None, help="retention window, default 90 or ZHIYU_RUN_RETENTION_DAYS")
    parser.add_argument("--apply", action="store_true", help="delete, instead of only reporting")
    parser.add_argument("--db", default=None, help="path to the SQLite file, overrides ZHIYU_DB")
    args = parser.parse_args()

    if args.db:
        server.DB = Path(args.db)
    if not server.DB.is_file():
        print(f"database not found: {server.DB}", file=sys.stderr)
        return 1

    report = server.prune(days=args.days, dry_run=not args.apply)
    print(json.dumps(report, ensure_ascii=False))
    if report["dryRun"] and report["candidates"]:
        print(f"一行未删。确认后加 --apply 删除 {report['candidates']} 条。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
