#!/usr/bin/env python3
"""Shared handlers for the /memory and /skills write-approval subcommands.

Both the interactive CLI (``cli.py``) and the gateway (``gateway/run.py``) call
into this module so the pending-review UX (list / approve / reject / diff /
mode) lives in one place. Each caller owns only its surface concerns:
formatting the returned text and, for the gateway, persisting config + evicting
the cached agent on a mode change.

Every public handler returns a plain text string suitable for both a terminal
and a chat message. Skill diffs are intentionally NOT inlined here — the
``diff`` handler returns the full diff for the CLI pager, but on a messaging
platform the gateway truncates it and points the user at the dashboard / file.
"""

from __future__ import annotations

import json
from typing import List, Optional

from tools import memory_pending_queue as pq
from tools import memory_pending_migration as mpm
from tools import memory_projection as mp
from tools import write_approval as wa


def _fmt_state(subsystem: str) -> str:
    on = wa.write_approval_enabled(subsystem)
    return f"{subsystem}.write_approval = {'on' if on else 'off'}"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_pending_list(subsystem: str) -> str:
    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."
    lines = [f"Pending {subsystem} writes ({len(records)}):"]
    for r in records:
        origin = r.get("origin", "foreground")
        tag = " [auto]" if origin == "background_review" else ""
        lines.append(f"  {r['id']}{tag}  {r.get('summary', '')}")
    where = "/{s} approve <id>".format(s=subsystem)
    lines.append("")
    lines.append(f"Apply: {where}   Reject: /{subsystem} reject <id>")
    if subsystem == wa.SKILLS:
        lines.append("Review full diff: /skills diff <id>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------

def handle_pending_subcommand(
    subsystem: str,
    args: List[str],
    *,
    memory_store=None,
    set_mode_fn=None,
) -> Optional[str]:
    """Dispatch a /memory or /skills subcommand.

    Args:
        subsystem: ``memory`` or ``skills``.
        args: tokens after the slash command (e.g. ``["approve", "a1b2"]``).
        memory_store: live MemoryStore for applying approved memory writes
            (CLI passes ``self.agent._memory_store``; gateway applies against a
            freshly loaded store).
        set_mode_fn: optional callable ``(enabled: bool) -> None`` that
            persists the new write_approval boolean to config (gateway provides
            this; CLI uses its own ``save_config_value`` and passes a closure).

    Returns a text string to show the user. Returns None when the args are not
    a write-approval subcommand (caller falls through to its other handling,
    e.g. /skills search).
    """
    if not args:
        # Bare /memory or /skills with no sub → show pending + gate state.
        return f"{_fmt_state(subsystem)}\n\n" + _fmt_pending_list(subsystem)

    sub = args[0].lower()
    rest = args[1:]

    if sub == "pending":
        return _fmt_pending_list(subsystem)

    if sub in {"approve", "apply"}:
        return _approve(subsystem, rest, memory_store)

    if sub in {"reject", "deny", "drop"}:
        return _reject(subsystem, rest)

    if sub == "diff" and subsystem == wa.SKILLS:
        return _diff(rest)

    if sub in {"approval", "mode"}:  # 'mode' kept as a back-compat alias
        return _set_approval(subsystem, rest, set_mode_fn)

    # -- journal / eviction / migration (memory only) --
    if sub == "migrate" and subsystem == wa.MEMORY:
        return _migrate()
    if sub == "evicted" and subsystem == wa.MEMORY:
        return _evicted()
    if sub == "journal" and subsystem == wa.MEMORY:
        return _journal()

    return None  # not ours — caller handles


def _resolve_one(subsystem: str, rest: List[str]):
    if not rest:
        return None, f"Usage: /{subsystem} approve|reject <id>  (or 'all')"
    return rest[0], None


def _approve(subsystem: str, rest: List[str], memory_store) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} approve <id>"

    records = wa.list_pending(subsystem)
    if not records:
        return f"No pending {subsystem} writes."

    if target.lower() == "all":
        targets = list(records)
    else:
        rec = wa.get_pending(subsystem, target)
        if not rec:
            return f"No pending {subsystem} write with id '{target}'."
        targets = [rec]

    applied, failed = 0, []
    for rec in targets:
        ok, msg = _apply_one(subsystem, rec, memory_store)
        if ok:
            wa.discard_pending(subsystem, rec["id"])
            applied += 1
        else:
            failed.append(f"{rec['id']}: {msg}")

    out = [f"Approved {applied} {subsystem} write(s)."]
    if failed:
        out.append("Failed:")
        out.extend(f"  {f}" for f in failed)
    return "\n".join(out)


def _apply_one(subsystem: str, rec, memory_store):
    payload = rec.get("payload", {})
    try:
        if subsystem == wa.MEMORY:
            if memory_store is None:
                return False, "memory store unavailable"
            from tools.memory_tool import apply_memory_pending
            result = apply_memory_pending(payload, memory_store)
            return bool(result.get("success")), result.get("error", "")
        else:
            from tools.skill_manager_tool import apply_skill_pending
            result = json.loads(apply_skill_pending(payload))
            return bool(result.get("success")), result.get("error", "")
    except Exception as e:
        return False, str(e)


def _reject(subsystem: str, rest: List[str]) -> str:
    target, err = _resolve_one(subsystem, rest)
    if err or target is None:
        return err or f"Usage: /{subsystem} reject <id>"
    if target.lower() == "all":
        n = 0
        for rec in wa.list_pending(subsystem):
            if wa.discard_pending(subsystem, rec["id"]):
                n += 1
        return f"Rejected {n} pending {subsystem} write(s)."
    if wa.discard_pending(subsystem, target):
        return f"Rejected pending {subsystem} write '{target}'."
    return f"No pending {subsystem} write with id '{target}'."


def _diff(rest: List[str]) -> str:
    if not rest:
        return "Usage: /skills diff <id>"
    rec = wa.get_pending(wa.SKILLS, rest[0])
    if not rec:
        return f"No pending skill write with id '{rest[0]}'."
    diff = wa.skill_pending_diff(rec)
    header = f"# Pending skill write {rec['id']}: {rec.get('summary', '')}\n"
    return header + "\n" + diff


def _set_approval(subsystem: str, rest: List[str], set_mode_fn) -> str:
    """Turn the approval gate on/off for a subsystem.

    ``set_mode_fn`` (when provided) persists the new boolean to config.
    """
    if not rest:
        return (f"{_fmt_state(subsystem)}\n"
                f"Set with: /{subsystem} approval <on|off>")
    arg = rest[0].strip().lower()
    truthy = {"on", "true", "yes", "1", "enable", "enabled"}
    falsey = {"off", "false", "no", "0", "disable", "disabled"}
    if arg in truthy:
        enabled = True
    elif arg in falsey:
        enabled = False
    else:
        return f"Invalid value '{arg}'. Use: on or off."
    if set_mode_fn is None:
        val = "true" if enabled else "false"
        return (f"To change the {subsystem} approval gate, run:\n"
                f"  hermes config set {subsystem}.write_approval {val}")
    try:
        set_mode_fn(enabled)
    except Exception as e:
        return f"Failed to set {subsystem}.write_approval: {e}"
    return f"{subsystem}.write_approval set to '{'on' if enabled else 'off'}'."


# ---------------------------------------------------------------------------
# Journal / eviction / migration handlers (memory only)
# ---------------------------------------------------------------------------


def _truncate_line(text: str, max_len: int = 120) -> str:
    """Truncate a line for chat-bubble display."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _migrate() -> str:
    """Run legacy-to-SQLite pending migration."""
    result = mpm.migrate_legacy_pending()
    migrated = len(result.get("migrated", []))
    failed = len(result.get("failed", []))
    if migrated == 0 and failed == 0:
        return "Legacy migration: nothing to migrate (no legacy files found)."
    out = [f"Legacy migration: {migrated} file(s) imported."]
    if failed:
        out.append(f"{failed} file(s) failed (will retry on next run):")
        for f in result["failed"][:5]:
            out.append(f"  - {f.get('file', '?')}")
        if len(result["failed"]) > 5:
            out.append(f"  ... and {len(result['failed']) - 5} more")
    return "\n".join(out)


def _evicted() -> str:
    """List dead-lettered and evicted entries (truncated for chat bubbles)."""
    dead = pq.list_all(status=pq.STATUS_DEAD)
    evicted = pq.list_evictions(limit=10)

    lines = []
    if dead:
        lines.append(f"Dead-lettered ({len(dead)}):")
        for r in dead[-10:]:
            kind = r.get("kind", "?")
            summary = str(r.get("summary") or r.get("payload", {}))
            if isinstance(summary, dict):
                summary = str(summary)[:80]
            line = f"  {r.get('id', '?')}  {kind}  {summary[:80]}"
            lines.append(_truncate_line(line))
    else:
        lines.append("Dead-lettered: 0")

    if evicted:
        lines.append(f"\nRecent evictions ({len(evicted)}):")
        for r in evicted:
            reason = str(r.get("reason") or "")[:80]
            line = f"  {r.get('id', '?')}  {r.get('target', '?')}  {reason}"
            lines.append(_truncate_line(line))
    else:
        lines.append("\nRecent evictions: 0")

    return "\n".join(lines)


def _journal() -> str:
    """Show projection journal status summary."""
    status = mp.get_status()
    lines = [
        "Memory journal status:",
        f"  Active: {status['active_count']}",
        f"  Pending: {status['pending_count']}",
        f"  Processing: {status['processing_count']}",
        f"  Failed: {status['failed_count']}",
        f"  Dead-lettered: {status['dead_letter_count']}",
        f"  Oldest age: {status['oldest_age_seconds']:.0f}s",
        f"  Behind: {'yes' if status['behind'] else 'no'}",
    ]
    if status.get("last_error"):
        lines.append(f"  Last error: {_truncate_line(str(status['last_error']), 120)}")
    return "\n".join(lines)
