"""No-agent scheduled drain for the durable memory journal.

The scheduler expects a script that is silent when there is no work and emits
one concise line when records were processed or failed.  The actual consumer
and lease semantics live in :mod:`tools.memory_projection`.

On every run this script also appends a metrics snapshot (one JSON line) to
``~/.hermes/logs/memory_journal_metrics.jsonl`` for external monitoring, and
rotates that file when it exceeds 100 KB.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from tools import memory_projection
from tools.memory_tool import ENTRY_DELIMITER, load_on_disk_store
_METRICS_PATH = Path.home() / ".hermes" / "logs" / "memory_journal_metrics.jsonl"
_MAX_METRICS_BYTES = 100_000  # 100 KB


def _append_metrics_line(status: dict) -> None:
    """Append one JSON line to the metrics file, rotating at 100 KB."""
    store = load_on_disk_store()
    line = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "active_count": status["active_count"],
        "pending_count": status["pending_count"],
        "processing_count": status["processing_count"],
        "failed_count": status["failed_count"],
        "dead_letter_count": status["dead_letter_count"],
        "evicted_count": status["evicted_count"],
        "memory_char_used": len(ENTRY_DELIMITER.join(store.memory_entries)),
        "memory_char_limit": store.memory_char_limit,
        "user_char_used": len(ENTRY_DELIMITER.join(store.user_entries)),
        "user_char_limit": store.user_char_limit,
        "behind": status["behind"],
    }

    _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Rotate: keep only one backup at .1
    if _METRICS_PATH.exists() and _METRICS_PATH.stat().st_size >= _MAX_METRICS_BYTES:
        rotated = _METRICS_PATH.with_suffix(".jsonl.1")
        shutil.move(str(_METRICS_PATH), str(rotated))

    with open(_METRICS_PATH, "a") as f:
        json.dump(line, f)
        f.write("\n")


def _format_metrics_summary(status: dict) -> str:
    """Build the one-line metrics summary string."""
    store = load_on_disk_store()
    memory_used = len(ENTRY_DELIMITER.join(store.memory_entries))
    user_used = len(ENTRY_DELIMITER.join(store.user_entries))
    return (
        f"memory journal: ok, "
        f"{status['active_count']} active, "
        f"{status['dead_letter_count']} dead, "
        f"{status['evicted_count']} evicted, "
        f"{memory_used}/{store.memory_char_limit} memory, "
        f"{user_used}/{store.user_char_limit} user"
    )


def main() -> int:
    result = memory_projection.run_once(max_records=100)
    processed = int(result.get("processed", 0))
    counts = result.get("counts", {})
    failures = [
        row for row in result.get("results", [])
        if row.get("outcome") not in {"done"}
    ]

    status = memory_projection.get_status()
    _append_metrics_line(status)

    if not processed and not failures:
        print(_format_metrics_summary(status))
        return 0

    parts = [f"Hermes memory journal processed {processed} record(s)."]
    if counts:
        parts.append(" ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    if failures:
        parts.append(
            "Failures: "
            + "; ".join(
                f"{row.get('id')} {row.get('outcome')}: {row.get('error', '')}"
                for row in failures
            )
        )
    print(" ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
