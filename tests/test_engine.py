"""Agent loop and session storage tests."""

from __future__ import annotations

import json

import pytest

from agent.engine import Engine, SessionStore, valid_session_id
from agent.llm import LLMError, StreamResult


def tool_call(name: str, arguments: str, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_session_id_validation():
    assert valid_session_id("abc123")
    assert valid_session_id("a-b_c")
    assert not valid_session_id("")
    assert not valid_session_id("../etc")
    assert not valid_session_id("a" * 65)


def test_session_store_crud(cfg):
    store = SessionStore(cfg.sessions_dir)
    session = store.create(title="hello")
    assert store.load(session["id"])["title"] == "hello"
    assert [s["id"] for s in store.list()] == [session["id"]]
    assert store.delete(session["id"]) is True
    assert store.load(session["id"]) is None


def test_session_store_rejects_bad_id(cfg):
    store = SessionStore(cfg.sessions_dir)
    with pytest.raises(ValueError):
        store.load("../../etc/passwd")


def test_engine_runs_tool_then_answers(cfg, fake_llm):
    llm = fake_llm(
        [
            StreamResult(
                tool_calls=[
                    tool_call(
                        "write_file",
                        json.dumps({"path": "out.txt", "content": "written by the agent"}),
                    )
                ]
            ),
            StreamResult(content="All done: created out.txt."),
        ]
    )
    engine = Engine(cfg, llm=llm)
    events: list[dict] = []
    result = engine.run("sess1", "create out.txt", on_event=events.append)

    assert (cfg.workspace / "out.txt").read_text(encoding="utf-8") == "written by the agent"
    assert "All done" in result["text"]
    assert result["error"] is None
    assert result["steps"] == 2
    assert result["session_id"] == "sess1"
    assert result["tools"][0]["name"] == "write_file"

    types = [event["type"] for event in events]
    assert "step" in types
    assert "tool_call" in types
    assert "tool_result" in types
    assert types[-1] == "final"


def test_engine_persists_conversation(cfg, fake_llm):
    llm = fake_llm([StreamResult(content="hello there")])
    engine = Engine(cfg, llm=llm)
    engine.run("persisted", "hi", on_event=None)

    stored = SessionStore(cfg.sessions_dir).load("persisted")
    assert stored is not None
    roles = [message["role"] for message in stored["messages"]]
    assert roles == ["user", "assistant"]
    assert stored["title"] == "hi"


def test_engine_second_turn_keeps_history(cfg, fake_llm):
    llm = fake_llm([StreamResult(content="first"), StreamResult(content="second")])
    engine = Engine(cfg, llm=llm)
    engine.run("multi", "one")
    engine.run("multi", "two")

    second_call_messages = llm.calls[1]
    assert second_call_messages[0]["role"] == "system"
    contents = [message.get("content") for message in second_call_messages]
    assert "one" in contents
    assert "first" in contents
    assert "two" in contents


def test_engine_reports_tool_error_to_model(cfg, fake_llm):
    llm = fake_llm(
        [
            StreamResult(tool_calls=[tool_call("read_file", '{"path": "../secret"}')]),
            StreamResult(content="I could not read that file."),
        ]
    )
    engine = Engine(cfg, llm=llm)
    events: list[dict] = []
    engine.run("err-sess", "read ../secret", on_event=events.append)

    tool_results = [event for event in events if event["type"] == "tool_result"]
    assert tool_results and tool_results[0]["is_error"] is True
    # the error text is fed back to the model as a tool message
    assert any(message["role"] == "tool" for message in llm.calls[1])


def test_engine_handles_llm_failure(cfg, fake_llm):
    class Boom:
        def stream_chat(self, *args, **kwargs):
            raise LLMError("HTTP 401 from model endpoint: bad key")

    engine = Engine(cfg, llm=Boom())
    events: list[dict] = []
    result = engine.run("boom", "hello", on_event=events.append)

    assert result["error"] and "bad key" in result["error"]
    assert any(event["type"] == "error" for event in events)
    assert events[-1]["type"] == "final"


def test_engine_stops_at_step_budget(cfg, fake_llm):
    cfg.max_steps = 2
    script = [
        StreamResult(tool_calls=[tool_call("memory_list", "{}", f"c{i}")]) for i in range(2)
    ]
    engine = Engine(cfg, llm=fake_llm(script))
    events: list[dict] = []
    result = engine.run("budget", "loop forever", on_event=events.append)

    assert result["steps"] == 2
    assert any(event["type"] == "warning" for event in events)


def test_system_prompt_includes_memory_index(cfg):
    from agent.memory import MemoryStore

    MemoryStore(cfg.memory_dir).save(
        "user-lang", "Speaks French", mem_type="user", description="Preferred language"
    )
    prompt = Engine(cfg).system_prompt()
    assert "user-lang" in prompt
    assert str(cfg.workspace) in prompt


def test_custom_system_prompt_wins(cfg):
    cfg.system_prompt = "You are a pirate."
    prompt = Engine(cfg).system_prompt()
    assert prompt.startswith("You are a pirate.")
    assert str(cfg.workspace) not in prompt


def test_trim_respects_context_budget(cfg):
    cfg.context_chars = 300
    engine = Engine(cfg)
    messages = [{"role": "user", "content": "x" * 100} for _ in range(10)]
    trimmed = engine._trim(messages)
    assert len(trimmed) < len(messages)
    assert sum(len(json.dumps(m)) for m in trimmed) <= cfg.context_chars or len(trimmed) <= 2


def test_trim_never_leaves_leading_tool_message(cfg):
    cfg.context_chars = 80
    engine = Engine(cfg)
    messages = [
        {"role": "user", "content": "a" * 60},
        {"role": "tool", "tool_call_id": "1", "content": "b" * 60},
        {"role": "user", "content": "c" * 60},
    ]
    trimmed = engine._trim(messages)
    assert trimmed[0]["role"] != "tool"


def test_truncation_drops_orphan_tool_messages(cfg):
    store = SessionStore(cfg.sessions_dir)
    session = {
        "id": "orpha",
        "title": "t",
        "created": "",
        "updated": "",
        "messages": [
            {"role": "user", "content": "u0"},
            {"role": "tool", "tool_call_id": "x", "content": "orphan"},
        ]
        + [{"role": "user", "content": f"u{i}"} for i in range(500)],
    }
    store.save(session)
    reloaded = store.load("orpha")
    assert reloaded["messages"][0]["role"] != "tool"


def test_trailing_assistant_tool_calls_are_dropped(cfg):
    store = SessionStore(cfg.sessions_dir)
    store.save(
        {
            "id": "dangling",
            "title": "t",
            "created": "",
            "updated": "",
            "messages": [
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
                    ],
                },
            ],
        }
    )
    assert store.load("dangling")["messages"][-1]["role"] == "user"


def test_session_survives_client_disconnect(cfg, fake_llm):
    class Disconnecting:
        def stream_chat(self, messages, tools=None, on_event=None):
            if on_event:
                on_event("text", "partial")
            raise KeyboardInterrupt("client gone")

    engine = Engine(cfg, llm=Disconnecting())
    with pytest.raises(KeyboardInterrupt):
        engine.run("disconnect", "hello")
    stored = SessionStore(cfg.sessions_dir).load("disconnect")
    assert stored is not None
    assert any(message.get("content") == "hello" for message in stored["messages"])
