import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import uvicorn

from backend import mcp_server
from backend.api import create_app
from backend.schemas import Connection
from backend.worker import process_one
from .conftest import sse_response


def call(client, name, arguments, rid=1):
    return mcp_server.handle({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, client)


def test_protocol_handshake_and_errors(client):
    init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}, client)
    assert init["result"]["serverInfo"]["name"] == "leo" and "tools" in init["result"]["capabilities"]
    assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, client) is None
    names = {t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, client)["result"]["tools"]}
    assert names == {"leo_ask", "leo_route", "leo_usage", "leo_models"}
    assert mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"}, client)["error"]["code"] == -32601
    assert call(client, "leo_missing", {})["error"]["code"] == -32602
    failed = call(client, "leo_ask", {"prompt": "Hi"})["result"]
    assert failed["isError"] and "Add and enable a connection" in failed["content"][0]["text"]


def test_leo_ask_returns_answer_routing_and_usage(client, store, monkeypatch):
    store.save_connection(Connection(name='Web', base_url='http://web.test/v1', model='web-model', free=True, skills=['frontend']).model_dump())
    transport = httpx.MockTransport(lambda r: sse_response('```html\n<h1>Coffee</h1>\n```'))
    # The MCP tool polls the API; run the queued job when it waits.
    monkeypatch.setattr(mcp_server.time, "sleep", lambda _: asyncio.run(process_one(store, 'mcp-test', transport)))
    result = call(client, "leo_ask", {"prompt": "Write an HTML hero section", "mode": "quick"})["result"]
    text = result["content"][0]["text"]
    assert not result["isError"]
    assert "<h1>Coffee</h1>" in text and "Route: frontend" in text and "generating → web-model" in text and "conversation_id:" in text
    usage = call(client, "leo_usage", {})["result"]["content"][0]["text"]
    assert "web-model" in usage and "100.0% of tokens" in usage


def test_stdio_server_against_live_api(store):
    store.save_connection(Connection(name='Coder', base_url='http://coder.test/v1', model='coder', free=True, skills=['coding'], tier='standard').model_dump())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(store.config, store), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    env = {**os.environ, "LEO_API_URL": f"http://127.0.0.1:{port}"}
    process = subprocess.Popen([sys.executable, "-m", "backend.mcp_server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env, cwd=os.path.dirname(os.path.dirname(__file__)))
    try:
        def rpc(message):
            process.stdin.write(json.dumps(message) + "\n"); process.stdin.flush()
            return json.loads(process.stdout.readline()) if "id" in message else None
        assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})["result"]["serverInfo"]["version"] == mcp_server.VERSION
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert len(rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]) == 4
        preview = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "leo_route", "arguments": {"prompt": "Write a Python function to parse dates"}}})
        text = preview["result"]["content"][0]["text"]
        assert "Skill: coding" in text and "First choice: coder" in text
        listing = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "leo_models", "arguments": {}}})
        assert "Coder · coder · standard" in listing["result"]["content"][0]["text"]
    finally:
        process.stdin.close(); process.wait(timeout=5)
        server.should_exit = True; thread.join(timeout=5)
