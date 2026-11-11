"""Exercise real child-process lifecycle and storage isolation without downloading or contacting providers."""
import asyncio
import json
import socket
import sys

import httpx
import pytest

from backend.local_router import LocalRouters
from backend.ninerouter import RouterError


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    entry = tmp_path / 'fake_router.py'
    entry.write_text('''import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
home = Path(os.environ['DATA_DIR'])
(home / 'saved-account').touch()
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
 def log_message(self, *args): pass
class Server(HTTPServer): allow_reuse_address = True
Server((os.environ['HOSTNAME'], int(os.environ['PORT'])), Handler).serve_forever()
''')
    monkeypatch.setattr('backend.local_router.shutil.which', lambda name: sys.executable)
    return tmp_path, entry


async def test_local_routers_isolate_users_and_preserve_data_across_restart(runtime):
    home, entry = runtime
    manager = LocalRouters(home, entry)
    try:
        a, b = await asyncio.gather(manager.ensure_router('alice', 'a'), manager.ensure_router('bob', 'b'))
        assert a != b and a.startswith('http://127.0.0.1:')
        process = manager.processes['alice']
        assert await manager.ensure_router('alice', 'a') == a
        assert manager.processes['alice'] is process, 'refresh must reuse the running child'
        (home / 'routers/alice/saved-account').write_text('Alice account')
        assert (home / 'routers/bob/saved-account').read_text() == ''
    finally:
        await manager.close()
    assert process.returncode is not None
    fresh = LocalRouters(home, entry)
    try:
        assert await fresh.ensure_router('alice', 'a') == a, 'saved model URLs must survive restart'
        assert (home / 'routers/alice/saved-account').read_text() == 'Alice account'
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(a + '/api/health')).is_success
    finally:
        await fresh.close()


async def test_busy_saved_port_is_never_adopted_as_a_router(runtime):
    home, entry = runtime
    manager = LocalRouters(home, entry)
    with socket.socket() as other:
        other.bind(('127.0.0.1', 0))
        other.listen()
        port = other.getsockname()[1]
        (manager.directory('alice') / 'endpoint.json').write_text(json.dumps({'port': port}))
        with pytest.raises(RouterError, match='busy'):
            await manager.ensure_router('alice', 'a')
    assert manager.processes == {}


async def test_local_router_rejects_paths_outside_its_data_directory(runtime):
    home, entry = runtime
    manager = LocalRouters(home, entry)
    with pytest.raises(RouterError, match='user ID'):
        await manager.ensure_router('../escape', 'a')
