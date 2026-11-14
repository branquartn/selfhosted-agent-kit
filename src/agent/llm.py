"""Minimal streaming client for any OpenAI-compatible chat completions API.

Works with DeepSeek, OpenAI, Groq, Together, OpenRouter, vLLM, LM Studio,
Ollama (`/v1` endpoint), etc. Only the standard library is used, so the kit
installs in seconds and has no supply-chain surface.

Design notes:

* ``stream_chat`` never raises on a model-level oddity; it raises ``LLMError``
  on transport/HTTP failures so the caller can surface a clean message.
* Tool calls arrive fragmented in streaming mode: they are accumulated by
  ``index`` before being returned.
* An ``on_event`` callback receives ``("text", chunk)`` and
  ``("reasoning", chunk)`` tuples so the web layer can forward tokens as they
  are produced.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

EventCallback = Callable[[str, str], None]


class LLMError(RuntimeError):
    """Raised when the model endpoint fails."""


@dataclass
class StreamResult:
    """Aggregated result of one chat completion round."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def assistant_message(self) -> dict[str, Any]:
        """Rebuild the assistant message to append to the conversation."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
            if not self.content:
                message["content"] = None
        return message


class LLMClient:
    """OpenAI-compatible chat completions client (streaming)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout

    # ------------------------------------------------------------------ HTTP
    def _request(self, payload: dict[str, Any]) -> urllib.request.Request:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "selfhosted-agent-kit/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )

    def _payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    # --------------------------------------------------------------- public
    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_event: EventCallback | None = None,
    ) -> StreamResult:
        """Run one completion round, streaming tokens through ``on_event``."""
        result = StreamResult()
        payload = self._payload(messages, tools, stream=True)
        try:
            with urllib.request.urlopen(self._request(payload), timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    self._consume_chunk(chunk, result, on_event)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise LLMError(f"HTTP {exc.code} from model endpoint: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"cannot reach model endpoint {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMError(f"model endpoint timed out after {self.timeout}s") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise LLMError(f"transport error talking to the model: {exc}") from exc
        return result

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> StreamResult:
        """Non-streaming completion (useful for sub-agents and tests)."""
        payload = self._payload(messages, tools, stream=False)
        try:
            with urllib.request.urlopen(self._request(payload), timeout=self.timeout) as resp:
                chunk = json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            raise LLMError(f"HTTP {exc.code} from model endpoint: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"cannot reach model endpoint {self.base_url}: {exc.reason}") from exc

        result = StreamResult()
        choices = chunk.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            result.content = message.get("content") or ""
            result.reasoning = message.get("reasoning_content") or ""
            for call in message.get("tool_calls") or []:
                result.tool_calls.append(
                    {
                        "id": call.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": (call.get("function") or {}).get("name") or "",
                            "arguments": (call.get("function") or {}).get("arguments") or "{}",
                        },
                    }
                )
            result.finish_reason = choices[0].get("finish_reason") or ""
        result.usage = chunk.get("usage") or {}
        return result

    # -------------------------------------------------------------- internal
    def _consume_chunk(
        self,
        chunk: dict[str, Any],
        result: StreamResult,
        on_event: EventCallback | None,
    ) -> None:
        if chunk.get("usage"):
            result.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                result.content += text
                if on_event:
                    on_event("text", text)
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                result.reasoning += reasoning
                if on_event:
                    on_event("reasoning", reasoning)
            for call in delta.get("tool_calls") or []:
                self._merge_tool_call(call, result.tool_calls)
            if choice.get("finish_reason"):
                result.finish_reason = choice["finish_reason"]

    @staticmethod
    def _merge_tool_call(call: dict[str, Any], bucket: list[dict[str, Any]]) -> None:
        raw_index = call.get("index")
        index = raw_index if isinstance(raw_index, int) else max(len(bucket) - 1, 0)
        while len(bucket) <= index:
            bucket.append(
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
        slot = bucket[index]
        if call.get("id"):
            slot["id"] = call["id"]
        if call.get("type"):
            slot["type"] = call["type"]
        fn = call.get("function") or {}
        if fn.get("name"):
            # Providers send the name once; concatenating would duplicate it
            # when a provider repeats the full name on every chunk.
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
