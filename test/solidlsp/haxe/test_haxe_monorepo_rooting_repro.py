"""Controlled reproduction / verification of the Haxe ``find_declaration`` /
``find_implementations`` failure root cause.

Investigation handoff: ``.claude/docs/haxe-find-declaration-timeout-investigation.md`` (Issue 2).

Hypothesis under test:
    Serena starts the Haxe language server (and therefore the ``haxe`` compiler it spawns)
    with the process working directory set to the Serena project root
    (``ls.py`` -> ``ProcessLaunchInfo(cmd=cmd, cwd=self.repository_root_path)``).
    Haxe resolves relative ``.hxml`` includes and ``-cp`` entries against that working
    directory. In a monorepo where Serena is rooted at the repo root but the Haxe build
    file lives in a sub-project (and references its includes relatively), the compiler
    cannot process the build -> all *compiler-backed* LSP features
    (definition / implementation / references) fail, while *locally-parsed* features
    (document symbols, i.e. ``find_symbol``) keep working.

These tests construct a miniature monorepo and prove the causal chain with a real Haxe
compiler + language server:

  * ``test_haxe_resolves_relative_hxml_include_against_cwd`` (deterministic, compiler-only):
        the SAME build file succeeds when ``haxe`` runs from the sub-project dir and fails
        with ``ENOENT`` on its relative include when run from the monorepo root.
  * ``test_subproject_rooting_resolves_definition`` (language server):
        rooting the LS at the sub-project -> ``request_definition`` RESOLVES.
  * ``test_monorepo_rooting_breaks_compiler_backed_lookup`` (language server):
        rooting the LS at the monorepo root with the sub-project build -> ``request_definition``
        does NOT resolve (raises / empty) while ``request_document_symbols`` still works.

IMPORTANT (symptom nuance): in this controlled reproduction the broken build surfaces as a
*fast* LSP error (-32603, "Could not process argument ..."), NOT the multi-minute hang /
``haxe/haxeKeepsCrashing`` crash-loop reported against the user's large real monorepo. The
ROOT CAUSE (compiler working directory / relative path resolution) is identical; the precise
failure mode (fast error vs. indefinite hang) is environment/scale dependent.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from solidlsp.ls_config import Language
from solidlsp.ls_exceptions import SolidLSPException
from test.conftest import language_tests_enabled, start_ls_context

pytestmark = [pytest.mark.haxe]

_APP_HX = """\
class App {
    static function main() {
        var h = new Helper();
        var x = h.value();
        trace(x);
    }
}
"""

_HELPER_HX = """\
class Helper {
    public function new() {}
    public function value():Int {
        return 42;
    }
}
"""

# client/main.hxml references its include by the RELATIVE path "build/extra.hxml".
# Haxe resolves that relative to the compiler's working directory, NOT relative to main.hxml.
_MAIN_HXML = "build/extra.hxml\n-cp src\n-main App\n--no-output\n"
_EXTRA_HXML = "-D extra_marker\n"


def _make_monorepo(root: Path) -> Path:
    """Create a mini-monorepo with a Haxe sub-project at ``<root>/client`` and return it."""
    client = root / "client"
    (client / "src").mkdir(parents=True, exist_ok=True)
    (client / "build").mkdir(parents=True, exist_ok=True)
    (client / "src" / "App.hx").write_text(_APP_HX, encoding="utf-8")
    (client / "src" / "Helper.hx").write_text(_HELPER_HX, encoding="utf-8")
    (client / "main.hxml").write_text(_MAIN_HXML, encoding="utf-8")
    (client / "build" / "extra.hxml").write_text(_EXTRA_HXML, encoding="utf-8")
    return client


def _value_call_pos(app_hx: Path) -> tuple[int, int]:
    """0-based (line, col) of the cross-file ``value()`` call in App.hx."""
    for i, line in enumerate(app_hx.read_text(encoding="utf-8").splitlines()):
        if "h.value" in line:
            return i, line.index("value")
    raise AssertionError("could not locate the h.value() call site in App.hx")


@pytest.mark.skipif(shutil.which("haxe") is None, reason="haxe compiler not on PATH")
def test_haxe_resolves_relative_hxml_include_against_cwd(tmp_path: Path) -> None:
    """ROOT MECHANISM (compiler only): a relative .hxml include resolves against the process
    working directory, so the same build breaks when compiled from the monorepo root.
    """
    root = tmp_path
    client = _make_monorepo(root)

    # (1) Correct working directory == the sub-project: succeeds.
    ok = subprocess.run(["haxe", "main.hxml"], cwd=str(client), capture_output=True, text=True, check=False)
    assert ok.returncode == 0, f"expected success from sub-project CWD, got rc={ok.returncode}\n{ok.stderr}"

    # (2) Wrong working directory == the monorepo root: the relative include is resolved against
    #     the monorepo root and is not found -> hard failure (this is the root-cause mechanism).
    bad = subprocess.run(["haxe", "client/main.hxml"], cwd=str(root), capture_output=True, text=True, check=False)
    assert bad.returncode != 0, "expected failure when compiling from the monorepo root"
    combined = (bad.stderr + bad.stdout).lower()
    assert "extra.hxml" in combined, f"expected the relative include in the error, got:\n{bad.stderr}\n{bad.stdout}"
    assert ("enoent" in combined) or ("not found" in combined) or ("cannot read" in combined), (
        f"expected a file-not-found style error, got:\n{bad.stderr}\n{bad.stdout}"
    )

    # (3) Pointing the compiler working directory at the sub-project (the fix) restores success.
    fixed = subprocess.run(["haxe", "--cwd", "client", "main.hxml"], cwd=str(root), capture_output=True, text=True, check=False)
    assert fixed.returncode == 0, f"expected --cwd client to fix it, got rc={fixed.returncode}\n{fixed.stderr}"


@pytest.mark.skipif(not language_tests_enabled(Language.HAXE), reason="Haxe tests disabled in this environment")
def test_subproject_rooting_resolves_definition(tmp_path: Path) -> None:
    """CONTROL / FIX: when the LS is rooted at the sub-project (correct compiler CWD),
    ``request_definition`` on the cross-file ``value()`` call resolves to Helper.hx.
    """
    client = _make_monorepo(tmp_path)
    line, col = _value_call_pos(client / "src" / "App.hx")
    rel = os.path.join("src", "App.hx")

    with start_ls_context(
        Language.HAXE,
        repo_path=str(client),
        ls_specific_settings={Language.HAXE: {"buildFile": "main.hxml"}},
    ) as ls:
        ls.set_request_timeout(30.0)
        defs = ls.request_definition(rel, line, col)

    assert defs, "expected a definition when the LS is rooted at the sub-project"
    assert any("Helper.hx" in (d.get("relativePath") or d.get("uri", "")) for d in defs), (
        f"expected the definition to resolve to Helper.hx, got {defs}"
    )


@pytest.mark.skipif(not language_tests_enabled(Language.HAXE), reason="Haxe tests disabled in this environment")
def test_monorepo_rooting_breaks_compiler_backed_lookup(tmp_path: Path) -> None:
    """ROOT CAUSE at the LS level: rooting the LS at the monorepo root while the build file
    lives in the sub-project (with a CWD-relative include) breaks the *compiler-backed* lookup
    (``request_definition`` raises or returns no Helper.hx), while the *locally-parsed*
    document-symbol lookup still works -- exactly the user's "find_symbol works, the others
    don't" signature.
    """
    root = tmp_path
    _make_monorepo(root)
    rel = os.path.join("client", "src", "App.hx")
    line, col = _value_call_pos(root / "client" / "src" / "App.hx")

    with start_ls_context(
        Language.HAXE,
        repo_path=str(root),
        ls_specific_settings={Language.HAXE: {"buildFile": os.path.join("client", "main.hxml")}},
    ) as ls:
        ls.set_request_timeout(30.0)

        # Locally-parsed feature keeps working (this is why find_symbol is unaffected).
        symbols, _roots = ls.request_document_symbols(rel).get_all_symbols_and_roots()
        names = {s.get("name") for s in symbols}
        assert "App" in names, f"document symbols (local parse) should still work, got {names}"

        # Compiler-backed feature is broken: it either raises (the observed -32603) or
        # fails to resolve to Helper.hx. Either way the tool cannot do its job.
        resolved_to_helper = False
        try:
            defs = ls.request_definition(rel, line, col)
            resolved_to_helper = bool(defs) and any("Helper.hx" in (d.get("relativePath") or d.get("uri", "")) for d in defs)
        except SolidLSPException:
            resolved_to_helper = False

    assert not resolved_to_helper, (
        "monorepo rooting was expected to break the compiler-backed definition lookup, but it resolved to Helper.hx"
    )


@pytest.mark.skipif(not language_tests_enabled(Language.HAXE), reason="Haxe tests disabled in this environment")
def test_monorepo_rooting_with_project_root_resolves_definition(tmp_path: Path) -> None:
    """WS2.1 FIX (green half of the red/green pair with
    ``test_monorepo_rooting_breaks_compiler_backed_lookup``): keep the LS rooted at the monorepo
    root, but set ``projectRoot`` to the sub-project so the Haxe compiler runs with
    ``--cwd <sub-project>``. The build file (given relative to ``projectRoot``) and its CWD-relative
    include then resolve, and the compiler-backed definition lookup resolves to Helper.hx again.
    """
    root = tmp_path
    _make_monorepo(root)
    rel = os.path.join("client", "src", "App.hx")
    line, col = _value_call_pos(root / "client" / "src" / "App.hx")

    with start_ls_context(
        Language.HAXE,
        repo_path=str(root),
        ls_specific_settings={
            Language.HAXE: {"buildFile": "main.hxml", "projectRoot": "client", "compilationTimeout": 60},
        },
    ) as ls:
        ls.set_request_timeout(60.0)
        defs = ls.request_definition(rel, line, col)

    assert defs, "expected a definition once projectRoot points the compiler at the sub-project"
    assert any("Helper.hx" in (d.get("relativePath") or d.get("uri", "")) for d in defs), (
        f"expected the definition to resolve to Helper.hx, got {defs}"
    )
