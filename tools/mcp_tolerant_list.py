"""Fork: defensive ``tools/list`` parsing for MCP servers with malformed tool schemas.

Some external MCP servers (e.g. Bezalel) publish a ``tools/list`` where ONE tool has a
structurally invalid ``inputSchema`` (``{"anyOf": [{"type": "object"}, {"type": "array"}]}``
with no top-level ``type``). mcp 2.x then raises ``pydantic.ValidationError`` on the whole
``ListToolsResult`` and parks the server with zero tools. This wrapper is always-on but only
activates after a strict validation failure located under the ``tools`` field: it re-issues the
raw request, drops the malformed tool(s) with a warning, and validates the rest.

Installed once per process from ``tools.mcp_tool_loop._ensure_mcp_loop`` (idempotent,
thread-safe). Kept in its own module so upstream merges never touch it. Reviewers (claude-code,
agy gemini-3.1-pro-high, grok) agreed on lock + ValidationError-typed filter; the send_request
plumbing (``_stamp``, timeout, ``_absorb_tool_listing``) is mirrored on purpose.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_TOLERANT_LIST_TOOLS_PATCHED = False
_TOLERANT_INSTALL_LOCK = threading.Lock()
_TOLERANT_WARNED: set = set()


def _is_tools_list_validation_error(exc: BaseException) -> bool:
    """True only for a pydantic failure located under the ``tools`` field."""
    try:
        from pydantic import ValidationError
    except Exception:
        return False
    if not isinstance(exc, ValidationError):
        return False
    try:
        return any(e.get("loc") and e["loc"][0] == "tools" for e in exc.errors())
    except Exception:
        return False


async def _lenient_list_tools(session, params):
    """Raw tools/list mirroring ClientSession.send_request minus strict validation."""
    import mcp.types as types_

    request = types_.ListToolsRequest(params=params)
    data = request.model_dump(by_alias=True, mode="json", exclude_none=True)
    opts: dict = {}
    stamp = getattr(session, "_stamp", None)
    if callable(stamp):
        stamp(data, opts)
    timeout = getattr(session, "_session_read_timeout_seconds", None)
    if timeout is not None:
        opts["timeout"] = timeout
    raw = await session._dispatcher.send_raw_request("tools/list", data.get("params"), opts)
    ttl = raw.get("ttlMs")
    if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) and ttl < 0:
        raw["ttlMs"] = 0

    try:
        from mcp_types import methods as _methods
    except Exception:  # pragma: no cover - SDK layout drift
        _methods = None
    version = getattr(session, "_negotiated_version", None) or "2025-11-25"
    kept, dropped = [], []
    for t in raw.get("tools") or []:
        name = t.get("name", "<unnamed>") if isinstance(t, dict) else "<non-object>"
        try:
            if not isinstance(t, dict):
                raise ValueError("tool is not an object")
            try:
                if _methods is None:
                    raise KeyError
                _methods.validate_server_result("tools/list", version, {"tools": [t]})
            except KeyError:
                types_.Tool.model_validate(t, by_name=False)
            kept.append(t)
        except Exception as e:
            why = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
            dropped.append((name, why))
    for name, why in dropped:
        if name not in _TOLERANT_WARNED:
            _TOLERANT_WARNED.add(name)
            logger.warning("MCP tools/list: dropped malformed tool %r (%s)", name, why)
    raw = {**raw, "tools": kept}
    result = types_.ListToolsResult.model_validate(raw, by_name=False)
    complete = (params is None or getattr(params, "cursor", None) is None) and result.next_cursor is None
    absorb = getattr(session, "_absorb_tool_listing", None)
    return absorb(result, complete=complete) if absorb else result


def install_tolerant_list_tools() -> None:
    """Install a tolerant ``ClientSession.list_tools`` wrapper (idempotent, thread-safe)."""
    global _TOLERANT_LIST_TOOLS_PATCHED
    with _TOLERANT_INSTALL_LOCK:
        if _TOLERANT_LIST_TOOLS_PATCHED:
            return
        try:
            from mcp.client.session import ClientSession
        except Exception:
            return
        if getattr(ClientSession.list_tools, "_hermes_tolerant", False):
            _TOLERANT_LIST_TOOLS_PATCHED = True
            return
        _orig_list_tools = ClientSession.list_tools

        async def _tolerant_list_tools(self, *args, **kwargs):
            if args or not set(kwargs) <= {"params"}:
                return await _orig_list_tools(self, *args, **kwargs)
            strict_exc = None
            if not getattr(self, "_hermes_list_tools_lenient", False):
                try:
                    return await _orig_list_tools(self, **kwargs)
                except Exception as exc:
                    if not _is_tools_list_validation_error(exc):
                        raise
                    strict_exc = exc
                    logger.warning("tools/list strict-validation failed (%s); dropping malformed tools",
                                   type(exc).__name__)
            try:
                result = await _lenient_list_tools(self, kwargs.get("params"))
            except Exception as fb_exc:
                if strict_exc is not None:
                    logger.warning("MCP tolerant tools/list fallback failed (%s: %s); surfacing original validation error",
                                   type(fb_exc).__name__, str(fb_exc)[:200])
                    raise strict_exc from fb_exc
                raise
            try:
                self._hermes_list_tools_lenient = True
            except Exception:
                pass
            return result

        setattr(_tolerant_list_tools, "_hermes_tolerant", True)
        setattr(_tolerant_list_tools, "_hermes_orig", _orig_list_tools)
        ClientSession.list_tools = _tolerant_list_tools  # type: ignore[method-assign]
        _TOLERANT_LIST_TOOLS_PATCHED = True
        logger.info("installed tolerant list_tools wrapper (skips malformed tool schemas)")


# Backwards-compatible private alias (the original fork commit exposed it from tools.mcp_tool).
_install_tolerant_list_tools = install_tolerant_list_tools
