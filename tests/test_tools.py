"""Tool sandbox and shell guard-rail tests."""

from __future__ import annotations

import json

import pytest

from agent import tools




def test_list_dir_reports_entries(cfg):
    (cfg.workspace / "sub").mkdir()
    (cfg.workspace / "sub" / "file.txt").write_text("x", encoding="utf-8")
    listing = tools.execute(cfg, "list_dir", {"path": ".", "recursive": True})
    assert "sub/" in listing
    assert "sub/file.txt" in listing


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "sub/../../outside.txt"])
def test_path_traversal_is_refused(cfg, path):
    result = tools.execute(cfg, "read_file", {"path": path})
    assert result.startswith("error:")
    assert "escapes the workspace" in result


def test_symlink_escape_is_refused(cfg, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (cfg.workspace / "link").symlink_to(outside)
    result = tools.execute(cfg, "read_file", {"path": "link/secret.txt"})
    assert result.startswith("error:")


def test_read_file_rejects_missing_file(cfg):
    assert tools.execute(cfg, "read_file", {"path": "nope.txt"}).startswith("error:")


def test_unknown_tool_is_reported_not_raised(cfg):
    result = tools.execute(cfg, "delete_universe", {})
    assert result.startswith("error: unknown tool")


def test_invalid_json_arguments(cfg):
    result = tools.execute(cfg, "read_file", "{not json")
    assert "invalid JSON arguments" in result














def test_output_truncation(cfg):
    cfg.max_tool_chars = 50
    (cfg.workspace / "big.txt").write_text("x" * 500, encoding="utf-8")
    result = tools.execute(cfg, "read_file", {"path": "big.txt"})
    assert "truncated" in result


def test_tool_schemas_are_valid_openai_shapes():
    names = set()
    for schema in tools.TOOL_SCHEMAS:
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"]
        assert function["description"]
        assert function["parameters"]["type"] == "object"
        names.add(function["name"])
    assert names == set(tools.TOOL_NAMES)


def test_arguments_as_dict_are_accepted(cfg):
    (cfg.workspace / "d.txt").write_text("dict form", encoding="utf-8")
    result = tools.execute(cfg, "read_file", {"path": "d.txt"})
    assert "dict form" in result




def test_json_string_arguments_are_parsed(cfg):
    (cfg.workspace / "j.txt").write_text("from json", encoding="utf-8")
    result = tools.execute(cfg, "read_file", json.dumps({"path": "j.txt"}))
    assert "from json" in result






