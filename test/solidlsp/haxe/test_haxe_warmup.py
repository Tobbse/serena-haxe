"""Unit tests for the Haxe startup warm-up (WS1.2).

The first compiler-backed request after a ``didOpen`` pays an expensive recompile; the second is
fast. Warming a representative file at startup moves that one-time cost off the user's first
``find_*`` call. File-resolution is tested against a real temporary repo; the open/request
orchestration and the on/off gate are tested with spies (no real language server).
"""

import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from solidlsp.language_servers.haxe_language_server import HaxeLanguageServer

pytestmark = [pytest.mark.haxe]


def _bare_haxe_ls(repo_root: Path | None = None) -> HaxeLanguageServer:
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._custom_settings = {}
    ls._compilation_timeout = 5.0
    if repo_root is not None:
        ls.repository_root_path = str(repo_root)
    return ls


def test_resolve_warmup_file_prefers_configured_warmupfile() -> None:
    ls = _bare_haxe_ls()
    ls._custom_settings = {"warmupFile": os.path.join("custom", "Entry.hx")}
    assert ls._resolve_warmup_file() == os.path.join("custom", "Entry.hx")


def test_resolve_warmup_file_uses_main_module_from_build_file(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Main.hx").write_text("class Main { static function main() {} }\n", encoding="utf-8")
    (tmp_path / "src" / "Other.hx").write_text("class Other {}\n", encoding="utf-8")
    (tmp_path / "build.hxml").write_text("-cp src\n-main Main\n--no-output\n", encoding="utf-8")

    ls = _bare_haxe_ls(tmp_path)
    assert ls._resolve_warmup_file() == os.path.join("src", "Main.hx")


def test_resolve_warmup_file_falls_back_to_first_hx(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Alpha.hx").write_text("class Alpha {}\n", encoding="utf-8")
    # No build file and thus no -main -> first .hx under the repo.
    ls = _bare_haxe_ls(tmp_path)
    assert ls._resolve_warmup_file() == os.path.join("src", "Alpha.hx")


def test_warm_up_opens_file_and_issues_a_request() -> None:
    ls = _bare_haxe_ls()
    rel = os.path.join("src", "Main.hx")
    ls._resolve_warmup_file = MagicMock(return_value=rel)
    ls._warmup_position = MagicMock(return_value=(0, 0))
    ls.open_file = MagicMock()
    ls.request_hover = MagicMock(return_value=None)

    ls._warm_up()

    ls.open_file.assert_called_once_with(rel)
    ls.request_hover.assert_called_once()


def test_warm_up_swallows_errors() -> None:
    ls = _bare_haxe_ls()
    ls._resolve_warmup_file = MagicMock(return_value=os.path.join("src", "Main.hx"))
    ls.open_file = MagicMock(side_effect=RuntimeError("boom"))
    ls._warm_up()  # must not raise


def test_run_warmup_bounded_disabled_does_not_warm() -> None:
    ls = _bare_haxe_ls()
    ls._custom_settings = {"warmup": False}
    ls._warm_up = MagicMock()

    ls._run_warmup_bounded(5.0)

    ls._warm_up.assert_not_called()


def test_run_warmup_bounded_enabled_warms() -> None:
    ls = _bare_haxe_ls()
    ls._custom_settings = {"warmup": True}
    warmed = threading.Event()
    ls._warm_up = MagicMock(side_effect=lambda: warmed.set())

    ls._run_warmup_bounded(5.0)

    assert warmed.wait(timeout=5.0)
    ls._warm_up.assert_called_once()


def test_run_warmup_bounded_skips_when_no_budget_left() -> None:
    ls = _bare_haxe_ls()
    ls._custom_settings = {"warmup": True}
    ls._warm_up = MagicMock()

    ls._run_warmup_bounded(0.0)

    ls._warm_up.assert_not_called()
