"""Deterministic local fixture for manual QA of connectors; never a production service.

Serves two things on one port:
- /mcp: an MCP server (Streamable HTTP) for a pretend issue tracker with create_issue and search_issues.
- /v1: an OpenAI-compatible model that calls a *create_issue tool when the request mentions an issue, then reports
  what the tool returned. Without tools it just answers.
"""
import json
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
ISSUES = []
TOOLS = [
    {"name": "create_issue", "description": "Create an issue in the tracker.",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}},
    {"name": "search_issues", "description": "Find issues by text.", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
]


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    if "id" not in body:
        return Response(status_code=202)
    method, ident = body["method"], body["id"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock-tracker", "version": "1"}}
        return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": result}, headers={"Mcp-Session-Id": "qa-session"})
    if method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = body["params"]["name"], body["params"].get("arguments") or {}
        if name == "create_issue":
            ISSUES.append(args.get("title", "Untitled"))
            text = f"Created QA-{len(ISSUES)}: {ISSUES[-1]}"
        else:
            found = [f"QA-{n + 1}: {t}" for n, t in enumerate(ISSUES) if str(args.get("q", "")).lower() in t.lower()]
            text = "\n".join(found) or "No issues found"
        result = {"content": [{"type": "text", "text": text}]}
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"Unknown method {method}"}})
    return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": result})


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-tools", "object": "model"}]}


def stream(events):
    def chunks():
        for event in events:
            yield "data: " + json.dumps(event) + "\n\n"
            time.sleep(0.05)
        yield "data: [DONE]\n\n"
    return StreamingResponse(chunks(), media_type="text/event-stream")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages, tools = body["messages"], body.get("tools") or []
    results = [m for m in messages if m["role"] == "tool"]
    usage = {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 30}}
    create = next((t["function"]["name"] for t in tools if t["function"]["name"].endswith("__create_issue")), None)
    asked = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if create and not results and "issue" in str(asked).lower():
        title = str(asked).split(":", 1)[-1].strip()[:80] or "New issue"
        call = {"index": 0, "id": "call_qa_1", "type": "function", "function": {"name": create, "arguments": json.dumps({"title": title})}}
        return stream([{"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": None}]},
                       {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}, usage])
    text = f"Done. The tracker says: {results[-1]['content']}" if results else "Hello from the mock tool model."
    words = [w + " " for w in text.split(" ")]
    return stream([{"choices": [{"delta": {"content": w}, "finish_reason": None}]} for w in words] +
                  [{"choices": [{"delta": {}, "finish_reason": "stop"}]}, usage])
