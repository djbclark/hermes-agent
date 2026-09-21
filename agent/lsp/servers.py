"""Server registry — per-language LSP server definitions.

Each :class:`ServerDef` matches files (by extension or basename for
extensionless files like ``Dockerfile``), resolves a project root, and
assembles the spawn command.  Auto-installation lives in
:mod:`agent.lsp.install`; nothing here probes binaries until a file in
that language is actually edited.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent.lsp.workspace import nearest_root

logger = logging.getLogger("agent.lsp.servers")

# LSP languageId for ``textDocument/didOpen``, as language → extensions.  A few
# servers (typescript-language-server, vue-language-server) refuse wrong IDs.
_EXTS_BY_LANGUAGE: Dict[str, Sequence[str]] = {
    "python": (".py", ".pyi"),
    "typescript": (".ts", ".mts", ".cts"),
    "typescriptreact": (".tsx",),
    "javascript": (".js", ".mjs", ".cjs"),
    "javascriptreact": (".jsx",),
    "vue": (".vue",), "svelte": (".svelte",), "astro": (".astro",),
    "go": (".go",), "rust": (".rs",),
    "ruby": (".rb", ".rake", ".gemspec", ".ru"),
    "c": (".c", ".h"),
    "cpp": (".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
    "csharp": (".cs", ".csx"), "fsharp": (".fs", ".fsi", ".fsx"),
    "swift": (".swift",), "java": (".java",), "kotlin": (".kt", ".kts"),
    "yaml": (".yaml", ".yml"), "json": (".json",), "jsonc": (".jsonc",),
    "lua": (".lua",), "php": (".php",), "prisma": (".prisma",), "dart": (".dart",),
    "ocaml": (".ml", ".mli"),
    "shellscript": (".sh", ".bash", ".zsh"),
    "terraform": (".tf", ".tfvars"),
    "latex": (".tex",), "bibtex": (".bib",), "gleam": (".gleam",),
    "clojure": (".clj", ".cljc", ".edn"), "clojurescript": (".cljs",),
    "nix": (".nix",), "typst": (".typ", ".typc"), "haskell": (".hs", ".lhs"),
    "julia": (".jl",), "elixir": (".ex", ".exs"), "zig": (".zig", ".zon"),
    "dockerfile": (".dockerfile",),
    "powershell": (".ps1", ".psm1", ".psd1"),
    "markdown": (".md", ".markdown"),
    "toml": (".toml",),
}
LANGUAGE_BY_EXT: Dict[str, str] = {ext: lang for lang, exts in _EXTS_BY_LANGUAGE.items() for ext in exts}

_SpawnFn = Callable[[str, "ServerContext"], Optional["SpawnSpec"]]
_RootFn = Callable[[str, str], Optional[str]]


@dataclass
class SpawnSpec:
    """Result of resolving a server for a file (``None`` means skip)."""
    command: List[str]
    workspace_root: str
    cwd: str
    env: Dict[str, str] = field(default_factory=dict)
    initialization_options: Dict[str, Any] = field(default_factory=dict)
    seed_diagnostics_on_first_push: bool = False


@dataclass
class ServerDef:
    """One language server: ``resolve_root(file, ws)`` → per-server root or ``None`` to skip;
    ``build_spawn(root, ctx)`` → :class:`SpawnSpec` or ``None`` when the binary can't be found."""
    server_id: str
    extensions: Tuple[str, ...]
    resolve_root: _RootFn
    build_spawn: _SpawnFn
    seed_first_push: bool = False
    description: str = ""
    # Server handles ``workspace/didChangeWorkspaceFolders``: one process serves every project root
    # (git worktrees included) as extra workspaceFolders instead of one process per root.
    multi_root: bool = False
    # Extra wait budget (seconds) on top of the service default.  Used as
    # ``max(service wait_timeout, this)`` so a slow first index (rust-analyzer)
    # can publish without raising the global default for every language.
    wait_timeout: Optional[float] = None
    # PATH names ``hermes lsp which/status`` probe after a config command override.
    binaries: Tuple[str, ...] = ()

    def matches(self, file_path: str) -> bool:
        return _file_ext_or_basename(file_path) in self.extensions


@dataclass
class ServerContext:
    """User policy passed into :meth:`ServerDef.build_spawn` (install strategy, overrides)."""
    workspace_root: str
    install_strategy: str = "auto"  # "auto" | "manual" | "off"
    binary_overrides: Dict[str, List[str]] = field(default_factory=dict)
    env_overrides: Dict[str, Dict[str, str]] = field(default_factory=dict)
    init_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)


# ---- helpers ----

def _file_ext_or_basename(path: str) -> str:
    """Lower-cased extension, or the full basename for extensionless files (``Dockerfile``)."""
    base = os.path.basename(path)
    return os.path.splitext(base)[1].lower() or base


def _which(*names: str) -> Optional[str]:
    """Return the full path of the first command found on PATH."""
    return next((p for n in names if (p := shutil.which(n))), None)


def _root_or_workspace(file_path: str, workspace: str, markers: Sequence[str], excludes: Sequence[str] = ()) -> Optional[str]:
    """``nearest_root`` with workspace fallback; ``None`` iff an exclude marker hit."""
    ceiling = os.path.dirname(workspace) if workspace else None
    found = nearest_root(file_path, markers, excludes=excludes, ceiling=ceiling)
    if found is None and excludes and nearest_root(file_path, markers, ceiling=ceiling) is not None:
        # None is ambiguous with excludes configured: a hit without them means
        # the exclude fired (gated off); otherwise fall back to the workspace.
        return None
    return found or workspace


def _markers_root(markers: Optional[Sequence[str]], excludes: Sequence[str] = ()) -> _RootFn:
    """Root resolver over marker files; ``None`` markers means "always the workspace root"."""
    if markers is None:
        return lambda fp, ws: ws
    return lambda fp, ws: _root_or_workspace(fp, ws, markers, excludes=excludes)


def _find_binary(ctx: ServerContext, server_id: str, which: Sequence[str], install_pkg: Optional[str]) -> Optional[str]:
    """Config override → PATH → (optional) auto-install; ``None`` when nothing resolves."""
    override = ctx.binary_overrides.get(server_id)
    bin_path = override[0] if override and override[0] and os.path.exists(override[0]) else _which(*which)
    if bin_path is None and install_pkg is not None:
        from agent.lsp.install import try_install
        bin_path = try_install(install_pkg, ctx.install_strategy)
    return bin_path


def _merge_init(base: Optional[Dict[str, Any]], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Deep-merge ``override`` onto ``base``; nested dicts combine, other values from ``override`` win."""
    if not base:
        return dict(override or {})
    if not override:
        return dict(base)
    out: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _merge_init(out[key], value)
        else:
            out[key] = value
    return out


def _make_spec(root: str, ctx: ServerContext, server_id: str, command: List[str],
               base_init: Optional[Dict[str, Any]] = None, seed: bool = False) -> SpawnSpec:
    init = _merge_init(base_init, ctx.init_overrides.get(server_id) or {})
    return SpawnSpec(command, root, root, env=ctx.env_overrides.get(server_id, {}),
                     initialization_options=init, seed_diagnostics_on_first_push=seed)


def _simple_spawn(server_id: str, which: Sequence[str], args: Sequence[str] = (),
                  install_pkg: Optional[str] = None, base_init: Optional[Dict[str, Any]] = None,
                  seed: bool = False) -> _SpawnFn:
    """Build a spawn function for the common single-binary server shape."""
    def build(root: str, ctx: ServerContext) -> Optional[SpawnSpec]:
        bin_path = _find_binary(ctx, server_id, which, install_pkg)
        return None if bin_path is None else _make_spec(root, ctx, server_id, [bin_path, *args], base_init, seed)
    return build


# ---- bespoke spawn builders ----

# Prefer basedpyright (brew) then classic pyright.  Server id stays ``pyright`` so
# ``lsp.servers.pyright.command`` overrides keep working.
_PYRIGHT_BINS = ("basedpyright-langserver", "basedpyright", "pyright-langserver", "pyright")
_PYRIGHT_CLI_NAMES = {"pyright", "pyright.exe", "basedpyright", "basedpyright.exe"}
_PYRIGHT_LANGSERVER_NAMES = ("basedpyright-langserver", "pyright-langserver")


def _spawn_pyright(root: str, ctx: ServerContext) -> Optional[SpawnSpec]:
    bin_path = _find_binary(ctx, "pyright", _PYRIGHT_BINS, "pyright")
    if bin_path is None:
        return None
    # CLI wrappers (``pyright`` / ``basedpyright``) ship the langserver as a sibling.
    if os.path.basename(bin_path) in _PYRIGHT_CLI_NAMES:
        directory = os.path.dirname(bin_path)
        bin_path = next(
            (os.path.join(directory, name) for name in _PYRIGHT_LANGSERVER_NAMES
             if os.path.exists(os.path.join(directory, name))),
            bin_path,
        )
    # Point pyright at the project venv; its default "python on PATH" rarely is.
    py = _detect_python(root)
    return _make_spec(root, ctx, "pyright", [bin_path, "--stdio"], {"python": {"pythonPath": py}} if py else {})


def _detect_python(root: str) -> Optional[str]:
    venvs = [v for v in (os.environ.get("VIRTUAL_ENV"), os.path.join(root, ".venv"), os.path.join(root, "venv")) if v]
    paths = (os.path.join(v, sub) for v in venvs for sub in ("bin/python", "bin/python3", "Scripts/python.exe"))
    return next((p for p in paths if os.path.exists(p)), None)


_warned_once: set = set()


def _warn_once(key: str, message: str) -> None:
    """Log ``message`` at WARNING the first time ``key`` is seen in this process."""
    if key not in _warned_once:
        _warned_once.add(key)
        logger.warning(message)


def _spawn_bash_ls(root: str, ctx: ServerContext) -> Optional[SpawnSpec]:
    bin_path = _find_binary(ctx, "bash-language-server", ("bash-language-server",), "bash-language-server")
    if bin_path is None:
        return None
    # bash-language-server delegates diagnostics to shellcheck; without it the
    # server runs but never reports anything.  Warn once so the gap is visible.
    if _which("shellcheck") is None:
        _warn_once("shellcheck", "bash-language-server: shellcheck not found on PATH — diagnostics will be empty "
                   "until shellcheck is installed (apt: shellcheck, brew: shellcheck, scoop: shellcheck).")
    return _make_spec(root, ctx, "bash-language-server", [bin_path, "start"])


def _find_pses_bundle(ctx: ServerContext) -> Optional[str]:
    """Locate the PowerShellEditorServices bundle dir (release zip, manual install).  Resolution order:
    ``lsp.servers.powershell.command[0]`` when a directory, ``init_overrides["powershell"]["bundlePath"]``,
    ``PSES_BUNDLE_PATH`` env, then ``<HERMES_HOME>/lsp/PowerShellEditorServices``."""
    from hermes_constants import get_hermes_home
    override = ctx.binary_overrides.get("powershell")
    init = ctx.init_overrides.get("powershell", {})
    candidates = [
        override[0] if override else None,
        str(init["bundlePath"]) if isinstance(init, dict) and init.get("bundlePath") else None,
        os.environ.get("PSES_BUNDLE_PATH"),
        os.path.join(str(get_hermes_home()), "lsp", "PowerShellEditorServices"),
    ]
    for cand in filter(None, candidates):
        # Accept either the bundle root or the inner module dir.
        if os.path.isfile(os.path.join(cand, "PowerShellEditorServices", "Start-EditorServices.ps1")):
            return cand
        if os.path.isfile(os.path.join(cand, "Start-EditorServices.ps1")):
            return os.path.dirname(cand)
    return None


_PSES_MISSING_MSG = (
    "powershell: pwsh found but the PowerShellEditorServices bundle is missing. Download the release zip from "
    "https://github.com/PowerShell/PowerShellEditorServices/releases, extract it, and either set "
    "lsp.servers.powershell.command to the bundle path or unzip it to <HERMES_HOME>/lsp/PowerShellEditorServices."
)


def _spawn_powershell_es(root: str, ctx: ServerContext) -> Optional[SpawnSpec]:
    """Spawn PowerShellEditorServices: needs a ``pwsh``/``powershell`` host plus the module bundle."""
    pwsh = _which("pwsh", "powershell")
    if pwsh is None:
        return None
    bundle = _find_pses_bundle(ctx)
    if bundle is None:
        _warn_once("pses-bundle", _PSES_MISSING_MSG)
        return None
    start_script = os.path.join(bundle, "PowerShellEditorServices", "Start-EditorServices.ps1")
    # PSES writes connection info to the session details file on startup.
    session_dir = hermes_lsp_session_dir()
    inner = (
        f"& '{start_script}' -BundledModulesPath '{bundle}' "
        f"-LogPath '{os.path.join(session_dir, 'pses.log')}' "
        f"-SessionDetailsPath '{os.path.join(session_dir, f'pses-session-{os.getpid()}.json')}' "
        f"-FeatureFlags @() -AdditionalModules @() "
        f"-HostName Hermes -HostProfileId hermes -HostVersion 1.0.0 -Stdio -LogLevel Normal"
    )
    return SpawnSpec(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", inner],
        root, root, env=ctx.env_overrides.get("powershell", {}),
        initialization_options={k: v for k, v in ctx.init_overrides.get("powershell", {}).items() if k != "bundlePath"},
    )


_TSSERVER_REL = os.path.join("typescript", "lib", "tsserver.js")


def _is_classic_tsserver(path: str) -> bool:
    """True iff ``path`` is a classic tsserver.js (TypeScript 7 dropped this file)."""
    return os.path.isfile(path) and os.path.basename(path) == "tsserver.js"


def _tsserver_js_near(start: str, *, hops: int = 8) -> Optional[str]:
    """Walk parents of ``start`` looking for ``node_modules/typescript/lib/tsserver.js``."""
    d = os.path.abspath(start)
    for _ in range(hops):
        for rel in (
            os.path.join("node_modules", _TSSERVER_REL),
            _TSSERVER_REL,
            os.path.join("lib", "tsserver.js"),
        ):
            cand = os.path.join(d, rel)
            if _is_classic_tsserver(cand):
                return cand
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _tls_has_classic_tsserver(tls_bin: str) -> bool:
    """True when typescript-language-server can find a classic tsserver next to itself."""
    return _tsserver_js_near(os.path.dirname(os.path.realpath(tls_bin)), hops=6) is not None


def _discover_tsserver_js(tls_bin: str) -> Optional[str]:
    """Locate a classic tsserver.js: Hermes lsp node_modules, TLS install tree, npm prefix."""
    from hermes_constants import get_hermes_home
    hermes_ts = os.path.join(str(get_hermes_home()), "lsp", "node_modules", _TSSERVER_REL)
    if _is_classic_tsserver(hermes_ts):
        return hermes_ts
    near_tls = _tsserver_js_near(os.path.dirname(os.path.realpath(tls_bin)))
    if near_tls:
        return near_tls
    npm = _which("npm")
    if npm:
        prefix = os.path.dirname(os.path.dirname(os.path.realpath(npm)))
        cand = os.path.join(prefix, "lib", "node_modules", _TSSERVER_REL)
        if _is_classic_tsserver(cand):
            return cand
    return None


def _spawn_typescript(root: str, ctx: ServerContext) -> Optional[SpawnSpec]:
    """PATH typescript-language-server; inject tsserver.path when TS 7 would fail initialize."""
    bin_path = _find_binary(
        ctx, "typescript", ("typescript-language-server",), "typescript-language-server",
    )
    if bin_path is None:
        return None
    base_init: Dict[str, Any] = {}
    if not _tls_has_classic_tsserver(bin_path):
        tsserver = _discover_tsserver_js(bin_path)
        if tsserver:
            base_init = {"tsserver": {"path": tsserver}}
    return _make_spec(root, ctx, "typescript", [bin_path, "--stdio"], base_init, seed=True)


def hermes_lsp_session_dir() -> str:
    """Return (and create) the dir for PSES session/log scratch files."""
    from hermes_constants import get_hermes_home
    d = os.path.join(str(get_hermes_home()), "lsp", "pses")
    os.makedirs(d, exist_ok=True)
    return d


# ---- the registry ----

_JS_MARKERS = ["package-lock.json", "bun.lockb", "bun.lock", "pnpm-lock.yaml", "yarn.lock", "package.json", "tsconfig.json"]
_DENO_EXCLUDES = ["deno.json", "deno.jsonc"]
_root_typescript = _markers_root(_JS_MARKERS, _DENO_EXCLUDES)


def _server(server_id: str, extensions: Tuple[str, ...], description: str, *,
            markers: Optional[Sequence[str]] = None, excludes: Sequence[str] = (),
            resolve_root: Optional[_RootFn] = None, build_spawn: Optional[_SpawnFn] = None,
            which: Sequence[str] = (), args: Sequence[str] = (), install_pkg: Optional[str] = None,
            base_init: Optional[Dict[str, Any]] = None, seed: bool = False,
            multi_root: bool = False, wait_timeout: Optional[float] = None,
            binaries: Sequence[str] = ()) -> ServerDef:
    """Registry entry factory: defaults to marker-based root + single-binary spawn."""
    bins = tuple(binaries or which or (server_id,))
    return ServerDef(
        server_id, extensions,
        resolve_root or _markers_root(markers, excludes),
        build_spawn or _simple_spawn(server_id, which or (server_id,), args, install_pkg, base_init, seed),
        seed_first_push=seed, description=description, multi_root=multi_root,
        wait_timeout=wait_timeout, binaries=bins,
    )


_PY_MARKERS = ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile", "pyrightconfig.json"]
# First cargo check on a cold rust-analyzer is often > the 5s document default.
RUST_ANALYZER_WAIT_TIMEOUT = 20.0

SERVERS: List[ServerDef] = [
    _server("pyright", (".py", ".pyi"), "Python — basedpyright / pyright",
            markers=_PY_MARKERS, build_spawn=_spawn_pyright, multi_root=True, binaries=_PYRIGHT_BINS),
    # After pyright so find_server_for_file still returns pyright; the manager
    # collects diagnostics from every match (ruff + pyright).
    _server("ruff", (".py", ".pyi"), "Python linter — ruff server",
            markers=_PY_MARKERS, which=("ruff",), args=("server",)),
    _server("typescript", (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"),
            "JavaScript/TypeScript — typescript-language-server", resolve_root=_root_typescript,
            which=("typescript-language-server",), build_spawn=_spawn_typescript, seed=True),
    _server("vue-language-server", (".vue",), "Vue.js — @vue/language-server", resolve_root=_root_typescript,
            args=("--stdio",), install_pkg="@vue/language-server"),
    _server("svelte-language-server", (".svelte",), "Svelte — svelte-language-server", resolve_root=_root_typescript,
            which=("svelteserver", "svelte-language-server"), args=("--stdio",), install_pkg="svelte-language-server"),
    _server("astro-language-server", (".astro",), "Astro — @astrojs/language-server", resolve_root=_root_typescript,
            which=("astro-ls", "astro-language-server"), args=("--stdio",), install_pkg="@astrojs/language-server"),
    _server("gopls", (".go",), "Go — gopls", markers=["go.work", "go.mod", "go.sum"], install_pkg="gopls"),
    _server("rust-analyzer", (".rs",), "Rust — rust-analyzer", markers=["Cargo.toml", "Cargo.lock"],
            install_pkg="rust-analyzer", seed=True, wait_timeout=RUST_ANALYZER_WAIT_TIMEOUT),
    _server("clangd", (".c", ".cpp", ".cc", ".cxx", ".h", ".hh", ".hpp", ".hxx"), "C/C++ — clangd",
            markers=["compile_commands.json", "compile_flags.txt", ".clangd"],
            args=("--background-index", "--clang-tidy"), install_pkg="clangd"),
    _server("bash-language-server", (".sh", ".bash", ".zsh", ".ksh"), "Bash — bash-language-server", build_spawn=_spawn_bash_ls),
    _server("yaml-language-server", (".yaml", ".yml"), "YAML — yaml-language-server",
            args=("--stdio",), install_pkg="yaml-language-server"),
    _server("lua-language-server", (".lua",), "Lua — lua-language-server",
            markers=[".luarc.json", ".luarc.jsonc", ".luacheckrc", ".stylua.toml", "stylua.toml", "selene.toml", "selene.yml"],
            install_pkg="lua-language-server"),
    _server("intelephense", (".php",), "PHP — intelephense", markers=["composer.json", "composer.lock", ".php-version"],
            args=("--stdio",), install_pkg="intelephense", base_init={"telemetry": {"enabled": False}}),
    _server("ocaml-lsp", (".ml", ".mli"), "OCaml — ocaml-lsp", markers=["dune-project", "dune-workspace", ".merlin", "opam"],
            which=("ocamllsp",)),
    _server("dockerfile-ls", (".dockerfile", "Dockerfile"), "Dockerfile — dockerfile-language-server-nodejs",
            which=("docker-langserver",), args=("--stdio",), install_pkg="dockerfile-language-server-nodejs"),
    # terraform-ls is heavy to auto-install; require the user to provide it.
    _server("terraform-ls", (".tf", ".tfvars"), "Terraform — terraform-ls", markers=[".terraform.lock.hcl", "terraform.tfstate"],
            args=("serve",), base_init={"experimentalFeatures": {"prefillRequiredFields": True, "validateOnSave": True}}),
    _server("dart", (".dart",), "Dart — built-in language server", markers=["pubspec.yaml", "analysis_options.yaml"],
            args=("language-server", "--lsp")),
    _server("haskell-language-server", (".hs", ".lhs"), "Haskell — haskell-language-server",
            markers=["stack.yaml", "cabal.project", "hie.yaml"],
            which=("haskell-language-server-wrapper", "haskell-language-server"), args=("--lsp",)),
    _server("julia", (".jl",), "Julia — LanguageServer.jl", markers=["Project.toml", "Manifest.toml"],
            args=("--startup-file=no", "--history-file=no", "-e", "using LanguageServer; runserver()")),
    _server("clojure-lsp", (".clj", ".cljs", ".cljc", ".edn"), "Clojure — clojure-lsp",
            markers=["deps.edn", "project.clj", "shadow-cljs.edn", "bb.edn", "build.boot"], args=("listen",)),
    _server("nixd", (".nix",), "Nix — nixd", resolve_root=lambda fp, ws: nearest_root(fp, ["flake.nix"]) or ws),
    _server("zls", (".zig", ".zon"), "Zig — zls", markers=["build.zig"]),
    _server("gleam", (".gleam",), "Gleam — built-in language server", markers=["gleam.toml"], args=("lsp",)),
    _server("elixir-ls", (".ex", ".exs"), "Elixir — elixir-ls", markers=["mix.exs", "mix.lock"],
            which=("elixir-ls", "language_server.sh")),
    _server("prisma", (".prisma",), "Prisma — built-in language server", markers=["schema.prisma", "prisma/schema.prisma"],
            args=("language-server",)),
    _server("kotlin-language-server", (".kt", ".kts"), "Kotlin — kotlin-language-server",
            markers=["settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts", "pom.xml"]),
    # jdtls has a complex install flow; we look for the wrapper script a manual install produces.
    _server("jdtls", (".java",), "Java — Eclipse JDT Language Server",
            markers=["pom.xml", "build.gradle", "build.gradle.kts", ".project", ".classpath", "settings.gradle"]),
    # No universal PowerShell root marker; nearest_root is exact-name only (no globs).
    _server("powershell", (".ps1", ".psm1", ".psd1"), "PowerShell — PowerShellEditorServices (manual bundle)",
            markers=["PSScriptAnalyzerSettings.psd1"], build_spawn=_spawn_powershell_es, binaries=("pwsh", "powershell")),
    _server("marksman", (".md", ".markdown"), "Markdown — marksman", which=("marksman",), args=("server",)),
    _server("taplo", (".toml",), "TOML — taplo", which=("taplo",), args=("lsp", "stdio")),
]


def find_servers_for_file(file_path: str) -> List[ServerDef]:
    """Return every registry entry that handles ``file_path`` (possibly several, e.g. pyright + ruff)."""
    return [srv for srv in SERVERS if srv.matches(file_path)]


def find_server_for_file(file_path: str) -> Optional[ServerDef]:
    """Return the first registry entry that handles ``file_path``, or None."""
    matched = find_servers_for_file(file_path)
    return matched[0] if matched else None


def language_id_for(path: str) -> str:
    """Return the LSP languageId to send in didOpen for ``path``."""
    return LANGUAGE_BY_EXT.get(_file_ext_or_basename(path), "plaintext")


__all__ = [
    "ServerDef", "ServerContext", "SpawnSpec", "SERVERS",
    "find_server_for_file", "find_servers_for_file", "language_id_for", "LANGUAGE_BY_EXT",
    "RUST_ANALYZER_WAIT_TIMEOUT",
]
