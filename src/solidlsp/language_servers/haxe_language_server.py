"""
Provides Haxe specific instantiation of the LanguageServer class using vshaxe/haxe-language-server.
Contains various configurations and settings specific to the Haxe programming language.
"""

import glob
import logging
import os
import pathlib
import shutil
import threading

from overrides import override

from solidlsp import ls_types
from solidlsp.ls import (
    DocumentSymbols,
    LanguageServerDependencyProvider,
    LanguageServerDependencyProviderSinglePath,
    LSPFileBuffer,
    SolidLanguageServer,
    SymbolBody,
)
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.lsp_protocol_handler.lsp_types import InitializeParams
from solidlsp.settings import SolidLSPSettings

log = logging.getLogger(__name__)


class HaxeLanguageServer(SolidLanguageServer):
    """
    Provides Haxe specific instantiation of the LanguageServer class using vshaxe/haxe-language-server.
    Contains various configurations and settings specific to Haxe.

    The Haxe language server requires:
    - Haxe compiler (3.4.0+) installed and in PATH
    - Node.js installed and in PATH
    - The language server binary (server.js), typically from the vshaxe VSCode extension

    The server is discovered in this order:
    1. User-configured path via ls_specific_settings (ls_path key)
    2. System-installed haxe-language-server in PATH
    3. VSCode extension bundled server (~/.vscode/extensions/nadako.vshaxe-*/bin/server.js)
    4. Auto-download from VSCode marketplace (if node/npm available)
    """

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        """
        Creates a HaxeLanguageServer instance. This class is not meant to be instantiated directly.
        Use LanguageServer.create() instead.
        """
        super().__init__(
            config,
            repository_root_path,
            None,
            "haxe",
            solidlsp_settings,
        )

        # Compilation synchronisation: starts SET (= already done), cleared when the server
        # sends window/workDoneProgress/create or $/progress begin, set again once all
        # progress tokens have ended. This ensures hover/references requests are not sent
        # while the Haxe compiler is still building the project.
        self._compilation_complete = threading.Event()
        self._compilation_complete.set()
        self._active_progress_tokens: set[str] = set()
        self._progress_lock = threading.Lock()

    def _create_dependency_provider(self) -> LanguageServerDependencyProvider:
        return self.DependencyProvider(self._custom_settings, self._ls_resources_dir)

    class DependencyProvider(LanguageServerDependencyProviderSinglePath):
        def _get_or_install_core_dependency(self) -> str:
            """
            Find the Haxe Language Server binary.
            Checks system PATH, VSCode extension, and falls back to auto-download.
            """
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

            # 3. Check Serena resource directory for previously downloaded server
            haxe_ls_dir = os.path.join(self._ls_resources_dir, "haxe-lsp")
            server_js_path = os.path.join(haxe_ls_dir, "bin", "server.js")
            if os.path.exists(server_js_path):
                log.info(f"Found Haxe Language Server at {server_js_path}")
                return server_js_path

            # 4. Attempt to download from the VSCode marketplace
            is_node_installed = shutil.which("node") is not None
            if is_node_installed:
                downloaded_path = self._download_from_vscode_marketplace(haxe_ls_dir)
                if downloaded_path:
                    return downloaded_path

            raise FileNotFoundError(
                "Haxe Language Server not found. Install options:\n"
                "  1. Install the vshaxe VSCode extension (recommended): code --install-extension nadako.vshaxe\n"
                "  2. Set ls_path in serena_config.yml under ls_specific_settings.haxe\n"
                "  3. Build from source:\n"
                "     git clone https://github.com/vshaxe/haxe-language-server\n"
                "     cd haxe-language-server && npm ci && npx lix run vshaxe-build -t language-server\n"
                "     Then set ls_path to the resulting bin/server.js"
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

        @staticmethod
        def _download_from_vscode_marketplace(target_dir: str) -> str | None:
            """
            Attempt to download the Haxe language server from the VSCode marketplace.
            Downloads the vshaxe extension VSIX and extracts server.js from it.
            """
            import gzip
            import io
            import tempfile
            import zipfile

            try:
                import urllib.request

                vsix_url = (
                    "https://marketplace.visualstudio.com/_apis/public/gallery/publishers/nadako/vsextensions/vshaxe/latest/vspackage"
                )
                log.info("Downloading Haxe Language Server from VSCode marketplace...")

                os.makedirs(target_dir, exist_ok=True)
                vsix_path = os.path.join(tempfile.gettempdir(), "vshaxe.vsix")

                urllib.request.urlretrieve(vsix_url, vsix_path)

                # The marketplace may return gzip-compressed content;
                # detect by checking the gzip magic bytes (\x1f\x8b)
                with open(vsix_path, "rb") as f:
                    raw_data = f.read()

                if raw_data[:2] == b"\x1f\x8b":
                    log.info("VSIX is gzip-compressed, decompressing...")
                    raw_data = gzip.decompress(raw_data)

                # VSIX files are ZIP archives; extract the language server binary
                with zipfile.ZipFile(io.BytesIO(raw_data), "r") as zf:
                    # Find server.js in the extension
                    server_entries = [n for n in zf.namelist() if n.endswith("bin/server.js")]
                    if not server_entries:
                        log.warning("Could not find server.js in vshaxe VSIX package")
                        return None

                    # Extract the bin/ directory contents
                    bin_dir = os.path.join(target_dir, "bin")
                    os.makedirs(bin_dir, exist_ok=True)

                    for entry in zf.namelist():
                        if "/bin/" in entry:
                            # Strip the prefix path to extract into our bin/ dir
                            filename = entry.split("/bin/", 1)[-1]
                            if filename:
                                dest_path = os.path.join(bin_dir, filename)
                                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                                with zf.open(entry) as src, open(dest_path, "wb") as dst:
                                    dst.write(src.read())

                server_js_path = os.path.join(bin_dir, "server.js")
                if os.path.exists(server_js_path):
                    log.info(f"Successfully downloaded Haxe Language Server to {server_js_path}")
                    return server_js_path

                log.warning("Downloaded VSIX but server.js not found after extraction")
                return None

            except Exception:
                log.warning("Failed to download Haxe Language Server from VSCode marketplace", exc_info=True)
                return None

        def _create_launch_command(self, core_path: str) -> list[str]:
            # If the core_path is a .js file, launch with node
            if core_path.endswith(".js"):
                return ["node", core_path]
            # Otherwise assume it's a directly executable binary
            return [core_path, "--stdio"]

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        return super().is_ignored_dirname(dirname) or dirname in [
            "node_modules",
            "export",
            "dump",
        ]

    @override
    def request_document_symbols(self, relative_file_path: str, file_buffer: LSPFileBuffer | None = None) -> DocumentSymbols:
        """Override to fix Haxe LSP symbol boundary issues.

        The Haxe language server sometimes reports incorrect ``range`` values for
        methods — the ``range.end`` extends far beyond the actual method body,
        engulfing subsequent sibling methods. This post-processing step detects
        such cases by counting brace depth in the source file and promotes
        incorrectly-nested children back to siblings.
        """
        result = super().request_document_symbols(relative_file_path, file_buffer=file_buffer)

        if not result.root_symbols:
            return result

        self._fix_symbol_boundaries(result.root_symbols, relative_file_path)

        return result

    def _fix_symbol_boundaries(self, symbols: list[ls_types.UnifiedSymbolInformation], relative_file_path: str) -> None:
        """Walk the symbol tree and fix any symbols whose range extends
        beyond their actual body (common Haxe LSP bug).
        """
        i = 0
        while i < len(symbols):
            symbol = symbols[i]
            children = symbol.get("children", [])
            if children:
                # Recursively fix children first
                self._fix_symbol_boundaries(children, relative_file_path)

                # Check if any children should actually be siblings
                promoted = self._check_and_promote_children(symbol, relative_file_path)
                if promoted:
                    # Insert promoted children as siblings after this symbol
                    for j, promoted_child in enumerate(promoted):
                        promoted_child["parent"] = symbol.get("parent")
                        symbols.insert(i + 1 + j, promoted_child)
            i += 1

    def _check_and_promote_children(
        self, symbol: ls_types.UnifiedSymbolInformation, relative_file_path: str
    ) -> list[ls_types.UnifiedSymbolInformation]:
        """Check if a symbol's range is too large and some children should be siblings.
        Returns list of children to promote.
        """
        children = symbol.get("children", [])
        if not children:
            return []

        # Only check methods/functions — classes legitimately contain children
        sym_kind = symbol.get("kind", 0)
        METHOD_KIND = 6  # LSP SymbolKind.Method
        FUNCTION_KIND = 12  # LSP SymbolKind.Function
        if sym_kind not in (METHOD_KIND, FUNCTION_KIND):
            return []

        # Get the symbol's reported range
        sym_range = symbol.get("location", {}).get("range", symbol.get("range", {}))
        sym_start_line: int = sym_range["start"]["line"]
        sym_end_line: int = sym_range["end"]["line"]

        # Read the relevant lines from the file
        try:
            abs_path = os.path.join(self.repository_root_path, relative_file_path)
            with open(abs_path, encoding=self._encoding) as f:
                lines = f.readlines()
        except (OSError, FileNotFoundError):
            return []

        # Find the actual end of the method by counting brace depth
        actual_end_line = self._find_method_end(lines, sym_start_line)

        if actual_end_line is None or actual_end_line >= sym_end_line:
            return []  # Range looks correct or we can't determine

        # Promote children that start after the actual end
        to_promote: list[ls_types.UnifiedSymbolInformation] = []
        to_keep: list[ls_types.UnifiedSymbolInformation] = []
        for child in children:
            child_sel = child.get("selectionRange", {})
            child_start: int = child_sel.get("start", {}).get("line", 0)
            if child_start > actual_end_line:
                to_promote.append(child)
            else:
                to_keep.append(child)

        if to_promote:
            # Fix the symbol's range
            sym_range["end"]["line"] = actual_end_line
            sym_range["end"]["character"] = len(lines[actual_end_line].rstrip()) if actual_end_line < len(lines) else 0
            symbol["children"] = to_keep

            # Rebuild the symbol body with corrected range
            if "body" in symbol:
                symbol["body"] = SymbolBody(
                    lines=[line.rstrip("\n") for line in lines],
                    start_line=sym_start_line,
                    start_col=sym_range["start"]["character"],
                    end_line=actual_end_line,
                    end_col=sym_range["end"]["character"],
                )

            log.debug(
                "Haxe boundary fix: promoted %d children from %s (corrected end: line %d -> %d)",
                len(to_promote),
                symbol.get("name", "?"),
                sym_end_line,
                actual_end_line,
            )

        return to_promote

    @staticmethod
    def _find_method_end(lines: list[str], start_line: int) -> int | None:
        """Find the actual end line of a method by counting brace depth,
        skipping string literals and comments.
        """
        depth = 0
        found_opening = False
        in_string: str | None = None  # None, '"', or "'"
        in_block_comment = False

        for i in range(start_line, len(lines)):
            line = lines[i]
            j = 0
            while j < len(line):
                # Block comment
                if in_block_comment:
                    if line[j : j + 2] == "*/":
                        in_block_comment = False
                        j += 2
                        continue
                    j += 1
                    continue

                # Line comment
                if line[j : j + 2] == "//":
                    break  # Rest of line is comment

                # Block comment start
                if line[j : j + 2] == "/*":
                    in_block_comment = True
                    j += 2
                    continue

                # String handling
                if in_string:
                    if line[j] == "\\":
                        j += 2  # Skip escaped char
                        continue
                    if line[j] == in_string:
                        in_string = None
                    j += 1
                    continue

                if line[j] in ('"', "'"):
                    in_string = line[j]
                    j += 1
                    continue

                # Brace counting
                if line[j] == "{":
                    depth += 1
                    found_opening = True
                elif line[j] == "}":
                    depth -= 1
                    if found_opening and depth == 0:
                        return i

                j += 1

        return None

    def _get_initialize_params(self, repository_absolute_path: str) -> InitializeParams:
        """
        Returns the initialize params for the Haxe Language Server.

        Uses the following strategy for displayArguments:
        1. User-configured buildFile from ls_specific_settings.haxe.buildFile
        2. Recursive auto-discovery of .hxml files with smart filtering
        """
        root_uri = pathlib.Path(repository_absolute_path).as_uri()

        # 1. Check for user-configured .hxml path
        configured_build_file = self._custom_settings.get("buildFile")
        if configured_build_file:
            log.info(f"Using user-configured Haxe build file: {configured_build_file}")
            display_arguments = [configured_build_file]
        else:
            # 2. Auto-discover .hxml files recursively
            display_arguments = self._discover_hxml_files(repository_absolute_path)

        initialize_params = {
            "locale": "en",
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True},
                    "completion": {"completionItem": {"snippetSupport": True}},
                    "definition": {},
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
            "initializationOptions": {
                "displayArguments": display_arguments,
            },
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
    def _discover_hxml_files(repository_absolute_path: str) -> list[str]:
        """
        Recursively discover .hxml files, filtering out dependency directories
        and prioritizing build files by name pattern.

        :return: list of display arguments (relative paths to .hxml files), or empty list
        """
        max_depth = 4
        skip_dirs = {"node_modules", "haxe_libraries", ".haxelib", "export", "dump", "bin", ".git", "build"}

        hxml_files: list[str] = []
        for root, dirs, files in os.walk(repository_absolute_path):
            # Skip dependency/build output directories
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            depth = root.replace(repository_absolute_path, "").count(os.sep)
            if depth > max_depth:
                dirs.clear()
                continue
            for f in files:
                if f.endswith(".hxml"):
                    hxml_files.append(os.path.join(root, f))

        # Filter out library descriptors (lix haxe_libraries/*.hxml)
        hxml_files = [f for f in hxml_files if "haxe_libraries" not in f]

        if not hxml_files:
            log.info("No .hxml files found in project")
            return []

        # Prioritize build files by name pattern
        def score_hxml(path: str) -> int:
            name = os.path.basename(path).lower()
            if "debug" in name:
                return 0  # Best: debug build has most info
            if "build" in name:
                return 1
            if "test" in name:
                return 2
            return 3

        hxml_files.sort(key=score_hxml)

        # Use relative path from project root
        best_hxml = os.path.relpath(hxml_files[0], repository_absolute_path)
        log.info(f"Auto-discovered Haxe build file: {best_hxml} (from {len(hxml_files)} candidates)")
        return [best_hxml]

    def _start_server(self) -> None:
        """
        Starts the Haxe Language Server, waits for compilation to complete, and yields the LanguageServer instance.

        Uses $/progress token tracking (same pattern as Kotlin LS) to detect when the Haxe
        compiler finishes its initial build. This is critical for large projects where
        compilation can take significantly longer than the previous 30s diagnostics-based wait.
        """

        def do_nothing(params: dict) -> None:
            return

        def window_log_message(msg: dict) -> None:
            log.info(f"LSP: window/logMessage: {msg}")

        def register_capability_handler(params: dict) -> None:
            """Handle client/registerCapability requests from the server."""
            return

        def work_done_progress_create(params: dict) -> dict:
            """Handle window/workDoneProgress/create: the server is about to report async progress.
            Clear the compilation-complete event so _start_server waits until all tokens finish.
            """
            token = str(params.get("token", ""))
            log.debug(f"Haxe LSP workDoneProgress/create: token={token!r}")
            with self._progress_lock:
                self._active_progress_tokens.add(token)
                self._compilation_complete.clear()
            return {}

        def progress_handler(params: dict) -> None:
            """Track $/progress begin/end to detect when all async compilation work finishes."""
            token = str(params.get("token", ""))
            value = params.get("value", {})
            kind = value.get("kind")
            if kind == "begin":
                title = value.get("title", "")
                log.info(f"Haxe LSP progress [{token}]: started - {title}")
                with self._progress_lock:
                    self._active_progress_tokens.add(token)
                    self._compilation_complete.clear()
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
                        self._compilation_complete.set()

        self.server.on_request("client/registerCapability", register_capability_handler)
        self.server.on_request("window/workDoneProgress/create", work_done_progress_create)
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("$/progress", progress_handler)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        log.info("Starting Haxe server process")
        self.server.start()
        initialize_params = self._get_initialize_params(self.repository_root_path)

        log.info("Sending initialize request from LSP client to LSP server and awaiting response")
        self.server.send.initialize(initialize_params)

        self.server.notify.initialized({})

        # Force didChangeConfiguration — some Haxe LSP versions require this to properly start
        self.server.notify.workspace_did_change_configuration({"settings": {}})

        # Wait for compilation to complete (same pattern as Kotlin LS):
        # - _compilation_complete starts SET (from __init__).
        # - If the server sends window/workDoneProgress/create, work_done_progress_create
        #   clears the event. wait() then blocks until all progress tokens end.
        # - If the server never sends workDoneProgress/create (simple project / older version),
        #   the event stays SET and wait() returns immediately.
        # No grace period timer needed — the event is only cleared by actual server signals.
        _COMPILATION_TIMEOUT = 300.0

        log.info("Waiting for Haxe LSP compilation to complete...")
        if self._compilation_complete.wait(timeout=_COMPILATION_TIMEOUT):
            log.info("Haxe server compilation completed, server ready")
        else:
            log.warning(
                "Haxe LSP did not signal compilation completion within %.0fs; proceeding anyway",
                _COMPILATION_TIMEOUT,
            )

        log.info("Haxe server ready")

    @override
    def _get_wait_time_for_cross_file_referencing(self) -> float:
        """Small safety buffer since we already waited for compilation to complete in _start_server."""
        return 1.0
