"""Small protocol adapters; all routing and retry policy lives in the orchestrator."""
import json
from dataclasses import dataclass

import httpx


class ProviderError(Exception):
    def __init__(self, message, retryable=False, retry_after=0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class Chunk:
    text: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish: str | None = None


def headers(provider, key):
    result = {"Content-Type": "application/json"}
    if key:
        result["Authorization"] = f"Bearer {key}"
    if provider["protocol"] == "anthropic":
        result["anthropic-version"] = "2023-06-01"
        if key:
            result["x-api-key"] = key
    return result


def make_request(provider, messages, max_tokens, effort=None):
    """Build the request. `effort` (low/medium/high) is sent only to reasoning models, in each API's own form."""
    protocol = provider["protocol"]
    payload = {"model": provider["model"], "stream": True}
    effort = effort if provider.get("reasoning") else None
    if protocol == "anthropic":
        payload.update(system="\n\n".join(m["content"] for m in messages if m["role"] == "system"),
                       messages=[m for m in messages if m["role"] != "system"], max_tokens=max_tokens)
        return "/messages", payload
    if protocol == "responses":
        payload.update(input=messages, max_output_tokens=max_tokens, store=False)
        if effort:
            payload["reasoning"] = {"effort": effort}
        return "/responses", payload
    payload.update(messages=messages, stream_options={"include_usage": True})
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
        error = response.json().get("error")
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
    detail = provider_detail(response)
    raise ProviderError(f"{hints.get(code, 'Provider request failed')} (HTTP {code}){': ' + detail if detail else ''}",
                        code in {408, 429} or code >= 500, retry_after)


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
        if kind == "content_block_delta":
            return Chunk(text=data.get("delta", {}).get("text", ""))
        if kind == "message_delta":
            chunk = usage_chunk(data.get("usage", {}))
            chunk.finish = data.get("delta", {}).get("stop_reason")
            return chunk
        if kind == "message_stop":
            return Chunk(finish="stop")
    elif protocol == "responses":
        if data.get("type") == "response.output_text.delta":
            return Chunk(text=data.get("delta", ""))
        if data.get("type") in {"response.completed", "response.incomplete"}:
            response = data.get("response", {})
            chunk = usage_chunk(response.get("usage") or {})
            chunk.finish = "length" if data["type"] == "response.incomplete" else "stop"
            return chunk
    else:
        chunk = usage_chunk(data.get("usage") or {})
        choices = data.get("choices") or []
        if choices:
            content = choices[0].get("delta", {}).get("content")
            chunk.text = content if isinstance(content, str) else ""
            chunk.finish = choices[0].get("finish_reason")
        return chunk
    return Chunk()


async def stream(provider, key, messages, max_tokens, timeout, transport=None, effort=None):
    path, payload = make_request(provider, messages, max_tokens, effort)
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
                    output_bytes += len(chunk.text.encode())
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
