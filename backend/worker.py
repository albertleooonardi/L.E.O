"""Run with `python -m backend.worker`; jobs outlive API connections."""
import asyncio
import logging
import time
import uuid

from .config import Config
from .orchestrator import Orchestrator
from .sandbox import Sandbox
from .store import Store

logger = logging.getLogger("leo.worker")


async def process_one(store, owner, transport=None, sandbox=None, run_id=None):
    run = store.claim(owner, run_id=run_id)
    if not run:
        return False
    user_id = run.get("user_id")
    engine = Orchestrator(store, run, owner, transport, sandbox)
    task = asyncio.create_task(engine.execute())
    try:
        while not task.done():
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if done:
                break
            current = store.get_run(run["id"], user_id, True)
            if not current or not store.heartbeat(run["id"], owner, user_id):
                task.cancel()
                break
            if current["cancel_requested"]:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                engine = Orchestrator(store, current, owner, transport)
                store.finish(run["id"], owner, "cancelled", error="Stopped by user", usage=engine.usage(), user_id=user_id)
                return True
            # Agent runs count their time from the last approval, since waiting for the owner is not work.
            if time.time() - max(run["created_at"], run["state"].get("resumed_at") or 0) > engine.limits()[3]:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                engine = Orchestrator(store, current, owner, transport)
                store.finish(run["id"], owner, "failed", error="Run time limit reached", usage=engine.usage(), user_id=user_id)
                return True
        await asyncio.gather(task, return_exceptions=False)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return True


async def serve(store=None):
    config = store.config if store else Config()
    store = store or Store(config)
    sandbox = Sandbox(config.sandbox_url, config.sandbox_token) if config.sandbox_url else None
    owner = str(uuid.uuid4())
    logger.info("Worker ready" + ("" if sandbox else "; agent mode is off (LEO_SANDBOX_URL is not set)"))
    while True:
        try:
            if not await process_one(store, owner, sandbox=sandbox):
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker iteration failed")
            await asyncio.sleep(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
