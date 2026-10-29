"""Convenience launcher for local API + worker. Production runs them separately."""
import asyncio

import uvicorn

from .api import create_app
from .config import Config
from .store import Store
from .worker import serve


async def main():
    config = Config()
    api_store = Store(config)
    worker_store = Store(config)
    server = uvicorn.Server(uvicorn.Config(create_app(config, api_store), host="127.0.0.1", port=8000, log_level="info"))
    worker = asyncio.create_task(serve(worker_store))
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
