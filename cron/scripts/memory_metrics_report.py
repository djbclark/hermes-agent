"""Report deterministic memory-curator decision metrics; never mutates memory."""
from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path

from tools import memory_pending_queue as pq
from tools import memory_projection


def _entries(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip() and line.strip() != "§"
    ]


def build_report(home: Path | None = None) -> dict:
    home = home or (Path.home() / ".hermes")
    memory = home / "memories" / "MEMORY.md"
    user = home / "memories" / "USER.md"
    status = memory_projection.get_status()
    evictions = pq.list_evictions(limit=200)
    dead = pq.list_all(status=pq.STATUS_DEAD)
    mem_entries = _entries(memory)
    duplicate_pairs = [
        round(SequenceMatcher(None, left, right).ratio(), 3)
        for i, left in enumerate(mem_entries)
        for right in mem_entries[i + 1 :]
        if SequenceMatcher(None, left, right).ratio() >= 0.75
    ]
    return {
        "memory_bytes": memory.stat().st_size if memory.exists() else 0,
        "memory_limit": 6000,
        "user_bytes": user.stat().st_size if user.exists() else 0,
        "user_limit": 3500,
        "memory_entry_count": len(mem_entries),
        "evictions_recorded": len(evictions),
        "dead_letters_recorded": len(dead),
        "duplicate_candidate_pairs": len(duplicate_pairs),
        "duplicate_max_similarity": max(duplicate_pairs, default=0),
        "journal_status": status,
    }


def main() -> None:
    print(json.dumps(build_report(), sort_keys=True))


if __name__ == "__main__":
    main()
