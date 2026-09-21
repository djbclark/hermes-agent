"""Tests for tools/mcp_tolerant_list.py (fork: tolerant MCP ``tools/list``).

One malformed tool definition from an MCP server (e.g. Bezalel's
``cards__checkout_profile``) used to make the SDK reject the whole
``ListToolsResult`` and park the server with zero tools. The fork wrapper drops
only the malformed tools. These tests lock that behaviour in.

The module mirrors the mcp 2.x session plumbing (``_dispatcher``, ``next_cursor``) and the
project pins ``mcp==2.0.0``. Tests that drive the lenient listing therefore need mcp 2.x and are
skipped on an older SDK (the dev venv can lag the pin); the error filter and the installed-wrapper
tests are SDK-independent and run everywhere.
"""
import asyncio
import importlib.util
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp.client.session")

import mcp.types as mcp_types_v1  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from tools import mcp_tolerant_list as tolerant  # noqa: E402

needs_mcp2 = pytest.mark.skipif(
    importlib.util.find_spec("mcp_types") is None,
    reason="lenient listing mirrors the mcp 2.x session API (project pins mcp==2.0.0)",
)

GOOD = {"name": "ok_tool", "description": "fine", "inputSchema": {"type": "object", "properties": {}}}
BAD_NAME_TYPE = {"name": 5, "inputSchema": {"type": "object"}}  # rejected by every SDK version
BAD_NO_NAME = {"inputSchema": {"type": "object"}}
BEZALEL_SHAPE = {
    "name": "cards__checkout_profile",
    "description": "no top-level type",
    "inputSchema": {"anyOf": [{"type": "object"}, {"type": "array"}]},
}


class _Dispatcher:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    async def send_raw_request(self, method, params, opts):
        self.calls.append((method, params, dict(opts)))
        return dict(self.raw)


def _session(raw, **attrs):
    return SimpleNamespace(_dispatcher=_Dispatcher(raw), **attrs)


def _tools_validation_error():
    """A real pydantic ValidationError located under the ``tools`` field."""
    with pytest.raises(ValidationError) as info:
        mcp_types_v1.ListToolsResult.model_validate({"tools": [BAD_NAME_TYPE]}, by_name=False)
    return info.value


@pytest.fixture(autouse=True)
def _isolated_module_state(monkeypatch):
    """Fresh warn-once set and install flag; restore ClientSession.list_tools afterwards."""
    monkeypatch.setattr(tolerant, "_TOLERANT_WARNED", set())
    monkeypatch.setattr(tolerant, "_TOLERANT_LIST_TOOLS_PATCHED", False)
    monkeypatch.setattr(ClientSession, "list_tools", ClientSession.list_tools)


# --------------------------------------------------------------------------- error filter


def test_validation_error_under_tools_field_is_recognised():
    assert tolerant._is_tools_list_validation_error(_tools_validation_error()) is True


def test_validation_error_elsewhere_is_not_recognised():
    with pytest.raises(ValidationError) as info:
        mcp_types_v1.Tool.model_validate({"inputSchema": {"type": "object"}}, by_name=False)  # loc == ("name",)
    assert tolerant._is_tools_list_validation_error(info.value) is False


@pytest.mark.parametrize("exc", [ValueError("x"), RuntimeError("boom"), TimeoutError()])
def test_non_validation_errors_are_not_recognised(exc):
    assert tolerant._is_tools_list_validation_error(exc) is False


# --------------------------------------------------------------------------- lenient listing


@needs_mcp2
def test_lenient_list_keeps_good_tools_and_drops_malformed_ones():
    session = _session({"tools": [BAD_NAME_TYPE, GOOD, "junk", BAD_NO_NAME]})
    result = asyncio.run(tolerant._lenient_list_tools(session, None))
    assert [t.name for t in result.tools] == ["ok_tool"]


@needs_mcp2
def test_lenient_list_with_no_bad_tools_is_a_plain_listing():
    session = _session({"tools": [GOOD]})
    result = asyncio.run(tolerant._lenient_list_tools(session, None))
    assert [t.name for t in result.tools] == ["ok_tool"]


@needs_mcp2
def test_lenient_list_warns_once_per_dropped_tool(caplog):
    session = _session({"tools": [BAD_NAME_TYPE, GOOD]})
    with caplog.at_level(logging.WARNING, logger=tolerant.logger.name):
        asyncio.run(tolerant._lenient_list_tools(session, None))
        asyncio.run(tolerant._lenient_list_tools(session, None))
    dropped = [r for r in caplog.records if "dropped malformed tool" in r.getMessage()]
    assert len(dropped) == 1


@needs_mcp2
def test_lenient_list_mirrors_session_stamp_and_read_timeout():
    stamped = {}

    def stamp(data, opts):
        stamped["data"] = data
        opts["stamped"] = True

    session = _session({"tools": [GOOD]}, _stamp=stamp, _session_read_timeout_seconds=7)
    asyncio.run(tolerant._lenient_list_tools(session, None))
    method, _params, opts = session._dispatcher.calls[0]
    assert method == "tools/list"
    assert opts == {"stamped": True, "timeout": 7}
    assert stamped["data"]["method"] == "tools/list"


@needs_mcp2
def test_lenient_list_reports_completeness_to_the_session():
    absorbed = []

    def absorb(result, complete):
        absorbed.append(complete)
        return result

    asyncio.run(tolerant._lenient_list_tools(_session({"tools": [GOOD]}, _absorb_tool_listing=absorb), None))
    paged = _session({"tools": [GOOD], "nextCursor": "page-2"}, _absorb_tool_listing=absorb)
    asyncio.run(tolerant._lenient_list_tools(paged, None))
    assert absorbed == [True, False]


@needs_mcp2
def test_lenient_list_drops_the_real_bezalel_shape_on_mcp2():
    session = _session({"tools": [BEZALEL_SHAPE, GOOD]})
    result = asyncio.run(tolerant._lenient_list_tools(session, None))
    assert [t.name for t in result.tools] == ["ok_tool"]


@needs_mcp2
def test_lenient_list_clamps_negative_ttl_on_mcp2():
    session = _session({"tools": [GOOD], "ttlMs": -5})
    result = asyncio.run(tolerant._lenient_list_tools(session, None))
    assert result.ttl_ms == 0


# --------------------------------------------------------------------------- installed wrapper


def _install_over(monkeypatch, original):
    """Install the wrapper on top of ``original`` and return the wrapper."""
    monkeypatch.setattr(ClientSession, "list_tools", original)
    tolerant.install_tolerant_list_tools()
    return ClientSession.list_tools


def test_install_is_idempotent_and_marks_the_wrapper(monkeypatch):
    async def original(self, **kw):
        return "orig"

    wrapper = _install_over(monkeypatch, original)
    assert wrapper._hermes_tolerant is True
    assert wrapper._hermes_orig is original
    tolerant.install_tolerant_list_tools()
    assert ClientSession.list_tools is wrapper  # not double-wrapped


def test_install_recognises_an_already_wrapped_method(monkeypatch):
    async def original(self, **kw):
        return "orig"

    wrapper = _install_over(monkeypatch, original)
    monkeypatch.setattr(tolerant, "_TOLERANT_LIST_TOOLS_PATCHED", False)  # simulate a fresh module state
    tolerant.install_tolerant_list_tools()
    assert ClientSession.list_tools is wrapper
    assert tolerant._TOLERANT_LIST_TOOLS_PATCHED is True


def test_wrapper_passes_valid_results_through_untouched(monkeypatch):
    async def original(self, **kw):
        return "orig-result"

    async def must_not_run(session, params):
        raise AssertionError("lenient path must not run when strict validation succeeds")

    monkeypatch.setattr(tolerant, "_lenient_list_tools", must_not_run)
    wrapper = _install_over(monkeypatch, original)
    assert asyncio.run(wrapper(SimpleNamespace())) == "orig-result"


def test_wrapper_falls_back_on_a_tools_validation_error_and_sticks(monkeypatch):
    strict_calls = []

    async def original(self, **kw):
        strict_calls.append(1)
        raise _tools_validation_error()

    async def lenient(session, params):
        return "lenient-result"

    monkeypatch.setattr(tolerant, "_lenient_list_tools", lenient)
    wrapper = _install_over(monkeypatch, original)
    session = SimpleNamespace()
    assert asyncio.run(wrapper(session)) == "lenient-result"
    assert session._hermes_list_tools_lenient is True
    assert asyncio.run(wrapper(session)) == "lenient-result"
    assert len(strict_calls) == 1  # a server known to be lenient skips the strict attempt


def test_wrapper_propagates_unrelated_errors(monkeypatch):
    async def original(self, **kw):
        raise RuntimeError("connection closed")

    async def must_not_run(session, params):
        raise AssertionError("lenient path must not mask unrelated errors")

    monkeypatch.setattr(tolerant, "_lenient_list_tools", must_not_run)
    wrapper = _install_over(monkeypatch, original)
    with pytest.raises(RuntimeError, match="connection closed"):
        asyncio.run(wrapper(SimpleNamespace()))


def test_wrapper_surfaces_the_original_error_when_the_fallback_also_fails(monkeypatch):
    strict_error = _tools_validation_error()

    async def original(self, **kw):
        raise strict_error

    async def lenient(session, params):
        raise RuntimeError("fallback broke")

    monkeypatch.setattr(tolerant, "_lenient_list_tools", lenient)
    wrapper = _install_over(monkeypatch, original)
    with pytest.raises(ValidationError) as info:
        asyncio.run(wrapper(SimpleNamespace()))
    assert info.value is strict_error
    assert isinstance(info.value.__cause__, RuntimeError)


def test_wrapper_delegates_calls_it_does_not_understand(monkeypatch):
    seen = []

    async def original(self, *args, **kw):
        seen.append((args, kw))
        return "orig"

    wrapper = _install_over(monkeypatch, original)
    assert asyncio.run(wrapper(SimpleNamespace(), "positional")) == "orig"
    assert asyncio.run(wrapper(SimpleNamespace(), extra=1)) == "orig"
    assert seen == [(("positional",), {}), ((), {"extra": 1})]


# --------------------------------------------------------------------------- wiring


def test_mcp_loop_still_installs_the_wrapper():
    """Upstream merges rewrite mcp_tool_loop; this is the one fork hook it must keep."""
    import inspect

    from tools import mcp_tool_loop

    source = inspect.getsource(mcp_tool_loop)
    assert "from tools.mcp_tolerant_list import install_tolerant_list_tools" in source
    assert "install_tolerant_list_tools()" in source
