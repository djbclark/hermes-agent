"""No-agent scheduled drain for the durable memory journal.

The scheduler expects a script that is silent when there is no work and emits
one concise line when records were processed or failed.  The actual consumer
and lease semantics live in :mod:`tools.memory_projection`.
"""

from __future__ import annotations

from tools import memory_projection


def main() -> int:
    result = memory_projection.run_once(max_records=100)
    processed = int(result.get("processed", 0))
    counts = result.get("counts", {})
    failures = [
        row for row in result.get("results", [])
        if row.get("outcome") not in {"done"}
    ]

    if not processed and not failures:
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
