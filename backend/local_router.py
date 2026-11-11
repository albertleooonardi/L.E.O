"""Per-user 9Router child processes for personal installs. Docker deployments keep using sandboxd."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
from pathlib import Path

from .ninerouter import NineRouter, RouterError


class LocalRouters:
    def __init__(self, data_dir, entry):
        self.home = Path(data_dir) / 'routers'
        self.entry = Path(entry).resolve()
        self.processes = {}
        self.locks = {}

    def directory(self, uid):
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,64}', uid):
            raise RouterError('Invalid local router user ID')
        directory = self.home / uid
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    async def ensure_router(self, uid, password):
        async with self.locks.setdefault(uid, asyncio.Lock()):
            directory = self.directory(uid)
            endpoint = directory / 'endpoint.json'
            try:
                port = json.loads(endpoint.read_text())['port']
                if type(port) is not int or not 1024 <= port <= 65535:
                    raise ValueError('Invalid saved port')
            except FileNotFoundError:
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1', 0))
                    port = listener.getsockname()[1]
                endpoint.write_text(json.dumps({'port': port}) + '\n')
            except (ValueError, KeyError, TypeError) as exc:
                raise RouterError(f'Invalid local router endpoint file: {endpoint}') from exc
            url = f'http://127.0.0.1:{port}'
            process = self.processes.get(uid)
            if process and process.returncode is None:
                return url
            node = shutil.which('node')
            if not node or not self.entry.is_file():
                raise RouterError('Local model router is not installed. Run ./setup.sh, then ./start.sh.')
            # Never attach to an unrelated service that happens to occupy our saved port.
            try:
                with socket.socket() as listener:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    listener.bind(('127.0.0.1', port))
            except OSError as exc:
                raise RouterError(f'Local model router port {port} is busy. Stop the other process and restart L.E.O.') from exc
            env = {**os.environ, 'DATA_DIR': str(directory), 'PORT': str(port), 'HOSTNAME': '127.0.0.1',
                   'BASE_URL': url, 'NEXT_PUBLIC_BASE_URL': url, 'NODE_ENV': 'production',
                   'INITIAL_PASSWORD': password, 'REQUIRE_API_KEY': 'true', 'ENABLE_REQUEST_LOGS': 'false'}
            for key in ('JWT_SECRET', 'API_KEY_SECRET', 'MACHINE_ID_SALT'):
                env[key] = hashlib.sha256(f'{key}:{password}'.encode()).hexdigest()
            try:
                with (directory / 'router.log').open('ab') as log:
                    process = await asyncio.create_subprocess_exec(node, str(self.entry), env=env,
                                                                   stdout=log, stderr=asyncio.subprocess.STDOUT)
                self.processes[uid] = process
                await NineRouter(url, password).wait_ready()
                if process.returncode is not None:
                    raise RouterError('The local model router exited during startup.')
            except asyncio.CancelledError:
                await self.delete_router(uid)
                raise
            except (RouterError, OSError):
                await self.delete_router(uid)
                raise RouterError(f'Local model router could not start. See {directory / "router.log"}.') from None
            return url

    async def delete_router(self, uid):
        """Stop the child; preserve its credentials and database for the next launch."""
        process = self.processes.pop(uid, None)
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), 5)
            except ProcessLookupError:
                pass
            except TimeoutError:
                process.kill()
                await process.wait()

    async def close(self):
        await asyncio.gather(*(self.delete_router(uid) for uid in list(self.processes)))
