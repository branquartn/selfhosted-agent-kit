"""Memory store tests."""

from __future__ import annotations

import pytest

from agent.memory import Memory, MemoryError, MemoryStore, slugify


def test_slugify():
    assert slugify("User Preferences!") == "user-preferences"
    assert slugify("  ") == "memory"
    assert len(slugify("a" * 200)) == 64


def test_save_creates_markdown_file(cfg):
    store = MemoryStore(cfg.memory_dir)
    memory = store.save("user-name", "Nicolas", mem_type="user", description="Who the user is")
    path = cfg.memory_dir / "user-name.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "name: user-name" in text
    assert "type: user" in text
    assert "description: Who the user is" in text
    assert "Nicolas" in text
    assert memory.updated.endswith("Z")


def test_save_twice_updates_in_place(cfg):
    store = MemoryStore(cfg.memory_dir)
    store.save("pref", "old body", mem_type="user", description="d")
    store.save("pref", "new body", mem_type="user", description="d2")
    memories = store.all()
    assert len(memories) == 1
    assert memories[0].content == "new body"
    assert memories[0].description == "d2"


def test_index_text_lists_memories(cfg):
    store = MemoryStore(cfg.memory_dir)
    store.save("project-x", "body", mem_type="project", description="A project")
    index = store.index_text()
    assert "project-x" in index
    assert "A project" in index


def test_empty_store_has_empty_index(cfg):
    assert MemoryStore(cfg.memory_dir).index_text() == ""


def test_get_and_delete(cfg):
    store = MemoryStore(cfg.memory_dir)
    store.save("to-remove", "body")
    assert store.get("to-remove") is not None
    assert store.delete("to-remove") is True
    assert store.get("to-remove") is None
    assert store.delete("to-remove") is False


def test_rejects_empty_content_and_bad_type(cfg):
    store = MemoryStore(cfg.memory_dir)
    with pytest.raises(MemoryError):
        store.save("x", "   ")
    with pytest.raises(MemoryError):
        store.save("x", "body", mem_type="invalid-type")


def test_content_size_limit(cfg):
    store = MemoryStore(cfg.memory_dir)
    with pytest.raises(MemoryError):
        store.save("big", "x" * 20_001)


def test_markdown_roundtrip():
    memory = Memory.from_markdown(
        "---\nname: Some Name\ntype: feedback\ndescription: keep it short\n---\nBody line\n"
    )
    assert memory.name == "some-name"
    assert memory.type == "feedback"
    assert memory.content == "Body line"
    assert "name: some-name" in memory.to_markdown()


def test_from_markdown_without_frontmatter_uses_fallback():
    memory = Memory.from_markdown("just text", fallback_name="fallback")
    assert memory.name == "fallback"
    assert memory.type == "reference"
    assert memory.content == "just text"


def test_unknown_type_falls_back_to_reference():
    memory = Memory.from_markdown("---\nname: x\ntype: bogus\n---\nbody")
    assert memory.type == "reference"
