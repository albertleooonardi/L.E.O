"""The /usage and /context screens: what the models have cost, and how full the agent's context window is.

Both read the local store, which records every model call this device makes (the server's Usage page adds up all
devices, from the totals leo reports).
"""
import json
import time

from rich.console import Group
from rich.table import Table
from rich.text import Text

from backend.agent import COMPACT_AT, SCHEMAS
from backend.context import token_bound


def tokens(n):
    n = n or 0
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def money(cost):
    cost = cost or 0
    return "free" if not cost else f"${cost:.4f}" if cost < 1 else f"${cost:.2f}"


def ago(stamp):
    seconds = max(0, time.time() - (stamp or 0))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def meter(fraction, width, g, style="leo.ok", mark=None):
    """A bar `width` cells wide, filled to `fraction`; `mark` (0..1) puts a tick where something happens."""
    filled = max(0, min(width, round(width * fraction)))
    text = Text()
    for i in range(width):
        if mark is not None and i == min(width - 1, round(width * mark)) and i >= filled:
            text.append("│" if g.bar != "#" else "|", style="leo.warn")
        elif i < filled:
            text.append(g.bar, style=style)
        else:
            text.append(g.track, style="leo.border")
    return text


def summary(total):
    used = (total.get("input_tokens") or 0) + (total.get("output_tokens") or 0)
    return (f"{total.get('calls', 0)} calls · {'~' if total.get('estimated') else ''}{tokens(used)} tokens "
            f"({tokens(total.get('input_tokens'))} in, {tokens(total.get('output_tokens'))} out) · {money(total.get('cost'))}")


def usage_view(store, uid, session_id=None, folder_ids=(), g=None):
    """Every model this device has called: calls, tokens, cost, and its share of all tokens; then the totals for this
    session, this folder, and this device."""
    report = store.usage_report(user_id=uid, recent=0)
    models = report["models"]
    if not models:
        return Text("No model calls on this device yet. Send a task, then /usage shows what each model used.", style="leo.muted")
    everything = sum((m["input_tokens"] or 0) + (m["output_tokens"] or 0) for m in models) or 1
    table = Table(box=g.box, border_style="leo.border", header_style="leo.muted", title=Text("Model usage on this device", style="leo.title"),
                  title_justify="left")
    for column, justify in (("Model", "left"), ("Share of tokens", "left"), ("Calls", "right"), ("Input", "right"),
                            ("Output", "right"), ("Cost", "right"), ("Last used", "right")):
        table.add_column(column, justify=justify, no_wrap=column != "Model")
    for model in sorted(models, key=lambda m: -((m["input_tokens"] or 0) + (m["output_tokens"] or 0))):
        used = (model["input_tokens"] or 0) + (model["output_tokens"] or 0)
        share = used / everything
        bar = meter(share, 12, g, "leo.accent")
        bar.append(f" {share * 100:4.0f}%", style="leo.muted")
        failed = model["uncertain"]
        calls = Text(str(model["calls"]))
        if failed:
            calls.append(f" ({failed} failed)", style="leo.warn")
        table.add_row(Text(model["model"]) + Text(f"\n{model['provider']}", style="leo.muted"), bar, calls,
                      ("~" if model["estimated"] else "") + tokens(model["input_tokens"]), tokens(model["output_tokens"]),
                      Text(money(model["cost"]), style="leo.ok" if not model["cost"] else ""), Text(ago(model["last_used"]), style="leo.muted"))
    chats = {c["id"]: c for c in report["conversations"]}
    totals = Table.grid(padding=(0, 2))
    totals.add_column(style="leo.muted")
    totals.add_column()
    if session_id in chats:
        totals.add_row("this session", summary(chats[session_id]))
    folder = [chats[i] for i in folder_ids if i in chats]
    if folder:
        totals.add_row("this folder", summary({"calls": sum(c["calls"] for c in folder), "cost": sum(c["cost"] for c in folder),
                                               "input_tokens": sum(c["input_tokens"] for c in folder),
                                               "output_tokens": sum(c["output_tokens"] for c in folder),
                                               "estimated": any(c["estimated"] for c in folder)}))
    totals.add_row("this device", summary(report["totals"]))
    note = Text(("~ estimated: the model did not report its token counts. " if any(m["estimated"] for m in models) else "")
                + "All your devices add up on the website's Usage page.", style="leo.muted")
    return Group(table, Text(""), totals, Text(""), note)


def context_view(run, store, uid, model=None, g=None):
    """How full the lead agent's context window is, and with what: system prompt, messages, tool results, tools."""
    prefs = store.preferences()
    connections = store.list_connections(uid)
    if not model:
        model = next((c["model"] for c in connections if c["enabled"]), None)
    window = next((c["context_window"] for c in connections if c["model"] == model), None)
    limit = min(window, prefs["context_tokens"]) if window else prefs["context_tokens"]
    allowance = prefs["context_tokens"] - prefs["max_output_tokens"]  # The agent summarizes older steps against this.
    state = ((run or {}).get("state") or {}).get("agent") or {}
    transcript = (state.get("transcripts") or {}).get("main") or []
    parts = {"system prompt": 0, "your messages": 0, "agent replies and tool calls": 0, "tool results": 0}
    names = {"system": "system prompt", "user": "your messages", "assistant": "agent replies and tool calls", "tool": "tool results"}
    for message in transcript:
        parts[names.get(message["role"], "your messages")] += token_bound([message]) - 32
    parts["tool definitions"] = len(json.dumps(list(SCHEMAS.values()))) // 3
    used = sum(parts.values())
    head = Text.assemble(("Context window", "leo.title"), ("  ·  ", "leo.muted"), (str(model or "no model yet"), "leo.accent"),
                         (f"  ·  {tokens(window)} window" if window else "", "leo.muted"),
                         (f", L.E.O. uses up to {tokens(limit)}" if window and limit < window else "", "leo.muted"))
    compact_at = allowance * COMPACT_AT
    inside = bool(limit) and compact_at < limit
    bar = meter(used / limit if limit else 0, 40, g, "leo.warn" if used > compact_at else "leo.ok",
                mark=compact_at / limit if inside else None)
    bar.append(f"  {tokens(used)} of {tokens(limit)} ({100 * used / limit if limit else 0:.0f}%)", style="bold")
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="leo.muted")
    rows.add_column(justify="right")
    rows.add_column()
    for label, size in parts.items():
        rows.add_row(label, tokens(size), meter(size / limit if limit else 0, 20, g, "leo.accent"))
    rows.add_row("free", tokens(max(limit - used, 0)), Text(""))
    if not transcript:
        note = "No task in this session yet: the context starts with L.E.O.'s instructions and tool definitions."
    elif inside:
        compacted = state.get("compactions", 0)
        note = (f"At {tokens(compact_at)} (the │ mark) older steps are summarized to make room"
                + (f"; summarized {compacted} time{'s' if compacted != 1 else ''} so far." if compacted else "; not needed yet."))
    else:
        note = ""
    if limit and not inside:
        note += (" " if note else "") + (f"This model's window is smaller than the point where L.E.O. summarizes older steps "
                                         f"({tokens(compact_at)}), so a long task can fill it first; lower Context tokens in Settings "
                                         "when you use this model for long tasks.")
    return Group(head, Text(""), bar, Text(""), rows, Text(""), Text(note + " Sizes are estimates (about 3 bytes a token).", style="leo.muted"))
