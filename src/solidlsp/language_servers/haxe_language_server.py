"""Haxe language server integration using vshaxe/haxe-language-server."""

import glob
import hashlib
import logging
import os
import pathlib
import shutil
import tempfile
import threading
import time
import urllib.request
import zipfile
from functools import cached_property

from overrides import override

from solidlsp.ls import (
    LanguageServerDependencyProvider,
    LanguageServerDependencyProviderSinglePath,
    LSPFileBuffer,
    SolidLanguageServer,
)
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_types import Hover
from solidlsp.lsp_protocol_handler.lsp_types import DiagnosticSeverity, InitializeParams
from solidlsp.settings import SolidLSPSettings

log = logging.getLogger(__name__)

# Version pinning convention (see eclipse_jdtls.py for the full spec):
#   INITIAL_* — frozen forever; legacy unversioned install dir is reserved for it.
#   DEFAULT_* — bumped on upgrades; goes into a versioned subdir.
INITIAL_VSHAXE_VERSION = "2.34.2"
INITIAL_VSHAXE_SHA256 = "104d785e3f7b57a7f3debf520d9751f7e7abf3a7e78d203db1a8ff3dc7ca30e2"
DEFAULT_VSHAXE_VERSION = "2.34.2"
DEFAULT_VSHAXE_SHA256 = "104d785e3f7b57a7f3debf520d9751f7e7abf3a7e78d203db1a8ff3dc7ca30e2"


def _vshaxe_sha(version: str) -> str | None:
    if version == INITIAL_VSHAXE_VERSION:
        return INITIAL_VSHAXE_SHA256
    if version == DEFAULT_VSHAXE_VERSION:
        return DEFAULT_VSHAXE_SHA256
    return None


# Dependency / build-output directory names skipped when walking a Haxe project tree for source or
# build files. ``_discover_hxml_file`` additionally skips ``build`` (it holds compiled-output .hxml).
_DEPENDENCY_DIRS = frozenset({"node_modules", "haxe_libraries", ".haxelib", "export", "dump", "bin", ".git"})


class HaxeLanguageServer(SolidLanguageServer):
    """Haxe language server integration using vshaxe/haxe-language-server.

    Requires Haxe compiler (3.4.0+) and Node.js.
    """

    # Startup-through-idle budget (seconds). Overridable via ls_specific_settings.haxe.compilationTimeout.
    # The default covers the measured one-time cold cost on a large repo (initial compile + classpath
    # parse + refactoring cache + the warm-up recompile ~= 180s) with margin.
    _COMPILATION_TIMEOUT_DEFAULT = 240.0
    # How long the server must stay idle (no active progress tokens) before we declare it ready.
    # The Haxe LS starts its classpath parse / refactoring cache *after* the first idle, so we must
    # not declare ready at the first idle moment.
    _IDLE_SETTLE_WINDOW = 1.5
    # LSP MessageType.Error (the ``type`` field of window/logMessage params).
    _LSP_MESSAGE_TYPE_ERROR = 1

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        """Creates a HaxeLanguageServer instance. Use LanguageServer.create() instead."""
        super().__init__(
            config,
            repository_root_path,
            None,
            "haxe",
            solidlsp_settings,
        )

        self._server_ready = threading.Event()
        self._server_ready.set()
        self._active_progress_tokens: set[str] = set()
        self._progress_lock = threading.Lock()
        self._last_compiler_message: str | None = None
        self._compilation_timeout = self._resolve_compilation_timeout()

    @classmethod
    def supports_implementation_request(cls) -> bool:
        # The vshaxe/haxe-language-server resolves textDocument/implementation for
        # interface->implementor and base-class->subclass (verified empirically), even
        # though it does not advertise `implementationProvider` in its server capabilities.
        return True

    @override
    def _create_dependency_provider(self) -> LanguageServerDependencyProvider:
        return self.DependencyProvider(self._custom_settings, self._ls_resources_dir)

    class DependencyProvider(LanguageServerDependencyProviderSinglePath):
        # Downloaded from Open VSX (not the VS Code Marketplace) because Open VSX
        # provides stable versioned URLs and SHA256 checksums for integrity verification.

        @override
        def _get_or_install_core_dependency(self) -> str:
            """Find the Haxe Language Server binary."""
            # 1. Check for haxe-language-server in PATH
            system_haxe_ls = shutil.which("haxe-language-server")
            if system_haxe_ls:
                log.info(f"Found system-installed haxe-language-server at {system_haxe_ls}")
                return system_haxe_ls

            # 2. Check VSCode extension locations
            vscode_server_path = self._find_vscode_extension_server()
            if vscode_server_path:
                log.info(f"Found Haxe Language Server in VSCode extension at {vscode_server_path}")
                return vscode_server_path

            # 3. Check resource dir / download from Open VSX
            version = self._custom_settings.get("version", DEFAULT_VSHAXE_VERSION)
            ls_dirname = "haxe-lsp" if version == INITIAL_VSHAXE_VERSION else f"haxe-lsp-{version}"
            haxe_ls_dir = os.path.join(self._ls_resources_dir, ls_dirname)
            server_js_path = os.path.join(haxe_ls_dir, "bin", "server.js")
            if os.path.exists(server_js_path):
                log.info(f"Found Haxe Language Server at {server_js_path}")
                return server_js_path

            if shutil.which("node") is None:
                raise FileNotFoundError(
                    "Haxe Language Server not found and Node.js is not installed (required to run it).\n"
                    "Install options:\n"
                    "  1. Install Node.js and re-run (auto-download will proceed)\n"
                    "  2. Install the vshaxe VSCode extension: code --install-extension nadako.vshaxe\n"
                    "  3. Set ls_path in serena_config.yml under ls_specific_settings.haxe"
                )

            downloaded_path = self._download_from_open_vsx(haxe_ls_dir, version)
            if downloaded_path:
                return downloaded_path

            raise FileNotFoundError(
                "Haxe Language Server not found. Install options:\n"
                "  1. Install the vshaxe VSCode extension: code --install-extension nadako.vshaxe\n"
                "  2. Set ls_path in serena_config.yml under ls_specific_settings.haxe"
            )

        @staticmethod
        def _find_vscode_extension_server() -> str | None:
            """Search for the Haxe language server in VSCode extension directories."""
            search_paths = [
                os.path.expanduser("~/.vscode/extensions/nadako.vshaxe-*/bin/server.js"),
                os.path.expanduser("~/.vscode-server/extensions/nadako.vshaxe-*/bin/server.js"),
                os.path.expanduser("~/.vscode-insiders/extensions/nadako.vshaxe-*/bin/server.js"),
            ]
            for pattern in search_paths:
                matches = sorted(glob.glob(pattern), reverse=True)  # newest version first
                for match in matches:
                    if os.path.isfile(match):
                        return match
            return None

        @classmethod
        def _download_from_open_vsx(cls, target_dir: str, version: str) -> str | None:
            """Download a vshaxe VSIX from Open VSX and extract server.js.
            Verifies the download against a hardcoded SHA256 checksum when using the default version.
            """
            try:
                download_url = f"https://open-vsx.org/api/nadako/vshaxe/{version}/file/nadako.vshaxe-{version}.vsix"
                log.info("Downloading Haxe Language Server v%s from Open VSX...", version)
                vsix_path = os.path.join(tempfile.gettempdir(), "vshaxe.vsix")
                urllib.request.urlretrieve(download_url, vsix_path)

                # Verify SHA256 checksum only when the resolved version is one of our pinned ones (INITIAL or current DEFAULT)
                expected_sha = _vshaxe_sha(version)
                if expected_sha is not None:
                    sha256 = hashlib.sha256()
                    with open(vsix_path, "rb") as f:
                        for chunk in iter(lambda: f.read(8192), b""):
                            sha256.update(chunk)
                    if sha256.hexdigest().lower() != expected_sha:
                        os.remove(vsix_path)
                        raise RuntimeError(
                            f"SHA256 checksum mismatch for vshaxe VSIX. Expected {expected_sha}, "
                            f"got {sha256.hexdigest()}. The file may be corrupted or tampered with."
                        )
                    log.info("SHA256 checksum verified")
                else:
                    log.info("Using custom version %s — skipping SHA256 verification", version)

                # VSIX files are ZIP archives — extract bin/ contents
                bin_dir = os.path.join(target_dir, "bin")
                os.makedirs(bin_dir, exist_ok=True)
                with zipfile.ZipFile(vsix_path, "r") as zf:
                    for entry in zf.namelist():
                        if "/bin/" in entry:
                            filename = entry.split("/bin/", 1)[-1]
                            if filename and ".." not in filename:
                                dest_path = os.path.join(bin_dir, filename)
                                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                                with zf.open(entry) as src, open(dest_path, "wb") as dst:
                                    dst.write(src.read())

                os.remove(vsix_path)

                server_js_path = os.path.join(bin_dir, "server.js")
                if os.path.exists(server_js_path):
                    log.info(f"Haxe Language Server v{version} installed to {server_js_path}")
                    return server_js_path

                log.error("Downloaded VSIX but server.js not found after extraction")
                return None

            except Exception:
                log.exception("Failed to download Haxe Language Server from Open VSX")
                return None

        @override
        def _create_launch_command(self, core_path: str) -> list[str]:
            if core_path.endswith(".js"):
                return ["node", core_path]
            return [core_path, "--stdio"]

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        return super().is_ignored_dirname(dirname) or dirname in [
            "node_modules",
            "export",
            "dump",
        ]

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        """Return initialize params for the Haxe Language Server.

        displayArguments are resolved from user-configured buildFile or auto-discovered .hxml files.
        """
        root_uri = pathlib.Path(repository_absolute_path).as_uri()

        # 1. Check for user-configured .hxml path
        configured_build_file = self._custom_settings.get("buildFile")
        if configured_build_file:
            log.info(f"Using user-configured Haxe build file: {configured_build_file}")
            display_arguments = [configured_build_file]
        else:
            # 2. Auto-discover the .hxml build file (cached, so the warm-up file picker reuses it)
            build_file = self._build_file_rel
            display_arguments = [build_file] if build_file else []

        # Optionally make the Haxe compiler treat a sub-directory as its working directory, so a
        # sub-project .hxml with CWD-relative includes/classpaths resolves correctly even when Serena
        # is rooted at a monorepo root. Haxe processes --cwd first, so it must precede the build file;
        # the build file is then interpreted relative to projectRoot.
        project_root = self._custom_settings.get("projectRoot")
        if project_root:
            abs_project_root = project_root if os.path.isabs(project_root) else os.path.join(repository_absolute_path, project_root)
            abs_project_root = os.path.abspath(abs_project_root)
            log.info("Using Haxe compiler working directory (--cwd): %s", abs_project_root)
            display_arguments = ["--cwd", abs_project_root, *display_arguments]

        init_options: dict = {"displayArguments": display_arguments}
        rename_source_folders = self._custom_settings.get("renameSourceFolders")
        if rename_source_folders:
            init_options["renameSourceFolders"] = rename_source_folders

        haxe_path = self._custom_settings.get("haxePath")
        if haxe_path:
            init_options["haxePath"] = haxe_path

        initialize_params = {
            "locale": "en",
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {
                        "relatedInformation": True,
                        "versionSupport": False,
                        "tagSupport": {"valueSet": [1, 2]},
                        "codeDescriptionSupport": True,
                        "dataSupport": True,
                    },
                    "synchronization": {"dynamicRegistration": True, "didSave": True},
                    "completion": {"completionItem": {"snippetSupport": True}},
                    "definition": {},
                    "implementation": {},
                    "references": {},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "codeAction": {},
                    "rename": {},
                    "signatureHelp": {},
                },
                "workspace": {
                    "workspaceFolders": True,
                    "didChangeConfiguration": {},
                    "symbol": {},
                },
            },
            "initializationOptions": init_options,
            "processId": os.getpid(),
            "rootPath": repository_absolute_path,
            "rootUri": root_uri,
            "workspaceFolders": [
                {
                    "uri": root_uri,
                    "name": os.path.basename(repository_absolute_path),
                }
            ],
        }
        return initialize_params  # type: ignore[return-value]

    @staticmethod
    def _discover_hxml_file(repository_absolute_path: str) -> list[str]:
        """Auto-discover a single .hxml build file (as a one-element list), filtering out dependency
        directories; returns ``[]`` if none are found.

        When several candidates exist the choice is **non-silent and deterministic**: a warning lists
        every candidate and the shallowest path (alphabetical tie-break) is used. For full control,
        set ``ls_specific_settings.haxe.buildFile`` in ``serena_config.yml``.
        """
        max_depth = 5
        skip_dirs = _DEPENDENCY_DIRS | {"build"}

        candidates: list[str] = []
        for root, dirs, files in os.walk(repository_absolute_path):
            # Skip dependency/build output directories
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            depth = len(pathlib.Path(root).relative_to(repository_absolute_path).parts)
            if depth > max_depth:
                dirs.clear()
                continue
            for f in files:
                if f.endswith(".hxml") and "haxe_libraries" not in root:
                    candidates.append(os.path.relpath(os.path.join(root, f), repository_absolute_path))

        if not candidates:
            log.info("No .hxml file found in project")
            return []

        # Deterministic order: shallowest path first, then alphabetical — never os.walk order.
        candidates.sort(key=lambda rel: (len(pathlib.Path(rel).parts), rel))
        chosen = candidates[0]

        if len(candidates) == 1:
            log.info(
                "Auto-discovered Haxe build file: %s. To use a different file, set "
                "ls_specific_settings.haxe.buildFile in serena_config.yml.",
                chosen,
            )
        else:
            log.warning(
                "Multiple Haxe build files found (%d): %s. Using %s (shallowest path, then alphabetical). "
                "To choose explicitly, set ls_specific_settings.haxe.buildFile in serena_config.yml.",
                len(candidates),
                ", ".join(candidates),
                chosen,
            )
        return [chosen]

    def _resolve_compilation_timeout(self) -> float:
        """Startup-through-idle budget in seconds, from ls_specific_settings.haxe.compilationTimeout.

        Falls back to ``_COMPILATION_TIMEOUT_DEFAULT`` for a missing or non-numeric value.
        """
        raw = self._custom_settings.get("compilationTimeout")
        if raw is None:
            return self._COMPILATION_TIMEOUT_DEFAULT
        try:
            return float(raw)
        except (TypeError, ValueError):
            log.warning(
                "Invalid ls_specific_settings.haxe.compilationTimeout=%r (expected seconds); using default %.0fs",
                raw,
                self._COMPILATION_TIMEOUT_DEFAULT,
            )
            return self._COMPILATION_TIMEOUT_DEFAULT

    def _handle_window_log_message(self, msg: dict) -> None:
        """Log every window/logMessage and remember the latest error-severity one.

        The Haxe LS reports build errors this way; capturing the most recent one lets us attach the
        likely cause to a subsequent failure or timeout (see ``describe_busy_state``).
        """
        log.info(f"LSP: window/logMessage: {msg}")
        if msg.get("type") == self._LSP_MESSAGE_TYPE_ERROR:
            text = msg.get("message")
            if text:
                self._last_compiler_message = str(text)

    def _handle_cache_build_failed(self, params: dict) -> None:
        """Handle the haxe/cacheBuildFailed notification (previously logged as 'Unhandled method')."""
        log.warning("Haxe LSP: cache build failed (haxe/cacheBuildFailed): %s", params)
        self._last_compiler_message = (
            "haxe/cacheBuildFailed: the compiler could not build its cache "
            "(check the build file, classpaths, and that the project compiles)"
        )

    def _handle_haxe_keeps_crashing(self, params: dict) -> None:
        """Handle the haxe/haxeKeepsCrashing notification (previously logged as 'Unhandled method')."""
        log.warning("Haxe LSP: the Haxe compiler keeps crashing (haxe/haxeKeepsCrashing): %s", params)
        self._last_compiler_message = "haxe/haxeKeepsCrashing: the Haxe compiler is repeatedly crashing"

    @override
    def describe_busy_state(self) -> str:
        bits: list[str] = []
        with self._progress_lock:
            n_tokens = len(self._active_progress_tokens)
        if n_tokens:
            bits.append(f"Haxe still compiling: {n_tokens} progress token(s) active")
        if self._last_compiler_message:
            bits.append(f"last compiler message: {self._last_compiler_message}")
        return "; ".join(bits)

    def _handle_diagnostics(self, params: dict) -> None:
        """Signal idle when diagnostics arrive, unless progress tokens are still active (race guard)."""
        uri = params.get("uri", "unknown")
        diags = params.get("diagnostics", [])
        errors = [d for d in diags if d.get("severity") == DiagnosticSeverity.Error]
        if errors:
            log.warning("Haxe LSP diagnostics for %s: %d errors: %s", uri, len(errors), errors)
        else:
            log.info("Haxe LSP diagnostics for %s: clean (%d total)", uri, len(diags))

        with self._progress_lock:
            if not self._active_progress_tokens:
                log.info("Haxe LSP: no active progress tokens — signalling idle")
                self._server_ready.set()
            else:
                log.info(
                    "Haxe LSP: diagnostics received but %d progress tokens still active — deferring",
                    len(self._active_progress_tokens),
                )

    def _handle_work_done_progress_create(self, params: dict) -> dict:
        """Handle window/workDoneProgress/create — mark the server busy until the token finishes."""
        token = str(params.get("token", ""))
        log.debug(f"Haxe LSP workDoneProgress/create: token={token!r}")
        with self._progress_lock:
            self._active_progress_tokens.add(token)
            self._server_ready.clear()
        return {}

    def _handle_progress(self, params: dict) -> None:
        """Track $/progress begin/end to detect when all async compilation work is idle."""
        token = str(params.get("token", ""))
        value = params.get("value", {})
        kind = value.get("kind")
        if kind == "begin":
            title = value.get("title", "")
            log.info(f"Haxe LSP progress [{token}]: started - {title}")
            with self._progress_lock:
                self._active_progress_tokens.add(token)
                self._server_ready.clear()
        elif kind == "report":
            pct = value.get("percentage")
            msg = value.get("message", "")
            pct_str = f" ({pct}%)" if pct is not None else ""
            log.debug(f"Haxe LSP progress [{token}]: {msg}{pct_str}")
        elif kind == "end":
            msg = value.get("message", "")
            log.info(f"Haxe LSP progress [{token}]: ended - {msg}")
            with self._progress_lock:
                self._active_progress_tokens.discard(token)
                if not self._active_progress_tokens:
                    self._server_ready.set()

    def _await_stable_idle(self, timeout: float, settle_window: float, poll_interval: float = 0.05) -> bool:
        """Block until the server has been continuously idle (no active progress tokens) for
        ``settle_window`` seconds, or until ``timeout`` seconds elapse.

        The Haxe LS goes briefly idle after its first ``Building Cache`` run and only then starts the
        classpath parse / refactoring cache. Waiting for *stable* idle (rather than the first idle)
        ensures those post-idle tokens are accounted for before we declare the server ready.

        :return: True if stable idle was reached; False if the timeout elapsed first.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if not self._server_ready.wait(timeout=remaining):
                return False
            # An idle moment occurred; confirm it persists for the full settle window.
            if not self._progress_resumed_within(settle_window, deadline, poll_interval):
                return True
            # A new progress token appeared within the window — keep waiting for idle again.

    def _progress_resumed_within(self, window: float, deadline: float, poll_interval: float) -> bool:
        """Poll for up to ``window`` seconds (never past ``deadline``) for the server to become busy
        again (a new progress token clearing ``_server_ready``). Returns True if it did.
        """
        end = min(time.monotonic() + window, deadline)
        while time.monotonic() < end:
            if not self._server_ready.is_set():
                return True
            time.sleep(poll_interval)
        return False

    @cached_property
    def _build_file_rel(self) -> str | None:
        """The repo-relative build file: the configured one, else the first auto-discovered .hxml.

        Cached: auto-discovery walks the project tree, and this is read both when building the
        compiler's displayArguments and again when picking the warm-up file.
        """
        configured = self._custom_settings.get("buildFile")
        if configured:
            return configured
        discovered = self._discover_hxml_file(self.repository_root_path)
        return discovered[0] if discovered else None

    @staticmethod
    def _parse_hxml(build_file_abs: str) -> tuple[str | None, list[str]]:
        """Best-effort parse of a top-level .hxml for its ``-main`` module and ``-cp`` class paths.

        Does not follow .hxml includes; callers fall back to other strategies when ``-main`` is absent.
        """
        tokens: list[str] = []
        try:
            for raw in pathlib.Path(build_file_abs).read_text(encoding="utf-8").splitlines():
                line = raw.split("#", 1)[0].strip()
                if line:
                    tokens.extend(line.split())
        except OSError:
            return None, []

        main: str | None = None
        class_paths: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in ("-main", "--main", "-m") and i + 1 < len(tokens):
                main = tokens[i + 1]
                i += 2
            elif token in ("-cp", "-p", "--class-path") and i + 1 < len(tokens):
                class_paths.append(tokens[i + 1])
                i += 2
            else:
                i += 1
        return main, class_paths

    def _main_module_file(self) -> str | None:
        """Repo-relative path of the build's ``-main`` module, resolved against its ``-cp`` paths."""
        build_file = self._build_file_rel
        if not build_file:
            return None
        main, class_paths = self._parse_hxml(os.path.join(self.repository_root_path, build_file))
        if not main:
            return None
        module_rel = main.replace(".", os.sep) + ".hx"
        build_dir = os.path.dirname(build_file)
        bases = [self.repository_root_path, os.path.join(self.repository_root_path, build_dir)]
        for class_path in class_paths or ["."]:
            for base in bases:
                candidate = os.path.normpath(os.path.join(base, class_path, module_rel))
                if os.path.isfile(candidate):
                    return os.path.relpath(candidate, self.repository_root_path)
        return None

    def _first_hx_file(self) -> str | None:
        """Repo-relative path of the first ``.hx`` file under the repository (deterministic order)."""
        for root, dirs, files in os.walk(self.repository_root_path):
            dirs[:] = sorted(d for d in dirs if d not in _DEPENDENCY_DIRS)
            for f in sorted(files):
                if f.endswith(".hx"):
                    return os.path.relpath(os.path.join(root, f), self.repository_root_path)
        return None

    def _resolve_warmup_file(self) -> str | None:
        """Pick the file to warm with: explicit ``warmupFile``, else the ``-main`` module, else the
        first indexed ``.hx``.
        """
        configured = self._custom_settings.get("warmupFile")
        if configured:
            return configured
        return self._main_module_file() or self._first_hx_file()

    def _warmup_position(self, relative_file_path: str) -> tuple[int, int]:
        """A symbol position to issue the warm-up request at — the first document symbol, else (0, 0)."""
        try:
            symbols, _ = self.request_document_symbols(relative_file_path).get_all_symbols_and_roots()
            for symbol in symbols:
                start = symbol.get("selectionRange", {}).get("start")
                if start is not None:
                    return start["line"], start["character"]
        except Exception:
            log.debug("Haxe LSP warm-up: could not determine a symbol position in %s", relative_file_path, exc_info=True)
        return 0, 0

    def _warm_up(self) -> None:
        """Open a representative source file and issue one compiler-backed request, so the expensive
        first ``didOpen`` recompile is paid at startup rather than on the user's first find_* call.

        Best-effort: any error is logged and swallowed (warming is an optimisation, not a requirement).
        """
        relative_file_path = self._resolve_warmup_file()
        if relative_file_path is None:
            log.info("Haxe LSP warm-up: no source file found to warm with; skipping")
            return
        try:
            log.info("Haxe LSP warm-up: opening %s to pay the first didOpen recompile", relative_file_path)
            with self.open_file(relative_file_path):
                line, column = self._warmup_position(relative_file_path)
                self.request_hover(relative_file_path, line, column)
            log.info("Haxe LSP warm-up complete")
        except Exception:
            log.warning("Haxe LSP warm-up failed (non-fatal)", exc_info=True)

    def _run_warmup_bounded(self, budget: float) -> None:
        """Run :meth:`_warm_up` on a background thread, bounded by ``budget`` seconds.

        Gated by ``ls_specific_settings.haxe.warmup`` (default true). If the warm-up overruns the
        budget it keeps running in the background (the user's first request then completes it).
        """
        if not self._custom_settings.get("warmup", True):
            log.info("Haxe LSP warm-up disabled (ls_specific_settings.haxe.warmup=false)")
            return
        if budget <= 0:
            log.warning("Haxe LSP: no startup time budget left for warm-up; skipping")
            return
        warm_thread = threading.Thread(target=self._warm_up, name="haxe-warmup", daemon=True)
        warm_thread.start()
        warm_thread.join(timeout=budget)
        if warm_thread.is_alive():
            log.warning(
                "Haxe LSP warm-up did not finish within %.0fs; proceeding (it continues in the background)",
                budget,
            )

    @override
    def _start_server(self) -> None:
        """Start the Haxe Language Server and wait for it to reach stable idle.

        Uses textDocument/publishDiagnostics and $/progress tokens to track compilation activity, and
        only declares the server ready once it has stayed idle for a short settle window — the Haxe LS
        starts its classpath parse / refactoring cache *after* the first idle, so a naive
        "ready on first idle" lets the first compiler-backed request collide with that work.
        """

        def register_capability_handler(params: dict) -> None:
            # Haxe LS sends this but we don't need dynamic capability registration.
            return

        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_request("window/workDoneProgress/create", self._handle_work_done_progress_create)
        self.server.on_notification("window/logMessage", self._handle_window_log_message)
        self.server.on_notification("$/progress", self._handle_progress)
        self.server.on_notification("textDocument/publishDiagnostics", self._handle_diagnostics)
        self.server.on_notification("haxe/cacheBuildFailed", self._handle_cache_build_failed)
        self.server.on_notification("haxe/haxeKeepsCrashing", self._handle_haxe_keeps_crashing)

        log.info("Starting Haxe server process")
        self.server.start()
        initialize_params = self._get_initialize_params(self.repository_root_path)

        log.info("Sending initialize request from LSP client to LSP server and awaiting response")
        self.server.send.initialize(initialize_params)

        self._server_ready.clear()
        self.server.notify.initialized({})

        # LS doesn't properly initialise without a workspace_did_change_configuration notification here.
        config_settings: dict = {}
        haxe_path = self._custom_settings.get("haxePath")
        if haxe_path:
            config_settings["haxePath"] = haxe_path
        self.server.notify.workspace_did_change_configuration({"settings": config_settings})

        log.info("Waiting for Haxe LSP to reach stable idle (timeout=%.0fs)...", self._compilation_timeout)
        idle_wait_start = time.monotonic()
        if self._await_stable_idle(self._compilation_timeout, self._IDLE_SETTLE_WINDOW):
            log.info("Haxe server reached stable idle, server ready")
            # Warm the first didOpen recompile with whatever of the compilation budget remains.
            remaining_budget = self._compilation_timeout - (time.monotonic() - idle_wait_start)
            self._run_warmup_bounded(remaining_budget)
        else:
            log.warning(
                "Haxe LSP did not reach stable idle within %.0fs — responses may be incomplete",
                self._compilation_timeout,
            )

    @override
    def request_hover(self, relative_file_path: str, line: int, column: int, file_buffer: LSPFileBuffer | None = None) -> Hover | None:
        """Request hover info, returning None instead of raising on failure.

        The Haxe language server does not provide hover for all symbol types (e.g. class
        declarations), and may raise errors instead of returning None in those cases.
        """
        try:
            return super().request_hover(relative_file_path, line, column, file_buffer=file_buffer)
        except SolidLSPException:
            log.warning("Hover request failed for %s:%d:%d", relative_file_path, line, column, exc_info=True)
            return None

    @override
    def _get_wait_time_for_cross_file_referencing(self) -> float:
        return 5
