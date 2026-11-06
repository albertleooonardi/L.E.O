"""Drawing the tree of agents: a few lines above the prompt while it works, every agent with its usage in /usage.

    ◆ Dots3-Note  reviewing                                  2.1K
    ├─ ✔ Auth module         Nemotron Lightning  accepted    3.4K
    │  ├─ ✔ Login route      North Mini          accepted     900
    │  └─ ⠹ Tests            North Mini          bash(pytest) 400
    └─ ○ Docs                                    waiting

The nodes come from the engine's `tree` events (backend/tree.py), and the usage from its call ledger.
"""
from prompt_toolkit.utils import get_cwidth
from rich.table import Table
from rich.text import Text

FINISHED = {"accepted", "failed", "not_reviewed"}
UNFINISHED_WHEN_STOPPED = {"waiting", "planning", "working", "in_review", "retrying"}
WORDS = {"in_review": "in review", "not_reviewed": "not reviewed"}
MAX_LINES = 10


def tokens(n):
    n = n or 0
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}K" if n >= 1000 else str(n)


def money(cost):
    cost = cost or 0
    return "free" if not cost else f"${cost:.4f}" if cost < 1 else f"${cost:.2f}"


def short_model(model):
    """`nvidia/nemotron-3-ultra:free` as `nemotron-3-ultra`."""
    return str(model or "").rsplit("/", 1)[-1].split(":", 1)[0]


def nodes_of(run):
    """The tree in a run's saved state."""
    return (((run or {}).get("state") or {}).get("agent") or {}).get("tree", {}).get("nodes") or {}


def agent_usage(rows):
    """The call ledger added up per agent, from stages `agent:<id>:<step>`; context summaries are `compact`."""
    usage = {}
    for row in rows:
        parts = str(row["stage"]).split(":")
        if parts[0] != "agent" or len(parts) < 2 or row["status"] != "completed":
            continue
        total = usage.setdefault(parts[1], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0})
        total["calls"] += 1
        total["input_tokens"] += row["input_tokens"] or 0
        total["output_tokens"] += row["output_tokens"] or 0
        total["cost"] += row["cost"] or 0
    return usage


def walk(nodes, g, keep=None):
    """Every agent below the root, depth first in creation order, with the connector text before its glyph."""
    children = {}
    for node in nodes.values():
        if node["id"] != "main" and (keep is None or node["id"] in keep):
            children.setdefault(node["parent"], []).append(node)

    def below(parent, lead):
        found = children.get(parent, [])
        for index, node in enumerate(found):
            last = index == len(found) - 1
            yield node, lead + (g.elbow if last else g.tee) + " "
            yield from below(node["id"], lead + ("   " if last else g.pipe))

    return list(below("main", ""))


def status_of(node, nodes, activity=None, run_status=None):
    """A node's state in words: what it is doing, or how it ended."""
    status = node["status"]
    if run_status in ("cancelled", "failed") and status in UNFINISHED_WHEN_STOPPED:
        return "stopped"
    if status == "planning" and any(n["parent"] == node["id"] and n["status"] == "in_review" for n in nodes.values()):
        return "reviewing"
    if status == "working" and activity and activity.get(node["id"]):
        return activity[node["id"]]
    return WORDS.get(status, status)


def glyph_of(node, g, frame):
    status = node["status"]
    if status == "accepted":
        return g.ok, "class:tb.ok"
    if status in ("failed", "not_reviewed"):
        return g.fail, "class:tb.error"
    if status == "retrying":
        return g.warn, "class:tb.autorun"
    if status in ("working", "planning"):
        return g.spinner[frame % len(g.spinner)], "class:tb.spin"
    if status == "in_review":
        return g.info, "class:tb.spin"
    return g.idle, "class:tb"


def cut(text, width, ellipsis="…"):
    """Text in at most `width` cells."""
    if get_cwidth(text) <= width:
        return text
    size = max(width - get_cwidth(ellipsis), 0)
    while text and get_cwidth(text) > size:
        text = text[:-1]
    return text + ellipsis if width >= get_cwidth(ellipsis) else ""


def line(left, right, width, g):
    """One row: `left` gives way so that `right`, the state, stays."""
    right = cut(right, max(width - 8, 8), g.ellipsis)
    room = width - get_cwidth(right) - 2 if right else width
    left = cut(left, max(room, 0), g.ellipsis)
    gap = width - get_cwidth(left) - get_cwidth(right)
    return left, " " * max(gap, 1 if right else 0), right


def right_side(node, nodes, usage, width, activity):
    used = usage.get(node["id"]) or {}
    total = (used.get("input_tokens") or 0) + (used.get("output_tokens") or 0)
    parts = [status_of(node, nodes, activity)]
    if width >= 70 and node["id"] != "main" and node.get("model"):
        parts.insert(0, short_model(node["model"])[:24])
    if total:
        parts.append(tokens(total))
    return "  ".join(parts)


def live(nodes, usage, width, height, g, frame, activity=None):
    """The tree as prompt_toolkit fragments, each row ending in a newline; nothing when the root is alone.

    It takes at most a third of the terminal and never more than MAX_LINES lines: finished branches collapse first, then
    waiting ones, and what still does not fit becomes `… N more`."""
    if len(nodes) < 2 or "main" not in nodes:
        return []
    limit = min(MAX_LINES, max(height // 3, 1))
    width = max(width - 1, 10)
    root = nodes["main"]
    children = [n for n in nodes.values() if n["id"] != "main"]

    def with_ancestors(wanted):
        keep = set()
        for node in wanted:
            while node and node["id"] != "main" and node["id"] not in keep:
                keep.add(node["id"])
                node = nodes.get(node["parent"])
        return keep

    def active(hidden):
        return with_ancestors([n for n in children if n["status"] not in hidden])

    summaries, keep = [], {n["id"] for n in children}
    if len(children) + 1 > limit:
        keep = active(FINISHED)
        done = sum(1 for n in children if n["id"] not in keep)
        if done:
            summaries.append((g.ok, "class:tb.ok", f"{done} finished"))
    if len(keep) + len(summaries) + 1 > limit:
        kept = active(FINISHED | {"waiting"})
        waiting = len(keep) - len(kept)
        if waiting:
            summaries.append((g.idle, "class:tb", f"{waiting} waiting"))
        keep = kept
    rows = walk(nodes, g, keep)
    out = []
    left, gap, right = line(f"{g.agent} {short_model(root.get('model'))}", right_side(root, nodes, usage, width, activity), width, g)
    out.append([("class:tb.current", left), ("", gap), ("class:tb", right + "\n")])
    shown = []
    for index, (glyph, style, words) in enumerate(summaries):
        last = index == len(summaries) - 1 and not rows
        shown.append([("class:tb", f"{g.elbow if last else g.tee} "), (style, glyph), ("class:tb", f" {words}\n")])
    for node, lead in rows:
        glyph, style = glyph_of(node, g, frame)
        left, gap, right = line(f"{lead}{glyph} {' '.join(str(node.get('task') or '').split())}", right_side(node, nodes, usage, width, activity), width, g)
        task = left[len(lead) + len(glyph):]
        shown.append([("class:tb", lead), (style, glyph), ("", task), ("", gap), ("class:tb", right + "\n")])
    if len(shown) + 1 > limit:
        hidden = len(shown) - max(limit - 2, 0)
        shown = shown[:max(limit - 2, 0)] + ([[("class:tb", f"{g.ellipsis} {hidden} more\n")]] if limit > 1 else [])
    for row in shown:
        out.append(row)
    return [fragment for row in out for fragment in row]


def full(nodes, usage, g, run_status=None):
    """Every agent with its model, state, calls, tokens, and cost; for /usage and the end of `leocode -p`."""
    table = Table(box=g.box, border_style="leo.border", header_style="leo.muted", title=Text("Agent tree of the latest task", style="leo.title"),
                  title_justify="left")
    for column, justify in (("Agent", "left"), ("Model", "left"), ("State", "left"), ("Calls", "right"), ("Tokens", "right"), ("Cost", "right")):
        table.add_column(column, justify=justify, no_wrap=column != "Agent")

    def row(label, node, used):
        used = used or {}
        total = (used.get("input_tokens") or 0) + (used.get("output_tokens") or 0)
        state = status_of(node, nodes, run_status=run_status) if node else ""
        style = "leo.ok" if state == "accepted" else "leo.error" if state in ("failed", "not reviewed") else "leo.warn" if state == "stopped" else "leo.muted"
        table.add_row(label, Text(short_model(node.get("model")) if node else "", style="leo.muted"), Text(state, style=style), str(used.get("calls", 0)),
                      tokens(total), money(used.get("cost")))

    root = nodes.get("main")
    if root:
        row(Text(f"{g.agent} {' '.join(str(root.get('task') or '').split())[:60]}", style="bold"), root, usage.get("main"))
    for node, lead in walk(nodes, g):
        row(Text(lead + " ".join(str(node.get("task") or "").split())[:80]), node, usage.get(node["id"]))
    if usage.get("compact"):
        row(Text("context summaries", style="leo.muted"), None, usage["compact"])
    return table
