"""Run with `python -m backend.worker`; jobs outlive API connections."""
import asyncio
import logging
import time
import uuid

from .config import Config
from .orchestrator import Orchestrator
from .store import Store

logger = logging.getLogger("leo.worker")


async def process_one(store, owner, transport=None):
    run = store.claim(owner)
    if not run:
        return False
    user_id = run.get("user_id")
    task = asyncio.create_task(Orchestrator(store, run, owner, transport).execute())
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
            if time.time() - run["created_at"] > run["state"]["preferences"]["max_run_seconds"]:
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
    store = store or Store(Config())
    owner = str(uuid.uuid4())
    logger.info("Worker ready")
    while True:
        try:
            if not await process_one(store, owner):
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
