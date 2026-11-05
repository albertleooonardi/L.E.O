"""Setting up models from inside the CLI: add one with an API key, or open the website's Models settings, then sync.

Models are saved on the L.E.O. account (as in Settings → Models), so the website and every device see them too.
"""
import webbrowser

from prompt_toolkit import PromptSession
from rich.text import Text

from backend.catalog import PRESETS
from backend.discovery import discover
from backend.providers import ProviderError

from .account import AccountError, SignedOut
from .ui import pick

TYPE_ID = "\0type"  # "Type a model ID" in the model list.


def usable(account):
    """Enabled models the agent can use (tool calling not known to be missing)."""
    return [c for c in account.store().list_connections(account.user_id) if c["enabled"] and c.get("tools") is not False]


async def ask(message, default="", secret=False):
    try:
        return (await PromptSession().prompt_async(message, default=default, is_password=secret)).strip()
    except (EOFError, KeyboardInterrupt):
        return None


async def add_model(account, console, transport=None):
    """Provider, key, then one of its models (found through its catalog); saved on the account. True if one was added."""
    presets = [p for p in PRESETS if p["id"] != "9router"]  # 9Router accounts are signed in on the website.
    chosen = await pick("Which provider?", [(p["id"], f"{p['name']} · {p['hint']}") for p in presets])
    preset = next((p for p in presets if p["id"] == chosen), None)
    if not preset:
        return False
    base_url = preset["base_url"]
    if preset.get("local") or preset["id"] == "custom":
        base_url = await ask("API base URL: ", base_url)
        if not base_url:
            return False
    key = await ask("API key (leave empty if it needs none): " if preset.get("local") else "API key: ", secret=True)
    if key is None:
        return False
    connection = {"name": preset["name"], "protocol": preset["protocol"], "base_url": base_url.rstrip("/"),
                  "free": bool(preset.get("free")), "local": bool(preset.get("local"))}
    console.print(Text("Looking up its models…", style="leo.muted"))
    try:
        found = (await discover(connection, key or None, transport))["models"]
    except ProviderError as exc:
        console.print(Text(f"Could not list its models ({exc}); you can type a model ID.", style="leo.warn"))
        found = []
    # Models known to call tools first: the agent needs them.
    found.sort(key=lambda m: (m.get("tools") is not True, m["model"]))
    options = [(m["model"], f"{m['model']} · {'tools' if m.get('tools') else 'tools unknown' if m.get('tools') is None else 'no tools'}"
                            f"{' · free' if m.get('free') else ''} · {m['tier']}") for m in found[:60]]
    model_id = await pick("Which model?", options + [(TYPE_ID, "Type a model ID")]) if options else TYPE_ID
    if model_id is None:
        return False
    profile = next((m for m in found if m["model"] == model_id), None)
    if model_id == TYPE_ID:
        model_id = await ask("Model ID: ")
        if not model_id:
            return False
    profile = profile or {"tier": preset.get("tier", "standard"), "skills": preset.get("skills", []), "reasoning": False, "tools": None,
                          "context_window": 32768}
    body = {**connection, "name": f"{preset['name']} · {model_id}"[:80], "model": model_id, "api_key": key or None,
            "free": bool(profile.get("free", connection["free"])), "tier": profile["tier"], "skills": profile["skills"],
            "reasoning": profile["reasoning"], "tools": profile.get("tools"), "context_window": profile["context_window"],
            **{k: profile[k] for k in ("input_price", "output_price") if profile.get(k) is not None}}
    try:
        await account.add_connection(body)
    except AccountError as exc:
        console.print(Text(str(exc), style="leo.error"))
        return False
    console.print(Text(f"Added {body['name']} to your L.E.O. account.", style="leo.ok"))
    return True


async def setup(account, console, transport=None, login=None, signed_out=False):
    """Shown when no model can be used: stay in leo and set one up. True once there is one; False to quit.

    A device the server signed out (its token revoked, or a reset server) is offered a fresh sign-in first: adding or
    syncing models needs one. `login(account, console)` signs in through the browser."""
    while not usable(account):
        signed_out = signed_out or not account.secrets.get("device-token")
        if signed_out and login:
            console.print(Text(f"This device is not signed in to {account.server or 'a L.E.O. server'}; sign in again to get your models.",
                               style="leo.warn"))
            options = [("login", f"Sign in again · opens the L.E.O. website ({account.server or 'choose a server'})"),
                       ("quit", "Quit")]
        else:
            reason = ("None of your models can call tools, which the agent needs." if account.store().list_connections(account.user_id)
                      else "Your account has no models this computer can use yet.")
            console.print(Text(reason, style="leo.warn"))
            options = [("add", "Add a model with an API key (OpenRouter, Groq, Gemini, OpenAI, Anthropic, Ollama, …)"),
                       ("web", "Open L.E.O. Settings → Models in the browser (sign in to ChatGPT, Copilot, …)"),
                       ("sync", "Sync again (after adding models on the website)"),
                       ("quit", "Quit")]
        choice = await pick("Sign in to continue:" if signed_out and login else "How do you want to add one?", options)
        if choice in (None, "quit"):
            return False
        try:
            if choice == "login":
                await login(account, console)
                signed_out = False
            elif choice == "add":
                await add_model(account, console, transport)
            elif choice == "web":
                webbrowser.open(f"{account.server}/?settings=models")
                console.print(Text(f"Opened {account.server}/?settings=models. Add a model there, then choose Sync again.", style="leo.muted"))
            else:
                summary = await account.sync()
                console.print(Text(f"Synced {summary['models']} models.", style="leo.ok"))
        except SignedOut:
            signed_out = True  # The next round offers Sign in again.
        except AccountError as exc:
            console.print(Text(str(exc), style="leo.error"))
    return True
