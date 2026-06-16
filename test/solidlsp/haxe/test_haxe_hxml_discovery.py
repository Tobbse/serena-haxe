"""Unit tests for Haxe ``.hxml`` build-file auto-discovery (WS2.2).

When ``buildFile`` isn't configured, Serena auto-discovers one. The pick is deterministic (shallowest
path, alphabetical tie-break), and when several ``.hxml`` match it warns and lists them. Why: a
multi-target monorepo has several build files, so the chosen one must be stable across machines and
visible — letting the user see what was picked and set ``buildFile`` to override it. Pure unit tests,
no language server.
"""

import logging
import os
from pathlib import Path

import pytest

from solidlsp.language_servers.haxe_language_server import HaxeLanguageServer

pytestmark = [pytest.mark.haxe]


def test_returns_empty_list_when_no_hxml(tmp_path: Path) -> None:
    assert HaxeLanguageServer._discover_hxml_file(str(tmp_path)) == []


def test_single_candidate_is_used(tmp_path: Path) -> None:
    (tmp_path / "build.hxml").write_text("-main Main\n", encoding="utf-8")
    assert HaxeLanguageServer._discover_hxml_file(str(tmp_path)) == ["build.hxml"]


def test_multiple_candidates_pick_shallowest_then_alphabetical(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.hxml").write_text("-main Deep\n", encoding="utf-8")  # depth 2
    (tmp_path / "zeta.hxml").write_text("-main Zeta\n", encoding="utf-8")  # depth 1
    (tmp_path / "alpha.hxml").write_text("-main Alpha\n", encoding="utf-8")  # depth 1, wins on alpha

    with caplog.at_level(logging.WARNING, logger="solidlsp.language_servers.haxe_language_server"):
        result = HaxeLanguageServer._discover_hxml_file(str(tmp_path))

    assert result == ["alpha.hxml"], "must pick the shallowest path, breaking ties alphabetically"
    # the warning must be non-silent: it lists every candidate and points at the buildFile setting
    assert "alpha.hxml" in caplog.text
    assert "zeta.hxml" in caplog.text
    assert os.path.join("sub", "deep.hxml") in caplog.text
    assert "buildFile" in caplog.text


def test_multiple_candidates_choice_is_order_independent(tmp_path: Path) -> None:
    """Whatever os.walk order the filesystem yields, the choice is stable."""
    (tmp_path / "b.hxml").write_text("-main B\n", encoding="utf-8")
    (tmp_path / "a.hxml").write_text("-main A\n", encoding="utf-8")
    assert HaxeLanguageServer._discover_hxml_file(str(tmp_path)) == ["a.hxml"]
