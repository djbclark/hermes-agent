"""Startup-latency regressions: probe-mode aux clients, lazy MCP SDK,
non-blocking banner update check.

These pin the CLI cold-start contract established in the sub-400ms pass:
- check_fn availability probes must not import the OpenAI SDK or build
  real HTTP clients (aux_probe_mode).
- tools/mcp_tool must not import the `mcp` SDK at module import time.
- build_welcome_banner must not block on the update-check prefetch.
"""

import sys
import threading
import time
from unittest.mock import patch

import pytest


class TestAuxProbeMode:
    def test_probe_mode_returns_stub_without_openai_import(self):
        import agent.auxiliary_client as aux

        with aux.aux_probe_mode():
            client = aux._create_openai_client(api_key="k", base_url="https://x.invalid/v1")
        assert isinstance(client, aux._AuxProbeClientStub)
        assert client.api_key == "k"

    def test_probe_stub_never_cached(self):
        import agent.auxiliary_client as aux

        stub = aux._AuxProbeClientStub()
        key = ("probe-test", False, "", "", "", (), False, "", None, "m")
        aux._store_cached_client(key, stub, "m")
        with aux._client_cache_lock:
            assert key not in aux._client_cache

    @pytest.mark.parametrize("wrap", [False, True], ids=["bare-stub", "adapter-wrapped-stub"])
    def test_repeat_probes_stay_resolvable_and_never_cache(self, wrap):
        """Probes go through _get_cached_client's inline store, not _store_cached_client,
        so the stub (bare, or wrapped in a Codex adapter whose leaf is the stub) used to
        land under the runtime key; the next probe then hit _compat_model() on the stub
        and check_vision_requirements() flipped to False for the process (#87654).
        """
        import agent.auxiliary_client as aux

        def _resolve(*a, **k):
            stub = aux._AuxProbeClientStub(api_key="k", base_url="https://x.invalid/v1")
            return (aux.CodexAuxiliaryClient(stub, "v/m") if wrap else stub), "v/m"

        aux.shutdown_cached_clients()
        with patch.object(aux, "resolve_provider_client", _resolve), aux.aux_probe_mode():
            answers = [aux._get_cached_client("probe-guard-test", "vendor/model")[0] is not None for _ in range(3)]
        assert answers == [True, True, True]
        with aux._client_cache_lock:
            assert not [k for k in aux._client_cache if k[1:2] == (False,) and k[-1] == "vendor/model"]

    def test_probe_stub_raises_on_runtime_use(self):
        import agent.auxiliary_client as aux

        stub = aux._AuxProbeClientStub()
        with pytest.raises(RuntimeError, match="availability checks only"):
            _ = stub.chat

    def test_probe_mode_is_scoped_and_reentrant(self):
        import agent.auxiliary_client as aux

        assert not aux._aux_probe_active()
        with aux.aux_probe_mode():
            assert aux._aux_probe_active()
            with aux.aux_probe_mode():
                assert aux._aux_probe_active()
            # inner exit must not clear the outer scope
            assert aux._aux_probe_active()
        assert not aux._aux_probe_active()

    def test_probe_mode_is_thread_local(self):
        import agent.auxiliary_client as aux

        seen = {}

        def other_thread():
            seen["active"] = aux._aux_probe_active()

        with aux.aux_probe_mode():
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
        assert seen["active"] is False

    def test_maybe_wrap_anthropic_passes_stub_through(self):
        import agent.auxiliary_client as aux

        stub = aux._AuxProbeClientStub(base_url="https://api.anthropic.com")
        out = aux._maybe_wrap_anthropic(stub, "m", "key", "https://api.anthropic.com")
        assert out is stub

    def test_to_async_client_passes_stub_through(self):
        import agent.auxiliary_client as aux

        stub = aux._AuxProbeClientStub()
        client, model = aux._to_async_client(stub, "m")
        assert client is stub
        assert model == "m"


class TestVisionCheckUsesProbeMode:
    def test_check_vision_requirements_enters_probe_mode(self):
        from tools import vision_tools
        import agent.auxiliary_client as aux

        states = []

        def fake_resolver(*a, **k):
            states.append(aux._aux_probe_active())
            return ("nous", aux._AuxProbeClientStub(), "m")

        with patch.object(aux, "resolve_vision_provider_client", fake_resolver):
            assert vision_tools.check_vision_requirements() is True
        assert states and all(states)


class TestLazyMcpSdk:
    def test_module_import_does_not_import_mcp_sdk(self):
        """Importing tools.mcp_tool must not pull in the `mcp` package."""
        import subprocess

        code = (
            "import sys; sys.modules.pop('mcp', None); "
            "import tools.mcp_tool; "
            "assert 'mcp' not in sys.modules, 'mcp imported eagerly'; "
            "print('ok')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "ok" in proc.stdout

    def test_availability_flag_reflects_find_spec(self):
        import importlib.util
        from tools import mcp_tool

        expected = importlib.util.find_spec("mcp") is not None
        assert mcp_tool._MCP_AVAILABLE is expected

    def test_ensure_mcp_sdk_binds_symbols(self):
        import importlib.util
        from tools import mcp_tool

        if importlib.util.find_spec("mcp") is None:
            pytest.skip("mcp SDK not installed")
        assert mcp_tool._ensure_mcp_sdk() is True
        assert mcp_tool.ClientSession is not None
        assert mcp_tool.stdio_client is not None

    def test_ensure_respects_patched_unavailable(self):
        from tools import mcp_tool

        with patch.object(mcp_tool, "_MCP_AVAILABLE", False):
            assert mcp_tool._ensure_mcp_sdk() is False

    def test_lazy_symbol_getattr_resolves_via_ensure(self):
        import importlib.util
        from tools import mcp_tool

        if importlib.util.find_spec("mcp") is None:
            pytest.skip("mcp SDK not installed")
        # getattr through the module (what mock.patch does when saving the
        # original) must materialize the symbol instead of AttributeError.
        assert getattr(mcp_tool, "StdioServerParameters") is not None

    def test_parallel_ensure_does_not_skip_http_when_clientsession_appears(self):
        """Waiters must not treat a mid-import ClientSession as 'SDK ready'.

        Unlocked `ClientSession is not None` made HTTP MCP servers raise
        "Upgrade the mcp package to get HTTP support" while a sibling was
        still importing streamable_http — the package was already installed.
        """
        import importlib.util
        from tools import mcp_tool

        if importlib.util.find_spec("mcp") is None:
            pytest.skip("mcp SDK not installed")
        if importlib.util.find_spec("mcp.client.streamable_http") is None:
            pytest.skip("mcp HTTP extra not installed")

        mcp_tool._MCP_SDK_IMPORT_ATTEMPTED = False
        mcp_tool.ClientSession = None
        mcp_tool.stdio_client = None
        mcp_tool._MCP_HTTP_AVAILABLE = False
        mcp_tool._MCP_NEW_HTTP = False
        mcp_tool._MCP_LEGACY_HTTP = False

        original_import = mcp_tool._import_sdk_names
        bound_clientsession = threading.Event()

        def slow_import(module, names, missing_msg=None):
            result = original_import(module, names, missing_msg)
            if module == "mcp" and "ClientSession" in names:
                bound_clientsession.set()
                time.sleep(0.2)
            return result

        http_seen = []

        def waiter():
            bound_clientsession.wait(timeout=2)
            mcp_tool._ensure_mcp_sdk()
            http_seen.append(mcp_tool._MCP_HTTP_AVAILABLE)

        t = threading.Thread(target=waiter)
        with patch.object(mcp_tool, "_import_sdk_names", slow_import):
            t.start()
            assert mcp_tool._ensure_mcp_sdk() is True
            t.join(timeout=3)
        assert not t.is_alive()
        assert mcp_tool._MCP_HTTP_AVAILABLE is True
        assert http_seen == [True]
        # Leave the module in the post-ensure state later tests expect.
        mcp_tool._MCP_SDK_IMPORT_ATTEMPTED = False
        mcp_tool.ClientSession = None
        assert mcp_tool._ensure_mcp_sdk() is True

    def test_prebound_clientsession_still_probes_http(self):
        """If ClientSession is already bound (e.g. by a mock before mcp was loaded),
        _ensure_mcp_sdk should still probe for the optional HTTP transports."""
        import importlib.util
        from tools import mcp_tool

        if importlib.util.find_spec("mcp") is None:
            pytest.skip("mcp SDK not installed")
        if importlib.util.find_spec("mcp.client.streamable_http") is None:
            pytest.skip("mcp HTTP extra not installed")

        mcp_tool._MCP_SDK_IMPORT_ATTEMPTED = False
        mcp_tool._MCP_HTTP_AVAILABLE = False
        mcp_tool.ClientSession = object()  # pre-bind

        try:
            assert mcp_tool._ensure_mcp_sdk() is True
            assert mcp_tool._MCP_HTTP_AVAILABLE is True
        finally:
            mcp_tool._MCP_SDK_IMPORT_ATTEMPTED = False
            mcp_tool.ClientSession = None
            mcp_tool._ensure_mcp_sdk()

    @pytest.mark.asyncio
    async def test_run_http_last_chance_import(self):
        """If _MCP_HTTP_AVAILABLE is False because HTTP was installed after the gateway
        started, _run_http should probe it one last time before raising."""
        import importlib.util
        from tools import mcp_tool
        from tools.mcp_tool_transport import MCPServerTransportMixin

        if importlib.util.find_spec("mcp") is None:
            pytest.skip("mcp SDK not installed")
        if importlib.util.find_spec("mcp.client.streamable_http") is None:
            pytest.skip("mcp HTTP extra not installed")

        class DummyServer(MCPServerTransportMixin):
            name = "dummy"
            def __init__(self):
                pass
            async def _serve_transport(self, transport_cm, label, connect_timeout):
                return "served"

        server = DummyServer()
        server._sse_fallback = False
        server._ever_connected = False
        server._auth_type = None

        mcp_tool._MCP_SDK_IMPORT_ATTEMPTED = True
        mcp_tool._MCP_HTTP_AVAILABLE = False

        try:
            result = await server._run_http({"url": "http://localhost", "connect_timeout": 1})
            assert result == "served"
            assert mcp_tool._MCP_HTTP_AVAILABLE is True
        finally:
            mcp_tool._MCP_SDK_IMPORT_ATTEMPTED = False
            mcp_tool.ClientSession = None
            mcp_tool._ensure_mcp_sdk()


class TestBannerUpdateCheckNonBlocking:
    def test_banner_does_not_block_on_pending_update_check(self):
        """When the prefetch hasn't finished, the banner path must return in
        well under the old 500ms blocking wait."""
        import hermes_cli.banner as banner

        with patch.object(banner, "_update_check_done", threading.Event()), \
             patch.object(banner, "_deferred_update_notice_started", False):
            start = time.perf_counter()
            behind = banner.get_update_result(timeout=0.05)
            if behind is None and not banner._update_check_done.is_set():
                banner._defer_update_notice()
            elapsed = time.perf_counter() - start
        assert elapsed < 0.3, f"banner update check blocked {elapsed:.3f}s"

    def test_deferred_notice_prints_through_prompt_toolkit_renderer(self):
        """The late notice lands after patch_stdout owns stdout, where raw ESC bytes are
        sanitized into visible ``?[1;33m`` text (#83969). It must reach prompt_toolkit as a
        parsed ANSI fragment — never as a bare ``Console.print`` to stdout."""
        import hermes_cli.banner as banner
        from prompt_toolkit.formatted_text import ANSI, to_formatted_text

        printed = []
        done = threading.Event()
        with patch.object(banner, "_update_check_done", done), \
             patch.object(banner, "_update_result", None), \
             patch.object(banner, "_deferred_update_notice_started", False), \
             patch("prompt_toolkit.print_formatted_text", side_effect=lambda *a, **k: printed.append(a[0])):
            banner._defer_update_notice(max_wait=5.0)
            banner._update_result = 3
            done.set()
            deadline = time.time() + 5
            while not printed and time.time() < deadline:
                time.sleep(0.02)
        assert printed, "deferred update notice never reached prompt_toolkit's renderer"
        assert isinstance(printed[0], ANSI)
        visible = "".join(text for _style, text, *_ in to_formatted_text(printed[0]))
        assert "3 commits behind" in visible
        assert "\x1b" not in visible and "[bold" not in visible

    def test_deferred_notice_silent_when_up_to_date(self):
        import hermes_cli.banner as banner

        printed = []

        def _fake_cprint(text):
            printed.append(text)

        done = threading.Event()
        with patch.object(banner, "_update_check_done", done), \
             patch.object(banner, "_update_result", 0), \
             patch.object(banner, "_deferred_update_notice_started", False), \
             patch.object(banner, "cprint", _fake_cprint):
            banner._defer_update_notice(max_wait=2.0)
            done.set()
            time.sleep(0.3)
        assert not printed
