"""Convenience launcher for local API + worker. Production runs them separately."""
import asyncio
import os

import uvicorn

from .api import create_app
from .config import Config
from .store import Store
from .worker import serve


async def main():
    config = Config()
    port = int(os.getenv('LEO_PORT', '8000'))
    for hostname in ('localhost', '127.0.0.1'):
        origin = f'http://{hostname}:{port}'
        if origin not in config.origins:
            config.origins.append(origin)
    api_store = Store(config)
    worker_store = Store(config)
    server = uvicorn.Server(uvicorn.Config(create_app(config, api_store), host="127.0.0.1", port=port, log_level="info"))
    async def ready_worker():
        # Local routers are restored in API startup before queued runs begin.
        while not server.started:
            await asyncio.sleep(.1)
        await serve(worker_store)

    worker = asyncio.create_task(ready_worker())
    try:
        await server.serve()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        worker_store.engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
