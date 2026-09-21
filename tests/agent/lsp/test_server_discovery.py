"""Invariant tests for brew-first LSP discovery, multi-server Python, and CLI which.

Contracts (not snapshots of catalog size or exact default timeouts):
- Python files match both pyright and ruff; first-match stays pyright.
- basedpyright is preferred without changing the pyright config key.
- typescript spawn injects tsserver.path only when classic tsserver is missing,
  and user initialization_options merge instead of clobbering.
- rust-analyzer waits longer than the document default and seeds the first push.
- ``hermes lsp which`` honors a config command override when that path exists.
"""
from __future__ import annotations

import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from agent.lsp.client import DIAGNOSTICS_DOCUMENT_WAIT
from agent.lsp.install import INSTALL_RECIPES
from agent.lsp.servers import (
    RUST_ANALYZER_WAIT_TIMEOUT,
    SERVERS,
    ServerContext,
    find_server_for_file,
    find_servers_for_file,
    language_id_for,
)


def _by_id(server_id: str):
    return next(s for s in SERVERS if s.server_id == server_id)


def test_python_matches_pyright_and_ruff_without_stealing_pyright():
    """ruff shares .py with pyright; first-match remains pyright so config keys stay stable."""
    ids = [s.server_id for s in find_servers_for_file("pkg/mod.py")]
    assert "pyright" in ids and "ruff" in ids
    assert find_server_for_file("pkg/mod.py").server_id == "pyright"
    assert ids.index("pyright") < ids.index("ruff")


def test_markdown_and_toml_language_ids_and_servers():
    assert language_id_for("README.md") == "markdown"
    assert language_id_for("Cargo.toml") == "toml"
    assert find_server_for_file("README.md").server_id == "marksman"
    assert find_server_for_file("pyproject.toml").server_id == "taplo"


def test_ruff_marksman_taplo_spawn_commands(monkeypatch, tmp_path):
    """Each new server's default argv is the brew/PATH form, not a guessed --stdio."""
    bins = {
        "ruff": str(tmp_path / "ruff"),
        "marksman": str(tmp_path / "marksman"),
        "taplo": str(tmp_path / "taplo"),
    }
    for path in bins.values():
        Path(path).write_text("x")

    def fake_which(*names):
        for n in names:
            if n in bins:
                return bins[n]
        return None

    monkeypatch.setattr("agent.lsp.servers._which", fake_which)
    ctx = ServerContext(workspace_root=str(tmp_path), install_strategy="manual")
    assert _by_id("ruff").build_spawn(str(tmp_path), ctx).command == [bins["ruff"], "server"]
    assert _by_id("marksman").build_spawn(str(tmp_path), ctx).command == [bins["marksman"], "server"]
    assert _by_id("taplo").build_spawn(str(tmp_path), ctx).command == [bins["taplo"], "lsp", "stdio"]


def test_install_recipes_cover_new_path_servers():
    for pkg in ("ruff", "marksman", "taplo"):
        assert pkg in INSTALL_RECIPES
        assert INSTALL_RECIPES[pkg]["bin"] == pkg


def test_spawn_pyright_prefers_basedpyright(monkeypatch, tmp_path):
    based = str(tmp_path / "basedpyright-langserver")
    Path(based).write_text("x")

    def fake_which(*names):
        for n in names:
            if n == "basedpyright-langserver":
                return based
        return None

    monkeypatch.setattr("agent.lsp.servers._which", fake_which)
    ctx = ServerContext(workspace_root=str(tmp_path), install_strategy="manual")
    spec = _by_id("pyright").build_spawn(str(tmp_path), ctx)
    assert spec is not None
    assert spec.command[0] == based
    assert spec.command[1:] == ["--stdio"]


def test_spawn_pyright_honors_command_override(tmp_path):
    """lsp.servers.pyright.command keeps working even when basedpyright is on PATH."""
    custom = tmp_path / "custom-pyright-langserver"
    custom.write_text("x")
    ctx = ServerContext(
        workspace_root=str(tmp_path),
        install_strategy="manual",
        binary_overrides={"pyright": [str(custom), "--stdio"]},
    )
    spec = _by_id("pyright").build_spawn(str(tmp_path), ctx)
    assert spec.command[0] == str(custom)


def test_typescript_injects_tsserver_path_when_classic_missing(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home

    tls = tmp_path / "typescript-language-server"
    tls.write_text("x")
    tsserver = get_hermes_home() / "lsp" / "node_modules" / "typescript" / "lib" / "tsserver.js"
    tsserver.parent.mkdir(parents=True, exist_ok=True)
    tsserver.write_text("classic")

    monkeypatch.setattr("agent.lsp.servers._tls_has_classic_tsserver", lambda _bin: False)
    ctx = ServerContext(
        workspace_root=str(tmp_path),
        install_strategy="manual",
        binary_overrides={"typescript": [str(tls), "--stdio"]},
        init_overrides={"typescript": {"tsserver": {"logVerbosity": "verbose"}}},
    )
    spec = _by_id("typescript").build_spawn(str(tmp_path), ctx)
    assert spec.command[0] == str(tls)
    assert spec.initialization_options["tsserver"]["path"] == str(tsserver)
    assert spec.initialization_options["tsserver"]["logVerbosity"] == "verbose"


def test_typescript_user_tsserver_path_wins(monkeypatch, tmp_path):
    from hermes_constants import get_hermes_home

    tls = tmp_path / "typescript-language-server"
    tls.write_text("x")
    tsserver = get_hermes_home() / "lsp" / "node_modules" / "typescript" / "lib" / "tsserver.js"
    tsserver.parent.mkdir(parents=True, exist_ok=True)
    tsserver.write_text("classic")
    user_path = "/custom/tsserver.js"

    monkeypatch.setattr("agent.lsp.servers._tls_has_classic_tsserver", lambda _bin: False)
    ctx = ServerContext(
        workspace_root=str(tmp_path),
        install_strategy="manual",
        binary_overrides={"typescript": [str(tls)]},
        init_overrides={"typescript": {"tsserver": {"path": user_path}}},
    )
    spec = _by_id("typescript").build_spawn(str(tmp_path), ctx)
    assert spec.initialization_options["tsserver"]["path"] == user_path


def test_rust_analyzer_seeds_and_waits_longer_than_document_default():
    """First cargo index must outlive the 5s document wait; first empty push is not a verdict."""
    ra = _by_id("rust-analyzer")
    assert ra.seed_first_push is True
    assert ra.wait_timeout is not None
    assert ra.wait_timeout > DIAGNOSTICS_DOCUMENT_WAIT
    assert RUST_ANALYZER_WAIT_TIMEOUT > DIAGNOSTICS_DOCUMENT_WAIT


def test_cmd_which_honors_command_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    custom = tmp_path / "my-gopls"
    custom.write_text("x")
    from agent.lsp import cli as lsp_cli

    monkeypatch.setattr(
        lsp_cli, "_binary_overrides_from_config", lambda: {"gopls": [str(custom)]}
    )
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        rc = lsp_cli._cmd_which("gopls")
    assert rc == 0
    assert buf.getvalue().strip() == str(custom)


def test_status_installed_when_path_binary_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from agent.lsp import cli as lsp_cli

    monkeypatch.setattr(lsp_cli, "_binary_overrides_from_config", lambda: {})

    def fake_which(name):
        if name == "marksman":
            return "/opt/homebrew/bin/marksman"
        return None

    with patch("shutil.which", side_effect=fake_which):
        assert lsp_cli._status_for("marksman") == "installed"
        assert lsp_cli._resolved_binary("marksman") == "/opt/homebrew/bin/marksman"
