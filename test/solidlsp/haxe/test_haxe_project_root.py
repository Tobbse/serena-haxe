"""Unit tests for the Haxe projectRoot / --cwd compiler working-directory setting (WS2.1).

When Serena is rooted at a monorepo root but the Haxe build lives in a sub-project whose .hxml uses
CWD-relative includes/classpaths, the compiler must be told to treat the sub-project as its working
directory. We do that by prepending ``--cwd <abs projectRoot>`` to the compiler displayArguments
(Haxe processes --cwd before the build file, so the build file and all its relative paths resolve
against projectRoot). These tests check the argument construction only — no language server.
"""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.haxe_language_server import HaxeLanguageServer

pytestmark = [pytest.mark.haxe]


def _bare_haxe_ls(repo_root: Path) -> HaxeLanguageServer:
    ls = HaxeLanguageServer.__new__(HaxeLanguageServer)
    ls._custom_settings = {}
    ls.repository_root_path = str(repo_root)
    return ls


def _display_arguments(ls: HaxeLanguageServer, repo_root: Path) -> list[str]:
    params = ls._get_initialize_params(str(repo_root))
    return params["initializationOptions"]["displayArguments"]


def test_projectroot_prepends_cwd_before_the_build_file(tmp_path: Path) -> None:
    ls = _bare_haxe_ls(tmp_path)
    ls._custom_settings = {"buildFile": "main.hxml", "projectRoot": "client"}

    args = _display_arguments(ls, tmp_path)

    assert args[0] == "--cwd"
    assert args[1] == os.path.abspath(os.path.join(str(tmp_path), "client"))
    assert args[2:] == ["main.hxml"]


def test_projectroot_absolute_path_is_used_as_is(tmp_path: Path) -> None:
    abs_client = os.path.abspath(str(tmp_path / "client"))
    ls = _bare_haxe_ls(tmp_path)
    ls._custom_settings = {"buildFile": "main.hxml", "projectRoot": abs_client}

    args = _display_arguments(ls, tmp_path)

    assert args[0] == "--cwd"
    assert args[1] == abs_client
    assert args[2:] == ["main.hxml"]


def test_no_projectroot_leaves_display_arguments_unchanged(tmp_path: Path) -> None:
    ls = _bare_haxe_ls(tmp_path)
    ls._custom_settings = {"buildFile": "build.hxml"}

    args = _display_arguments(ls, tmp_path)

    assert "--cwd" not in args
    assert args == ["build.hxml"]
