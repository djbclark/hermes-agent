"""CLI subcommand: `hermes memory-journal <subcommand>`.

Thin shell around the deterministic projection consumer in
``tools/memory_projection.py`` -- claims/applies queued memory writes from
the SQLite journal (``tools/memory_pending_queue.py``), evicting entries by
deterministic rule when the bounded store is full instead of re-queuing.
No LLM calls happen anywhere in this path.

This module intentionally has no side effects at import time -- main.py
wires the argparse subparsers on demand (same convention as
``hermes_cli/curator.py``).
"""

from __future__ import annotations

import argparse
import json as _json
from datetime import datetime, timezone
from typing import Optional


def _fmt_age(seconds: float) -> str:
    secs = int(seconds)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return str(ts)
    return dt.isoformat(timespec="seconds")


def _cmd_status(args) -> int:
    from tools import memory_projection as mp

    status = mp.get_status()

    if getattr(args, "json", False):
        print(_json.dumps(status, indent=2, ensure_ascii=False))
        return 0

    print(f"memory journal: {'BEHIND' if status['behind'] else 'OK'}")
    print(f"  active:       {status['active_count']} "
          f"(pending={status['pending_count']} processing={status['processing_count']} "
          f"failed={status['failed_count']})")
    print(f"  dead-letter:  {status['dead_letter_count']}")
    print(f"  oldest age:   {_fmt_age(status['oldest_age_seconds'])}")
    if status["last_error"]:
        print(f"  last error:   [{status['last_error_record_id']}] {status['last_error']}")
    else:
        print("  last error:   (none)")
    return 0


def _cmd_run(args) -> int:
    from tools import memory_projection as mp

    max_records = getattr(args, "max_records", 100)
    result = mp.run_once(max_records=max_records)

    if getattr(args, "json", False):
        print(_json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print(f"memory journal: processed {result['processed']} record(s) "
          f"(owner={result['owner']})")
    for outcome, count in sorted(result["counts"].items()):
        print(f"  {outcome:12s} {count}")
    for r in result["results"]:
        if r["outcome"] not in ("done",):
            print(f"  [{r['outcome']}] {r['id']} {r['action']}/{r['target']}: {r.get('error', '')}")
    return 0


def _cmd_list_dead(args) -> int:
    from tools import memory_projection as mp

    limit = getattr(args, "limit", 50)
    records = mp.list_dead_letters(limit=limit)

    if getattr(args, "json", False):
        print(_json.dumps(records, indent=2, ensure_ascii=False))
        return 0

    if not records:
        print("memory journal: no dead-lettered records")
        return 0

    print(f"dead-lettered records ({len(records)}):")
    for r in records:
        print(f"  {r['id']}  {r['action']:8s} {r['target']:8s}  "
              f"attempts={r['attempts']}  {_fmt_ts(r['updated_at'])}")
        if r.get("error_detail"):
            print(f"      {r['error_detail']}")
    return 0


def _cmd_list_evicted(args) -> int:
    from tools import memory_pending_queue as pq

    target = getattr(args, "target", None)
    limit = getattr(args, "limit", 50)
    records = pq.list_evictions(target=target, limit=limit)

    if getattr(args, "json", False):
        print(_json.dumps(records, indent=2, ensure_ascii=False))
        return 0

    if not records:
        print("memory journal: no evicted entries")
        return 0

    print(f"evicted entries ({len(records)}):")
    for r in records:
        preview = r["entry_text"][:80] + ("..." if len(r["entry_text"]) > 80 else "")
        print(f"  {_fmt_ts(r['evicted_at'])}  target={r['target']:8s}  {preview}")
    return 0


def register_cli(parent: argparse.ArgumentParser) -> None:
    """Attach `memory-journal` subcommands to *parent*."""
    parent.set_defaults(func=lambda a: (parent.print_help(), 0)[1])
    subs = parent.add_subparsers(dest="memory_journal_command")

    p_status = subs.add_parser(
        "status", help="Show journal backlog, oldest age, and last error"
    )
    p_status.add_argument("--json", action="store_true", help="Emit JSON")
    p_status.set_defaults(func=_cmd_status)

    p_run = subs.add_parser(
        "run", help="Claim and apply queued memory writes (deterministic, no LLM)"
    )
    p_run.add_argument(
        "--max-records", type=int, default=100,
        help="Stop after applying this many records (default: 100)",
    )
    p_run.add_argument("--json", action="store_true", help="Emit JSON")
    p_run.set_defaults(func=_cmd_run)

    p_dead = subs.add_parser(
        "list-dead", help="List dead-lettered records (exhausted retries or conflicts)"
    )
    p_dead.add_argument("--limit", type=int, default=50)
    p_dead.add_argument("--json", action="store_true", help="Emit JSON")
    p_dead.set_defaults(func=_cmd_list_dead)

    p_evicted = subs.add_parser(
        "list-evicted", help="List entries evicted from the bounded store to make room"
    )
    p_evicted.add_argument("--target", choices=["memory", "user"], default=None)
    p_evicted.add_argument("--limit", type=int, default=50)
    p_evicted.add_argument("--json", action="store_true", help="Emit JSON")
    p_evicted.set_defaults(func=_cmd_list_evicted)


def cli_main(argv=None) -> int:
    """Standalone entry (also usable by hermes_cli.main fallthrough)."""
    parser = argparse.ArgumentParser(prog="hermes memory-journal")
    register_cli(parser)
    args = parser.parse_args(argv)
    fn = getattr(args, "func", None)
    if fn is None:
        parser.print_help()
        return 0
    return int(fn(args) or 0)
