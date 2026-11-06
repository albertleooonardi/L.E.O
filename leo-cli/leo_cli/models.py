"""Setting up models from inside the CLI: sign in with an account, or add several with an API key, then sync.

Models are saved on the L.E.O. account (as in Settings → Models), so the website and every device see them too.
"""
import asyncio
import webbrowser

from prompt_toolkit import PromptSession
from rich.text import Text

from backend.catalog import PRESETS
from backend.discovery import discover
from backend.providers import ProviderError

from .account import AccountError, SignedOut
from .ui import pick, pick_many

TYPE_ID = "\0type"  # "Type a model ID" in the model list.


def usable(account):
    """Enabled models the agent can use (tool calling not known to be missing)."""
    return [c for c in account.store().list_connections(account.user_id) if c["enabled"] and c.get("tools") is not False]


async def ask(message, default="", secret=False):
    try:
        return (await PromptSession().prompt_async(message, default=default, is_password=secret)).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def saved_keys(account):
    """One connection per provider whose key is on this device, so "more models" needs no key typed again."""
    store, found = account.store(), {}
    for connection in store.list_connections(account.user_id, private=True):
        if "/api/cli/relay/" not in connection["base_url"] and (connection["local"] or store.decrypt(connection)):
            found.setdefault(connection["base_url"], connection)
    return list(found.values())


async def add_model(account, console, transport=None):
    """/models add: the ways in of Settings → Models. True if a model was added."""
    sources = saved_keys(account)
    options = [("account", "Sign in with an account · ChatGPT, GitHub Copilot, Kiro, Kimi, …")]
    options += [(index, f"More models from {source['name'].split(' · ')[0]} · uses your saved key") for index, source in enumerate(sources)]
    options.append(("key", "Add a provider with an API key · OpenRouter, Gemini, OpenAI, Anthropic, Ollama, …"))
    choice = await pick("How do you want to add models?", options)
    if choice is None:
        return False
    if choice == "account":
        return await sign_in_account(account, console)
    if choice == "key":
        return await add_with_key(account, console, transport)
    source = sources[choice]
    connection = {key: source[key] for key in ("protocol", "base_url", "free", "local")}
    return await add_from(account, console, transport, {**connection, "name": source["name"].split(" · ")[0]}, account.store().decrypt(source))


async def add_with_key(account, console, transport=None):
    """Provider and key, then its models (found through its catalog); saved on the account. True if one was added."""
    presets = [p for p in PRESETS if p["id"] != "9router"]  # 9Router accounts are signed in with "Sign in with an account".
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
    return await add_from(account, console, transport, connection, key or None, preset)


async def add_from(account, console, transport, connection, key, preset=None):
    """Tick the provider's models to add. Those known to call tools come first: the agent needs them."""
    console.print(Text(f"Looking up the models of {connection['name']}…", style="leo.muted"))
    try:
        found = (await discover(connection, key, transport))["models"]
    except ProviderError as exc:
        console.print(Text(f"Could not list its models ({exc}); you can type a model ID.", style="leo.warn"))
        found = []
    added = {c["model"] for c in account.store().list_connections(account.user_id) if c["base_url"] == connection["base_url"]}
    fresh = sorted((m for m in found if m["model"] not in added), key=lambda m: (m.get("tools") is not True, m["model"]))
    if found and not fresh:
        console.print(Text(f"All {len(found)} models it lists are already added; you can still type a model ID.", style="leo.muted"))
    picked = [TYPE_ID]
    if fresh:
        options = [(m["model"], f"{m.get('name') or m['model']} · {'tools' if m.get('tools') else 'tools unknown' if m.get('tools') is None else 'no tools'}"
                                f"{' · free' if m.get('free') else ''} · {m['tier']}") for m in fresh]
        picked = await pick_many(f"Which models of {connection['name']}?", options + [(TYPE_ID, "Type a model ID")], keep=TYPE_ID)
        if not picked:
            return False
    profiles = {m["model"]: m for m in fresh}
    if TYPE_ID in picked:
        typed = await ask("Model ID: ")
        picked = [model for model in picked if model != TYPE_ID] + ([typed] if typed else [])
    saved, default = [], {"tier": (preset or {}).get("tier", "standard"), "skills": (preset or {}).get("skills", []), "reasoning": False,
                          "tools": None, "context_window": 32768}
    try:
        for model_id in picked:
            profile = profiles.get(model_id, default)
            label = (profile.get("name") or model_id).removesuffix(" (free)")
            body = {**connection, "name": f"{connection['name']} · {label}"[:80], "model": model_id, "api_key": key,
                    "free": bool(profile.get("free", connection["free"])), "tier": profile["tier"], "skills": profile["skills"],
                    "reasoning": profile["reasoning"], "tools": profile.get("tools"), "context_window": profile["context_window"],
                    **{k: profile[k] for k in ("input_price", "output_price") if profile.get(k) is not None}}
            await account.add_connection(body, sync=False)
            saved.append(body["name"])
        if saved:
            await account.sync()
    except SignedOut:
        raise
    except AccountError as exc:
        console.print(Text(str(exc), style="leo.error"))
    if saved:
        console.print(Text(f"Added to your L.E.O. account: {', '.join(saved)}.", style="leo.ok"))
    return bool(saved)


async def sign_in_account(account, console, poll_seconds=3):
    """Sign in to ChatGPT, Copilot, and the others on the server's 9Router, as in Settings → Models. True if signed in."""
    offered = await account.model_accounts()
    if not offered["available"]:
        console.print(Text(offered.get("error") or "Your L.E.O. server cannot offer account sign-in.", style="leo.warn"))
        return False
    signed = {}
    for item in offered["accounts"]:
        signed.setdefault(item["provider"], item)
    chosen = await pick("Which account?", [(p["id"], p["name"] + (" · signed in, add another" if p["id"] in signed else ""))
                                           for p in offered["providers"]])
    provider = next((p for p in offered["providers"] if p["id"] == chosen), None)
    if not provider:
        return False
    if provider.get("risk"):
        console.print(Text(provider["risk"], style="leo.warn"))
        if await pick(f"Sign in to {provider['name']} anyway?", [("yes", "Yes, I accept this"), ("no", "No")], default="no") != "yes":
            return False
    session = await account.start_sign_in(provider["id"], bool(provider.get("risk")))
    try:
        opened = webbrowser.open(session["url"])
    except Exception:  # No browser here (SSH): the link below works from any device.
        opened = False
    if session.get("user_code"):
        console.print(Text.assemble(("Enter this code on the sign-in page:  ", "leo.muted"), (f" {session['user_code']} ", "leo.key")))
    # On one line, so it copies cleanly.
    console.print(Text(f"Sign in to {provider['name']}" + (" in your browser. If it did not open, go to:" if opened else "; open this link:"), style="leo.muted"))
    console.print(Text(session["url"], style="underline"), soft_wrap=True)
    console.print(Text("Waiting for you to finish… (Ctrl-C to cancel)", style="leo.muted"))
    try:
        while session["status"] in ("waiting", "finishing"):
            await asyncio.sleep(poll_seconds)
            session = await account.sign_in_status(session["id"])
    except asyncio.CancelledError:
        await account.cancel_sign_in(session["id"])
        console.print(Text("Sign-in cancelled.", style="leo.warn"))
        return False
    if session["status"] != "done":
        console.print(Text(session.get("error") or "Sign-in was not completed.", style="leo.error"))
        return False
    await account.sync()
    result = session.get("result") or {}
    who = f" as {result['account']}" if result.get("account") else ""
    console.print(Text(f"Signed in to {provider['name']}{who}: {result.get('added', 0)} models added.", style="leo.ok"))
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
                       ("account", "Sign in with an account (ChatGPT, GitHub Copilot, Kiro, Kimi, …)"),
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
                await add_with_key(account, console, transport)
            elif choice == "account":
                await sign_in_account(account, console)
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
