"""Small protocol adapters; all routing and retry policy lives in the orchestrator."""
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


# A limit on the whole account, not on the one model that happened to hit it.
SHARED_LIMIT = re.compile(r"free-models-per-day|usage limit has been reached", re.I)


class ProviderError(Exception):
    def __init__(self, message, retryable=False, retry_after=0, shared_seconds=None, limit=None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.shared_seconds = shared_seconds  # A SHARED_LIMIT: seconds until it comes back.
        self.limit = limit  # Requests that limit allows, when the provider says.


def quota_group(provider):
    """The name of the limit this model shares with others, or None. OpenRouter counts every `:free` model of a key
    together, and a router's models with one prefix (`cx/`) are one signed-in account."""
    host, model = urlsplit(provider["base_url"]).netloc, provider["model"]
    if model.endswith(":free"):
        return f"{host}:free"
    return f"{host}:{model.split('/', 1)[0]}" if "/" in model else None


def shared_reset(response, detail):
    """Seconds until a shared limit comes back: from the provider's reset time or its message, else an hour."""
    stamp = response.headers.get("x-ratelimit-reset", "")
    if stamp.isdigit():
        moment = int(stamp) / (1000 if int(stamp) > 10**11 else 1)
        return max(moment - time.time(), 60)
    said = re.search(r"reset after (?:(\d+)h\s*)?(?:(\d+)m\s*)?(?:(\d+)s)?", detail)
    if said and any(said.groups()):
        return max(int(said.group(1) or 0) * 3600 + int(said.group(2) or 0) * 60 + int(said.group(3) or 0), 60)
    return 3600


@dataclass
class Chunk:
    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish: str | None = None
    # Tool-call fragments: {"index", "id", "name", "arguments"}; assemble them with ToolCalls.
    calls: list | None = None


class ToolCalls:
    """Assembles streamed tool-call fragments into complete calls: [{"id", "name", "arguments": dict}]."""

    def __init__(self):
        self.parts = {}

    def add(self, fragments):
        for fragment in fragments or []:
            # Some OpenAI-compatible servers (Gemini) send whole calls without an index; each is its own call.
            key = fragment.get("index")
            key = len(self.parts) if key is None else key
            part = self.parts.setdefault(key, {"id": None, "name": "", "arguments": ""})
            part["id"] = fragment.get("id") or part["id"]
            part["name"] = fragment.get("name") or part["name"]
            part["arguments"] += fragment.get("arguments") or ""

    def result(self):
        calls = []
        for n, part in enumerate(self.parts[k] for k in sorted(self.parts)):
            if not part["name"]:
                continue
            try:
                arguments = json.loads(part["arguments"]) if part["arguments"].strip() else {}
                if not isinstance(arguments, dict):
                    raise ValueError()
            except ValueError:
                # Malformed arguments go back to the model as an error instead of failing the run.
                arguments = {"_invalid_json": part["arguments"][:2000]}
            calls.append({"id": part["id"] or f"call_{n}", "name": part["name"], "arguments": arguments})
        return calls


def _openai_messages(messages):
    result = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            result.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            result.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            result.append({"role": m["role"], "content": m["content"]})
    return result


def _anthropic_messages(messages):
    result = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            previous = result[-1] if result else None
            # All results of one assistant turn go back together in a single user message.
            if previous and previous["role"] == "user" and isinstance(previous["content"], list):
                previous["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            result.append({"role": "assistant", "content": ([{"type": "text", "text": m["content"]}] if m.get("content") else []) +
                           [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]} for c in m["tool_calls"]]})
        else:
            result.append({"role": m["role"], "content": m["content"]})
    return result


def _responses_input(messages):
    result = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            if m.get("content"):
                result.append({"role": "assistant", "content": m["content"]})
            result += [{"type": "function_call", "call_id": c["id"], "name": c["name"], "arguments": json.dumps(c["arguments"])}
                       for c in m["tool_calls"]]
        elif m["role"] == "tool":
            result.append({"type": "function_call_output", "call_id": m["tool_call_id"], "output": m["content"]})
        else:
            result.append({"role": m["role"], "content": m["content"]})
    return result


def headers(provider, key):
    result = {"Content-Type": "application/json"}
    if provider["protocol"] == "anthropic":
        # Anthropic reads API keys from x-api-key; a Bearer header is treated as an OAuth token.
        result["anthropic-version"] = "2023-06-01"
        if key:
            result["x-api-key"] = key
    elif key:
        result["Authorization"] = f"Bearer {key}"
    return result


def make_request(provider, messages, max_tokens, effort=None, tools=None):
    """Build the request. `effort` (low/medium/high) is sent only to reasoning models, in each API's own form.

    `messages` use one neutral transcript format: assistant messages may carry `tool_calls`
    ([{"id", "name", "arguments": dict}]) and `{"role": "tool", "tool_call_id", "name", "content"}` returns a result.
    `tools` are [{"name", "description", "parameters": JSON schema}]."""
    protocol = provider["protocol"]
    payload = {"model": provider["model"], "stream": True}
    effort = effort if provider.get("reasoning") else None
    if protocol == "anthropic":
        payload.update(system="\n\n".join(m["content"] for m in messages if m["role"] == "system"),
                       messages=_anthropic_messages(messages), max_tokens=max_tokens)
        if effort:
            payload["output_config"] = {"effort": effort}  # Thinking depth on current Claude models.
        if tools:
            payload["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        return "/messages", payload
    if protocol == "responses":
        payload.update(input=_responses_input(messages), max_output_tokens=max_tokens, store=False)
        if effort:
            payload["reasoning"] = {"effort": effort}
        if tools:
            payload["tools"] = [{"type": "function", **t} for t in tools]
        return "/responses", payload
    payload.update(messages=_openai_messages(messages), stream_options={"include_usage": True})
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
    payload[provider.get("token_parameter", "max_tokens")] = max_tokens
    if effort:
        # OpenRouter takes a reasoning object; OpenAI-compatible APIs (OpenAI, Gemini, vLLM) take reasoning_effort.
        if "openrouter.ai" in provider["base_url"]:
            payload["reasoning"] = {"effort": effort}
        else:
            payload["reasoning_effort"] = effort
    return "/chat/completions", payload


def provider_detail(response):
    """Short provider explanation (e.g. which quota was hit). Never echoes request headers or keys."""
    try:
        data = response.json()
        # Gemini's OpenAI-compatible API wraps errors in a one-item list.
        error = (data[0] if isinstance(data, list) and data else data).get("error")
        message = error.get("message") if isinstance(error, dict) else error
        if isinstance(error, dict) and isinstance(error.get("metadata"), dict) and isinstance(error["metadata"].get("raw"), str):
            message = f"{message}: {error['metadata']['raw']}"
    except (ValueError, AttributeError):
        return ""
    return " ".join(message.split())[:240] if isinstance(message, str) else ""


def check_status(response):
    if response.is_success:
        return
    code = response.status_code
    hints = {401: "API key was rejected", 403: "Account or model access was denied", 404: "Endpoint or model was not found",
             400: "Provider rejected the request; check model, protocol, context size, and token parameter",
             402: "Provider credits are exhausted", 429: "Provider rate limit reached"}
    try:
        retry_after = min(3600, max(1, float(response.headers.get("retry-after", "30"))))
    except ValueError:
        retry_after = 30
    if "retry-after" not in response.headers:
        # Gemini states the wait in the body instead ("Please retry in 17.8s", or a RetryInfo retryDelay).
        hint = re.search(r'retry in (\d+(?:\.\d+)?)\s*s|"retryDelay":\s*"(\d+(?:\.\d+)?)s"', response.text[:20000])
        if hint:
            retry_after = min(3600, float(hint.group(1) or hint.group(2)))
    detail = provider_detail(response)
    hint = hints.get(code, "Provider request failed")
    if code == 429 and re.search(r"quota", detail, re.I):
        # Free tiers report daily or per-minute quotas as 429 and mention billing only as an upgrade path.
        hint = "Provider quota reached; free-tier quotas reset over time"
    elif re.search(r"credit balance|insufficient (credits|funds|quota)|billing", detail, re.I):
        hint = "Provider credits or billing are exhausted; add credits in the provider's console"
    shared, limit = (shared_reset(response, detail), response.headers.get("x-ratelimit-limit", "")) if SHARED_LIMIT.search(detail) else (None, "")
    raise ProviderError(f"{hint} (HTTP {code}){': ' + detail if detail else ''}", code in {408, 429} or code >= 500, retry_after, shared,
                        int(limit) if limit.isdigit() else None)


def usage_chunk(usage):
    def count(value):
        return value if type(value) is int and value >= 0 else None
    input_tokens = count(usage.get("input_tokens", usage.get("prompt_tokens")))
    output_tokens = count(usage.get("output_tokens", usage.get("completion_tokens")))
    total = count(usage.get("total_tokens"))
    # Some APIs (Gemini) leave billed thinking tokens out of the output count but include them in the total.
    if total is not None and input_tokens is not None and output_tokens is not None:
        output_tokens = max(output_tokens, total - input_tokens)
    return Chunk(input_tokens=input_tokens, output_tokens=output_tokens)


def decode_chunk(protocol, data):
    if data.get("error") or data.get("type") in {"error", "response.failed"}:
        raise ProviderError("Provider reported a streaming error", True)
    if protocol == "anthropic":
        kind = data.get("type")
        if kind == "message_start":
            return usage_chunk(data.get("message", {}).get("usage", {}))
        if kind == "content_block_start" and (data.get("content_block") or {}).get("type") == "tool_use":
            block = data["content_block"]
            return Chunk(calls=[{"index": data.get("index"), "id": block.get("id"), "name": block.get("name"), "arguments": ""}])
        if kind == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "input_json_delta":
                return Chunk(calls=[{"index": data.get("index"), "arguments": delta.get("partial_json", "")}])
            return Chunk(text=delta.get("text", ""))
        if kind == "message_delta":
            chunk = usage_chunk(data.get("usage", {}))
            chunk.finish = data.get("delta", {}).get("stop_reason")
            return chunk
        if kind == "message_stop":
            return Chunk(finish="stop")
    elif protocol == "responses":
        if data.get("type") == "response.output_text.delta":
            return Chunk(text=data.get("delta", ""))
        if data.get("type") == "response.output_item.done" and (data.get("item") or {}).get("type") == "function_call":
            # The finished item carries the whole call, so argument deltas need no assembly.
            item = data["item"]
            return Chunk(calls=[{"index": data.get("output_index"), "id": item.get("call_id"), "name": item.get("name"),
                                 "arguments": item.get("arguments", "")}])
        if data.get("type") in {"response.completed", "response.incomplete"}:
            response = data.get("response", {})
            chunk = usage_chunk(response.get("usage") or {})
            chunk.finish = "length" if data["type"] == "response.incomplete" else "stop"
            return chunk
    else:
        chunk = usage_chunk(data.get("usage") or {})
        choices = data.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            chunk.text = content if isinstance(content, str) else ""
            chunk.finish = choices[0].get("finish_reason")
            if delta.get("tool_calls"):
                chunk.calls = [{"index": c.get("index"), "id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                                "arguments": (c.get("function") or {}).get("arguments") or ""} for c in delta["tool_calls"]]
        return chunk
    return Chunk()


async def stream(provider, key, messages, max_tokens, timeout, transport=None, effort=None, tools=None):
    path, payload = make_request(provider, messages, max_tokens, effort, tools)
    finished = False
    output_bytes = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 15)), transport=transport, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", provider["base_url"] + path, headers=headers(provider, key), json=payload) as response:
                if not response.is_success:
                    await response.aread()  # Error bodies are small and explain quota/model problems.
                check_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    if raw == "[DONE]":
                        finished = True
                        break
                    if len(raw) > 2_000_000:
                        raise ProviderError("Provider event exceeded the size limit")
                    chunk = decode_chunk(provider["protocol"], json.loads(raw))
                    output_bytes += len(chunk.text.encode()) + sum(len((c.get("arguments") or "").encode()) for c in chunk.calls or [])
                    if output_bytes > max_tokens * 64:
                        raise ProviderError("Provider exceeded the response size limit")
                    if chunk.finish:
                        finished = True
                    yield chunk
        if not finished:
            raise ProviderError("Provider stream ended before completion", True)
    except httpx.TimeoutException as exc:
        raise ProviderError("Provider timed out", True) from exc
    except httpx.HTTPError as exc:
        raise ProviderError("Cannot reach the provider; check its URL and that the server is running", True) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise ProviderError("Provider returned an unsupported response format") from exc


async def list_models(provider, key, transport=None):
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False, trust_env=False) as client:
            response = await client.get(provider["base_url"] + "/models", headers=headers(provider, key))
            check_status(response)
            if len(response.content) > 5_000_000:
                raise ProviderError("Model catalog is too large; enter the model ID manually")
            data = response.json()
            models = data.get("data", data.get("models", []))
            # Gemini lists "models/<id>" while its chat endpoint takes the bare model ID.
            return sorted({str(m.get("id", m.get("name", ""))).removeprefix("models/") for m in models if isinstance(m, dict)} - {""})
    except httpx.HTTPError as exc:
        raise ProviderError("Cannot reach model catalog; check the URL and server") from exc
    except (ValueError, TypeError) as exc:
        raise ProviderError("Model discovery is unavailable; enter the model ID manually") from exc
