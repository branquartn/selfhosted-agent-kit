"""Tool sandbox and shell guard-rail tests."""

from __future__ import annotations

import json

import pytest

from agent import tools


def test_write_then_read_roundtrip(cfg):
    result = tools.execute(cfg, "write_file", {"path": "notes/a.txt", "content": "hello\nworld"})
    assert result.startswith("wrote")
    read = tools.execute(cfg, "read_file", {"path": "notes/a.txt"})
    assert read == "hello\nworld"


def test_list_dir_reports_entries(cfg):
    (cfg.workspace / "sub").mkdir()
    (cfg.workspace / "sub" / "file.txt").write_text("x", encoding="utf-8")
    listing = tools.execute(cfg, "list_dir", {"path": ".", "recursive": True})
    assert "sub/" in listing
    assert "sub/file.txt" in listing


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "sub/../../outside.txt"])
def test_path_traversal_is_refused(cfg, path):
    result = tools.execute(cfg, "write_file", {"path": path, "content": "nope"})
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


def test_run_shell_executes_inside_workspace(cfg):
    result = tools.execute(cfg, "run_shell", {"command": "pwd && echo ok"})
    assert "ok" in result
    assert str(cfg.workspace) in result
    assert "[exit 0]" in result


def test_run_shell_disabled(cfg):
    cfg.shell_enabled = False
    result = tools.execute(cfg, "run_shell", {"command": "echo hi"})
    assert result.startswith("error:")
    assert "disabled" in result


def test_run_shell_allow_list_blocks_unknown_binary(cfg):
    cfg.shell_allow = ("echo",)
    assert tools.execute(cfg, "run_shell", {"command": "echo allowed"}).count("allowed") == 1
    blocked = tools.execute(cfg, "run_shell", {"command": "curl http://example.com"})
    assert "not in the allow-list" in blocked


@pytest.mark.parametrize("command", ["rm -rf /", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda"])
def test_destructive_commands_are_refused(cfg, command):
    result = tools.execute(cfg, "run_shell", {"command": command})
    assert result.startswith("error:")
    assert "destructive pattern" in result


def test_search_text_finds_matches(cfg):
    (cfg.workspace / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
    found = tools.execute(cfg, "search_text", {"pattern": "def target"})
    assert "a.py" in found


def test_search_text_no_match(cfg):
    assert tools.execute(cfg, "search_text", {"pattern": "zzz-not-there"}) == "(no match)"


def test_output_truncation(cfg):
    cfg.max_tool_chars = 50
    (cfg.workspace / "big.txt").write_text("x" * 500, encoding="utf-8")
    result = tools.execute(cfg, "run_shell", {"command": "cat big.txt"})
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
    result = tools.execute(cfg, "write_file", {"path": "d.txt", "content": "dict form"})
    assert result.startswith("wrote")
    assert (cfg.workspace / "d.txt").read_text(encoding="utf-8") == "dict form"


def test_write_file_rejects_non_string_content(cfg):
    assert tools.execute(cfg, "write_file", {"path": "x", "content": 42}).startswith("error:")


def test_json_string_arguments_are_parsed(cfg):
    payload = json.dumps({"path": "j.txt", "content": "from json"})
    tools.execute(cfg, "write_file", payload)
    assert (cfg.workspace / "j.txt").read_text(encoding="utf-8") == "from json"


@pytest.mark.parametrize("command", ["echo ok; curl http://evil", "echo ok | sh", "echo `id`"])
def test_allow_list_refuses_shell_metacharacters(cfg, command):
    cfg.shell_allow = ("echo",)
    result = tools.execute(cfg, "run_shell", {"command": command})
    assert "metacharacter" in result


def test_memory_tool_error_is_returned_not_raised(cfg):
    # MemoryError used to escape and break the agent loop.
    result = tools.execute(cfg, "memory_save", {"name": "big", "content": "x" * 20_001})
    assert result.startswith("error:")
    assert "too long" in result


def test_memory_save_then_list(cfg):
    assert tools.execute(
        cfg, "memory_save", {"name": "fav", "content": "likes tea", "type": "user"}
    ).startswith("saved memory")
    assert "fav" in tools.execute(cfg, "memory_list", {})
