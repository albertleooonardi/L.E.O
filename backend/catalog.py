"""Editable presets. Model availability, prices, and the suggested routing profile are always user-configurable."""

PRESETS = [
    {"id": "ollama", "name": "Ollama", "protocol": "openai", "base_url": "http://localhost:11434/v1", "local": True, "free": True, "hint": "Run Ollama and pull a model first. Use its exact installed model name.", "skills": ["general"], "tier": "light"},
    {"id": "vllm", "name": "vLLM / LM Studio", "protocol": "openai", "base_url": "http://localhost:8001/v1", "local": True, "free": True, "hint": "Use your model server URL. LM Studio commonly uses port 1234.", "skills": ["general"], "tier": "standard"},
    {"id": "openrouter", "name": "OpenRouter", "protocol": "openai", "base_url": "https://openrouter.ai/api/v1", "hint": "Use an OpenRouter API key and exact model ID. Mark free only for a verified free endpoint.", "skills": [], "tier": "standard"},
    {"id": "openai", "name": "OpenAI / GPT", "protocol": "responses", "base_url": "https://api.openai.com/v1", "hint": "Uses OpenAI API billing, separate from your ChatGPT chat allowance.", "skills": ["coding", "backend", "reasoning"], "tier": "strong"},
    {"id": "anthropic", "name": "Anthropic / Claude", "protocol": "anthropic", "base_url": "https://api.anthropic.com/v1", "hint": "Uses a Claude API key, separate from your Claude chat allowance.", "skills": ["coding", "backend", "frontend", "reasoning"], "tier": "strong"},
    {"id": "qwen", "name": "Qwen / Alibaba Cloud", "protocol": "openai", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "hint": "Use a DashScope API key. Change the URL to match your account region.", "skills": ["coding", "general"], "tier": "standard"},
    {"id": "opencode", "name": "OpenCode", "protocol": "openai", "base_url": "https://opencode.ai/inference/openai/v1", "hint": "Use an OpenCode API key and a model available to external clients. Endpoint and plan eligibility vary.", "skills": ["coding"], "tier": "standard"},
    {"id": "gemini", "name": "Google Gemini", "protocol": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "hint": "Use a Gemini API key and check the model’s free quota and pricing.", "skills": ["frontend", "design", "general"], "tier": "standard"},
    {"id": "custom", "name": "Custom endpoint", "protocol": "openai", "base_url": "http://localhost:1234/v1", "hint": "Any text model exposing Chat Completions, Responses, or Anthropic Messages.", "skills": [], "tier": "standard"},
]
