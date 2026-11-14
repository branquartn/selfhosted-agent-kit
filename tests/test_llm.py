"""HTTP client tests against a faked model endpoint (no network)."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from agent.llm import LLMClient, LLMError, StreamResult


def sse(*chunks) -> bytes:
    payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    payload += "data: [DONE]\n\n"
    return payload.encode("utf-8")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_stream_chat_collects_text_and_usage():
    body = sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"total_tokens": 7}},
    )
    seen: list[tuple[str, str]] = []
    with patch("agent.llm.urllib.request.urlopen", return_value=FakeResponse(body)):
        result = LLMClient("http://x/v1", "key", "model").stream_chat(
            [{"role": "user", "content": "hi"}], on_event=lambda k, c: seen.append((k, c))
        )
    assert result.content == "Hello"
    assert result.usage["total_tokens"] == 7
    assert result.finish_reason == "stop"
    assert seen == [("text", "Hel"), ("text", "lo")]


def test_stream_chat_accumulates_fragmented_tool_calls():
    body = sse(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "read_file", "arguments": '{"pa'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'th": "a.txt"}'}}
                        ]
                    }
                }
            ]
        },
    )
    with patch("agent.llm.urllib.request.urlopen", return_value=FakeResponse(body)):
        result = LLMClient("http://x/v1", "key", "model").stream_chat(
            [{"role": "user", "content": "hi"}]
        )
    assert result.wants_tools
    assert result.tool_calls[0]["id"] == "call_a"
    assert result.tool_calls[0]["function"]["name"] == "read_file"
    assert json.loads(result.tool_calls[0]["function"]["arguments"]) == {"path": "a.txt"}


def test_stream_chat_reasoning_is_forwarded():
    body = sse({"choices": [{"delta": {"reasoning_content": "thinking..."}}]})
    seen: list[tuple[str, str]] = []
    with patch("agent.llm.urllib.request.urlopen", return_value=FakeResponse(body)):
        result = LLMClient("http://x/v1", "key", "model").stream_chat(
            [{"role": "user", "content": "hi"}], on_event=lambda k, c: seen.append((k, c))
        )
    assert result.reasoning == "thinking..."
    assert ("reasoning", "thinking...") in seen


def test_stream_chat_maps_http_error():
    import urllib.error

    error = urllib.error.HTTPError("http://x", 401, "unauthorized", {}, io.BytesIO(b'{"error":"bad key"}'))
    with patch("agent.llm.urllib.request.urlopen", side_effect=error):
        with pytest.raises(LLMError) as excinfo:
            LLMClient("http://x/v1", "key", "model").stream_chat([{"role": "user", "content": "hi"}])
    assert "401" in str(excinfo.value)


def test_assistant_message_shape_with_tools():
    result = StreamResult(
        tool_calls=[{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]
    )
    message = result.assistant_message()
    assert message["role"] == "assistant"
    assert message["content"] is None
    assert message["tool_calls"][0]["function"]["name"] == "x"


def test_assistant_message_shape_without_tools():
    assert StreamResult(content="hi").assistant_message() == {"role": "assistant", "content": "hi"}


def test_stream_payload_asks_for_usage():
    captured = {}

    class Capture:
        def __init__(self):
            self.buffer = io.BytesIO(b"data: [DONE]\n\n")

        def __enter__(self):
            return self.buffer

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["payload"] = json.loads(request.data)
        return Capture()

    with patch("agent.llm.urllib.request.urlopen", side_effect=fake_urlopen):
        LLMClient("http://x/v1", "k", "m").stream_chat([{"role": "user", "content": "hi"}])
    assert captured["payload"]["stream_options"] == {"include_usage": True}


def test_tool_call_fragment_without_index_is_merged():
    body = sse(
        {"choices": [{"delta": {"tool_calls": [{"function": {"name": "a", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": ""}}]}}]},
    )
    with patch("agent.llm.urllib.request.urlopen", return_value=FakeResponse(body)):
        result = LLMClient("http://x/v1", "k", "m").stream_chat([{"role": "user", "content": "hi"}])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["function"]["name"] == "a"
