"""L.E.O. as an MCP server (stdio), so agents like Claude Code can hand tasks to your routed models.

It talks to a running L.E.O. API, so runs, routing, budgets, and history behave exactly as in the app.

    claude mcp add leo -- uv run --directory /path/to/L.E.O python -m backend.mcp_server

Environment: LEO_API_URL (default http://127.0.0.1:8000) and LEO_API_TOKEN when the server requires one.
Messages are newline-delimited JSON-RPC 2.0 on stdin/stdout; logs go to stderr only.
"""
import json
import os
import sys
import time
import uuid

import httpx

VERSION = "0.2.0"
TERMINAL = {"completed", "failed", "cancelled"}
MODES = ["auto", "quick", "reviewed", "deep"]

TOOLS = [
    {"name": "leo_ask",
     "description": "Send a request to L.E.O. Its routing head picks the skill, difficulty, mode, and the cheapest capable model "
                    "for each task, then returns the answer with the routing and token usage. Pass conversation_id to continue a chat.",
     "inputSchema": {"type": "object", "required": ["prompt"], "properties": {
         "prompt": {"type": "string", "description": "The request, as you would type it in the L.E.O. chat."},
         "mode": {"type": "string", "enum": MODES, "description": "auto (default) lets L.E.O. choose; quick is one call; reviewed adds a review; deep plans and splits tasks."},
         "conversation_id": {"type": "string", "description": "Continue an existing L.E.O. conversation."},
         "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 3600, "description": "Stop waiting after this long (the run keeps going in L.E.O.)."}}}},
    {"name": "leo_route",
     "description": "Preview how L.E.O. would route a request (skill, difficulty, mode, ranked models) without calling any model or spending tokens.",
     "inputSchema": {"type": "object", "required": ["prompt"], "properties": {
         "prompt": {"type": "string"}, "mode": {"type": "string", "enum": MODES}}}},
    {"name": "leo_usage",
     "description": "Token, call, cost, and context usage per model, overall or for one conversation.",
     "inputSchema": {"type": "object", "properties": {"conversation_id": {"type": "string"}}}},
    {"name": "leo_models",
     "description": "List the model connections L.E.O. can route to, with their skills, strength tier, and whether they spend hidden reasoning tokens.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _check(response):
    if response.is_success:
        return response.json()
    try:
        detail = response.json().get("detail", response.text)
    except ValueError:
        detail = response.text
    raise RuntimeError(f"L.E.O. API returned {response.status_code}: {detail}")


def _usage_line(usage):
    tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
    return (f"{usage.get('calls', 0)} calls, {'~' if usage.get('estimated') else ''}{tokens:,} tokens "
            f"({usage.get('input_tokens') or 0:,} in / {usage.get('output_tokens') or 0:,} out), est. ${usage.get('estimated_cost') or 0:.4f}")


def ask(client, args):
    mode = args.get("mode") or "auto"
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    body = {"prompt": args["prompt"], "mode": mode, "request_id": str(uuid.uuid4())}
    if args.get("conversation_id"):
        body["conversation_id"] = args["conversation_id"]
    run = _check(client.post("/api/runs", json=body))
    deadline = time.monotonic() + int(args.get("timeout_seconds") or 600)
    while run["status"] not in TERMINAL:
        if time.monotonic() > deadline:
            return (f"L.E.O. is still working (run {run['id']}, phase {run['phase']}). It continues in the background; "
                    f"check the app or call leo_usage with conversation_id {run['conversation_id']}."), False
        time.sleep(1.5)
        run = _check(client.get(f"/api/runs/{run['id']}"))
    usage = run.get("usage") or {}
    route = usage.get("route") or {}
    steps = ", ".join(f"{c['stage']} → {c['model']}" + ("" if c["status"] == "completed" else f" ({c['status']})") for c in run.get("calls", []))
    footer = [f"conversation_id: {run['conversation_id']}"]
    if route:
        footer.insert(0, f"Route: {route.get('skill')} · {route.get('difficulty')} → {route.get('mode')} mode" + (" (auto)" if route.get("auto") else ""))
    footer += [f"Models: {steps or 'none'}", f"Usage: {_usage_line(usage)}"]
    if usage.get("review_passed") is False:
        footer.append("Review: unresolved findings; inspect the answer.")
    if run["status"] != "completed":
        text = f"L.E.O. run {run['status']}: {run.get('error') or 'no detail'}"
        if run.get("output"):
            text += "\n\nPartial answer:\n" + run["output"]
        return text + "\n\n---\n" + "\n".join(footer), True
    return run["output"] + "\n\n---\n" + "\n".join(footer), False


def route(client, args):
    # Same default as leo_ask, so the preview predicts what leo_ask will do.
    result = _check(client.post("/api/route", json={"prompt": args["prompt"], "mode": args.get("mode") or "auto"}))
    lines = [f"Skill: {result['skill']} · difficulty: {result['difficulty']} · mode: {result['mode']}",
             f"Signals: {', '.join(result['signals']) or 'none'}; skills seen: {', '.join(result['skills']) or 'none'}",
             f"First choice: {result['candidates'][0]['model'] if result['candidates'] else 'no eligible model'} ({result['reason']})"]
    lines += [f"  {i + 1}. {c['name']} · {c['model']} · {c['tier']}{' · reasoning' if c['reasoning'] else ''} · skills: {', '.join(c['skills']) or 'none'}"
              for i, c in enumerate(result["candidates"])]
    if result.get("note"):
        lines.append(result["note"])
    return "\n".join(lines), False


def usage(client, args):
    params = {"conversation_id": args["conversation_id"]} if args.get("conversation_id") else {}
    report = _check(client.get("/api/usage", params=params))
    t = report["totals"]
    all_tokens = t["input_tokens"] + t["output_tokens"]
    pct = lambda part, whole: f"{100 * part / whole:.1f}%" if whole else "—"
    lines = [f"Total: {t['calls']} calls ({pct(t['completed'], t['calls'] - t.get('in_progress', 0))} completed), {all_tokens:,} tokens, est. ${t['cost']:.4f}"]
    for m in report["models"]:
        share = m["input_tokens"] + m["output_tokens"]
        roles = ", ".join(f"{k}×{v}" for k, v in m["roles"].items())
        lines.append(f"- {m['model']} ({m['provider']}): {pct(share, all_tokens)} of tokens, {share:,} tokens, {m['completed']}/{m['calls']} calls ok, roles {roles}")
    for chat in report["conversations"][:10]:
        c = chat.get("context")
        lines.append(f"- Chat '{chat['title'][:60]}': " + (f"context {c['percent']}% ({c['tokens']:,}/{c['allowance']:,})" if c else "no completed call"))
    return "\n".join(lines), False


def models(client, args):
    skills = {s["id"]: s["name"] for s in _check(client.get("/api/skills"))}
    rows = _check(client.get("/api/connections"))
    if not rows:
        return "No model connections yet. Add one in L.E.O. Settings.", False
    return "\n".join(f"- {c['name']} · {c['model']} · {c['tier']}{' · reasoning' if c['reasoning'] else ''} · "
                     f"{'local' if c['local'] else 'cloud'} · {'no API charge' if c['free'] else 'paid'}{'' if c['enabled'] else ' · disabled'} · "
                     f"skills: {', '.join(skills.get(s, s) for s in c['skills']) or 'generalist'}" for c in rows), False


HANDLERS = {"leo_ask": ask, "leo_route": route, "leo_usage": usage, "leo_models": models}


def handle(message, client):
    """Answer one JSON-RPC message; notifications get no reply."""
    method, rid = message.get("method"), message.get("id")
    if rid is None:
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": (message.get("params") or {}).get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "leo", "version": VERSION},
                      "instructions": "Use leo_ask to delegate work to the owner's local and free models through L.E.O.'s routing. "
                                      "Use leo_route to preview routing without spending tokens."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            handler = HANDLERS.get(params.get("name"))
            if not handler:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"Unknown tool: {params.get('name')}"}}
            try:
                text, failed = handler(client, params.get("arguments") or {})
            except (RuntimeError, ValueError, KeyError, httpx.HTTPError) as exc:
                text, failed = f"L.E.O. error: {exc}", True
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}
    except Exception as exc:  # Never crash the stdio loop on one bad request.
        print(f"leo-mcp: {exc!r}", file=sys.stderr)
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error; see L.E.O. MCP server logs"}}


def main():
    token = os.getenv("LEO_API_TOKEN", "")
    client = httpx.Client(base_url=os.getenv("LEO_API_URL", "http://127.0.0.1:8000"), timeout=30, trust_env=False,
                          headers={"Authorization": f"Bearer {token}"} if token else {})
    print(f"leo-mcp {VERSION} ready", file=sys.stderr)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = handle(message, client)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
