"""Discover every model a saved key can use, and suggest a routing profile for each.

OpenRouter: only free models are offered (price 0 or a ``:free`` ID). Gemini: text chat models from the native
catalog (image, speech, audio, live, embedding, robotics, and agent products are left out). Other
OpenAI-compatible servers: whatever their ``/models`` lists. Profiles are suggestions from catalog data
(size, family, description, thinking support); the owner can edit them, and access can be checked with
one tiny request per model before adding it.
"""
import asyncio
import re
from urllib.parse import urlsplit

import httpx

from . import providers

# Anthropic list prices (USD per million input/output tokens) as of 2026-10-06. Suggestions only: L.E.O. never
# reads live billing, and the owner edits prices per connection. Haiku 5.5 doubles above 100K-token prompts.
CLAUDE_PRICES = {"claude-opus-4-5": (5, 25), "claude-sonnet-4-5": (3, 15), "claude-fable-5-1": (10, 50), "claude-fable-5": (10, 50), "claude-opus-5-5": (4, 20), "claude-opus-5": (5, 25),
                 "claude-opus-4-8": (5, 25), "claude-opus-4-7": (5, 25), "claude-opus-4-6": (5, 25), "claude-sonnet-5-5": (2, 10),
                 "claude-sonnet-5": (2, 10), "claude-sonnet-4-6": (3, 15), "claude-haiku-5-5": (0.10, 0.50), "claude-haiku-4-5": (1, 5)}
NOT_CHAT = re.compile(r"tts|image|banana|lyria|transcribe|embed|aqa|live|native-audio|robotics|computer-use|deep-research|"
                      r"customtools|antigravity|omni|safety|guard|moderation|rerank|whisper|vision-only", re.I)
CODER_NAME = re.compile(r"cod(e|er|ing|estral)|devstral|laguna|starcoder", re.I)
# Only descriptions that call the model a coding model, not ones that merely mention code among other uses.
CODER_DESCRIPTION = re.compile(r"\bcoding (agent|model|assistant|specialist)|\bcode (generation|model)|\bsoftware engineering (agent|model)|\bagentic coding", re.I)
STRONG = re.compile(r"\b(ultra|pro|opus|large|frontier|max)\b", re.I)
LIGHT = re.compile(r"\b(lite|mini|nano|small|tiny|xs|haiku|compact)\b", re.I)


def size_billions(*texts):
    """Largest parameter count mentioned (e.g. 550B, 31b, 2.6B), ignoring active-parameter figures like A4B."""
    sizes = [float(n) for text in texts for n in re.findall(r"(?<![a-z\d.])(\d+(?:\.\d+)?)\s?b\b(?!\s?-?active)", text or "", re.I)]
    return max(sizes) if sizes else None


NEGATED = re.compile(r"\b(against|not (recommended|suited|intended|designed)|avoid|unsuitable)\b", re.I)


def claims(description):
    """Description sentences that state what the model is for, minus ones that warn against a use."""
    return " ".join(part for part in re.split(r"(?<=[.!?])\s+", description[:600]) if not NEGATED.search(part))


def profile(model_id, description="", thinking=False, context=None, free=None, name=None):
    text = f"{model_id} {name or ''}"
    description = claims(description or "")
    size = size_billions(text, description[:400])
    # From Gemini 3 on, Flash-Lite writes at the level of earlier Flash models; the name alone undersells it.
    generation = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id)
    modern_lite = "flash-lite" in model_id and generation is not None and float(generation.group(1)) >= 3
    if STRONG.search(text) or (size and size >= 100):
        tier = "strong"
    elif not modern_lite and (LIGHT.search(text) or (size and size <= 10) or "flash-lite" in model_id):
        tier = "light"
    else:
        tier = "standard"
    skills = []
    if CODER_NAME.search(text) or CODER_DESCRIPTION.search(description[:300]):
        skills = ["coding", "backend", "frontend"]
    elif re.search(r"reasoning", f"{text} {description[:200]}", re.I) and tier == "strong":
        skills = ["reasoning", "coding", "backend"]
    elif model_id.startswith("gemini-") and tier != "light":
        skills = ["frontend", "design", "general"]  # Gemini's strength, as in the Gemini preset.
    # Everything else is a generalist: it can take any task and loses ties to a specialist.
    return {"model": model_id, "name": name or model_id, "tier": tier, "skills": skills, "reasoning": bool(thinking),
            "context_window": max(1024, min(int(context or 32768), 2000000)), "free": free, "description": (description or "")[:240]}


def provider_kind(connection):
    host = urlsplit(connection["base_url"]).hostname or ""
    if connection.get("protocol") == "anthropic" or host == "api.anthropic.com":
        return "anthropic"
    return "openrouter" if host.endswith("openrouter.ai") else "gemini" if host == "generativelanguage.googleapis.com" else "other"


async def _get(url, headers, transport, params=None):
    try:
        async with httpx.AsyncClient(timeout=30, transport=transport, trust_env=False, follow_redirects=False) as client:
            response = await client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise providers.ProviderError("Cannot reach the model catalog; check the URL and your connection") from exc
    providers.check_status(response)
    if len(response.content) > 10_000_000:
        raise providers.ProviderError("Model catalog is too large")
    return response.json()


async def discover(connection, key, transport=None):
    """Candidate models with suggested profiles, plus the ones left out and why."""
    kind, models, skipped = provider_kind(connection), [], []
    if kind == "openrouter":
        for item in (await _get(connection["base_url"] + "/models", providers.headers(connection, key), transport)).get("data", []):
            model_id, pricing = item.get("id", ""), item.get("pricing") or {}
            free = model_id.endswith(":free") or (pricing.get("prompt") == "0" and pricing.get("completion") == "0")
            if not free:
                continue  # Free models only for OpenRouter.
            outputs = (item.get("architecture") or {}).get("output_modalities") or ["text"]
            if model_id.startswith("openrouter/"):
                skipped.append({"model": model_id, "reason": "router alias, not a model"})
            elif outputs != ["text"] or NOT_CHAT.search(f"{model_id} {item.get('name', '')}"):
                skipped.append({"model": model_id, "reason": "not a text chat model"})
            else:
                reasoning = item.get("reasoning") or {}
                thinks = bool(reasoning.get("mandatory") or reasoning.get("default_enabled", bool(reasoning)))
                models.append(profile(model_id, item.get("description") or "", thinks, item.get("context_length"), True, item.get("name")))
    elif kind == "gemini":
        native = connection["base_url"].removesuffix("/openai")
        data = await _get(native + "/models", {"x-goog-api-key": key} if key else {}, transport, {"pageSize": 1000})
        for item in data.get("models", []):
            model_id = item.get("name", "").removeprefix("models/")
            methods = item.get("supportedGenerationMethods") or []
            if "generateContent" not in methods or NOT_CHAT.search(model_id) or (item.get("outputTokenLimit") or 0) < 8192:
                skipped.append({"model": model_id, "reason": "not a text chat model"})
            elif model_id.endswith("-latest"):
                skipped.append({"model": model_id, "reason": "alias of another model"})
            else:
                models.append(profile(model_id, item.get("description") or "", item.get("thinking"), item.get("inputTokenLimit"),
                                      connection.get("free"), item.get("displayName")))
    elif kind == "anthropic":
        data = await _get(connection["base_url"] + "/models", providers.headers(connection, key), transport, {"limit": 1000})
        for item in data.get("data", []):
            model_id = item.get("id", "")
            family = re.search(r"(opus|sonnet|haiku|fable|mythos)", model_id)
            version = re.search(r"-(\d+)(?:-(\d))?(?:-|$)", model_id)
            number = float(f"{version.group(1)}.{version.group(2) or 0}") if version else 0
            if not family or number < 4.5:
                skipped.append({"model": model_id, "reason": "older model"})
                continue
            tier = {"opus": "strong", "fable": "strong", "mythos": "strong", "sonnet": "standard", "haiku": "light"}[family.group(1)]
            skills = {"strong": ["coding", "backend", "frontend", "design", "reasoning"], "standard": ["coding", "backend", "frontend"], "light": []}[tier]
            price = CLAUDE_PRICES.get(re.sub(r"-\d{8}$", "", model_id))  # Dated snapshots share their family's price.
            models.append({"model": model_id, "name": item.get("display_name") or model_id, "tier": tier, "skills": skills,
                           # Effort control exists from 4.6 on; earlier models reject it.
                           "reasoning": number >= 4.6, "context_window": max(1024, min(int(item.get("max_input_tokens") or 200000), 2000000)),
                           "free": False, "input_price": price[0] if price else None, "output_price": price[1] if price else None,
                           "description": "Suggested price from Anthropic's list; edit it if it changes." if price else "Set prices before paid use."})
    else:
        data = await _get(connection["base_url"] + "/models", providers.headers(connection, key), transport)
        for item in data.get("data", data.get("models", [])):
            model_id = str(item.get("id", item.get("name", ""))).removeprefix("models/") if isinstance(item, dict) else ""
            if model_id and not NOT_CHAT.search(model_id):
                models.append(profile(model_id, "", False, item.get("context_length"), connection.get("free")))
    return {"provider": kind, "models": sorted(models, key=lambda m: m["model"]), "skipped": skipped}


async def check_access(connection, key, models, transport=None, concurrency=3):
    """One tiny request per model: {model: {"reason": None if usable, "reasoning": effort setting to keep}}.

    Some APIs reject an effort setting for models the catalog marks as thinking (e.g. Gemma on Gemini's
    OpenAI-compatible API); those are retried once without it and kept with effort control off."""
    gate = asyncio.Semaphore(concurrency)

    async def attempt(model, reasoning):
        candidate = {**connection, "model": model, "reasoning": reasoning}
        text = ""
        async for chunk in providers.stream(candidate, key, [{"role": "user", "content": "Reply with the word OK."}], 512, 45, transport, "low"):
            text += chunk.text
        return text

    async def one(model):
        async with gate:
            reasoning = model["reasoning"]
            try:
                try:
                    text = await attempt(model["model"], reasoning)
                except providers.ProviderError as exc:
                    if not (reasoning and "HTTP 400" in str(exc)):
                        raise
                    reasoning = False
                    text = await attempt(model["model"], False)
                return model["model"], {"reason": None if text.strip() else "returned no text", "reasoning": reasoning}
            except providers.ProviderError as exc:
                return model["model"], {"reason": str(exc).split(": Provider returned error")[0][:160], "reasoning": reasoning}
    return dict(await asyncio.gather(*(one(m) for m in models)))
