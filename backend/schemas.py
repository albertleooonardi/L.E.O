from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Connection(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    protocol: Literal["openai", "responses", "anthropic"] = "openai"
    base_url: str = Field(max_length=500)
    model: str = Field(min_length=1, max_length=160)
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    # Reuse the saved key of another connection to the same host, so one credential serves many models.
    key_from: str | None = Field(default=None, max_length=36)
    enabled: bool = True
    local: bool = False
    free: bool = False
    priority: int = Field(default=10, ge=0, le=100)
    context_window: int = Field(default=32768, ge=1024, le=2000000)
    input_price: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    output_price: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    token_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    # Routing profile: what the model is good at, how hard a task it can take, and whether it spends hidden reasoning tokens.
    skills: list[str] = Field(default_factory=list, max_length=20)
    tier: Literal["light", "standard", "strong"] = "standard"
    reasoning: bool = False

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Use an http:// or https:// API base URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Keep credentials and query parameters out of the base URL")
        if parsed.hostname in {"169.254.169.254", "metadata.google.internal"}:
            raise ValueError("Cloud metadata endpoints cannot be providers")
        return value.rstrip("/")


class Preferences(StrictModel):
    generator_id: str | None = None
    planner_id: str | None = None
    reviewer_id: str | None = None
    mode: Literal["auto", "quick", "reviewed", "deep"] = "auto"
    routing: Literal["auto", "priority", "local_first", "free_first"] = "auto"
    smart_routing: bool = True
    # Web research: HEAD searches when a request needs fresh or external information (auto) or never (off).
    web_search: Literal["auto", "off"] = "auto"
    search_url: str = Field(default="", max_length=300)
    search_pages: int = Field(default=3, ge=1, le=5)
    page_photos: bool = True
    allow_paid: bool = False
    max_cost: float = Field(default=0.10, gt=0, le=100, allow_inf_nan=False)
    max_calls: int = Field(default=8, ge=1, le=20)
    max_repairs: int = Field(default=1, ge=0, le=3)
    max_output_tokens: int = Field(default=8192, ge=128, le=65536)
    context_tokens: int = Field(default=64000, ge=1024, le=1000000)
    token_budget: int = Field(default=200000, ge=2048, le=2000000)
    timeout_seconds: int = Field(default=120, ge=10, le=600)
    max_run_seconds: int = Field(default=600, ge=30, le=3600)
    system_prompt: str = Field(default="You are L.E.O., a helpful assistant. Answer accurately and concisely. State uncertainty. For code, provide complete usable examples and explain assumptions.", max_length=4000)

    @field_validator("search_url")
    @classmethod
    def valid_search_url(cls, value):
        value = value.strip().rstrip("/")
        if value and (urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).hostname):
            raise ValueError("Use an http:// or https:// address for the search service")
        return value

    @model_validator(mode="after")
    def enough_context(self):
        if self.max_output_tokens >= self.context_tokens:
            raise ValueError("Context limit must exceed the output limit")
        return self


class RunInput(StrictModel):
    prompt: str = Field(min_length=1, max_length=30000)
    conversation_id: str | None = None
    mode: Literal["auto", "quick", "reviewed", "deep"] | None = None
    # Per-message override of web search: True forces it, False skips it, None lets HEAD decide.
    search: bool | None = None
    request_id: str = Field(min_length=8, max_length=100)

    @field_validator("prompt")
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("Enter a message")
        return value.strip()


class Skill(StrictModel):
    """A specialty the router matches tasks to; its instructions are added when a model works on that task."""
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    keywords: list[str] = Field(default_factory=list, max_length=60)
    instructions: str = Field(default="", max_length=4000)
    enabled: bool = True

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, value):
        return list(dict.fromkeys(k.strip().lower() for k in value if k.strip()))[:60]


class RouteInput(StrictModel):
    prompt: str = Field(min_length=1, max_length=30000)
    mode: Literal["auto", "quick", "reviewed", "deep"] | None = None


class DiscoveredModel(BaseModel):
    # Accepts entries as returned by discovery; display-only fields (description, added) are ignored.
    model_config = ConfigDict(extra="ignore")
    model: str = Field(min_length=1, max_length=160)
    name: str = Field(default="", max_length=120)
    tier: Literal["light", "standard", "strong"] = "standard"
    skills: list[str] = Field(default_factory=list, max_length=20)
    reasoning: bool = False
    context_window: int = Field(default=32768, ge=1024, le=2000000)
    free: bool | None = None
    input_price: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)
    output_price: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)


class AdoptModels(StrictModel):
    models: list[DiscoveredModel] = Field(min_length=1, max_length=60)
    check: bool = True
