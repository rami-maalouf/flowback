"""app-owned engine: drain work after the app's lifetime pipe closes."""

import asyncio
import fcntl
import logging
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn

from .server import ACTIVE_PHASES, create_app

LOGGER = logging.getLogger(__name__)


class ManagedLifetime:
    def __init__(self, app, *, grace=10):
        self.app = app
        self.grace = grace
        self.disconnected = threading.Event()
        self.requests = 0
        self.last_request = time.monotonic()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        self.requests += 1
        try:
            return await self.app(scope, receive, send)
        finally:
            self.requests -= 1
            self.last_request = time.monotonic()

    def should_stop(self):
        if (
            not self.disconnected.is_set()
            or self.requests
            or time.monotonic() - self.last_request < self.grace
            or not hasattr(self.app.state, "store")
        ):
            return False
        store = self.app.state.store
        with store.lock:
            if any(job["snapshot"]["phase"] in ACTIVE_PHASES for job in store.jobs.values()):
                return False
            if any(
                item["phase"] in {"queued", "separating"}
                for item in store.extractions.items.values()
            ):
                return False
        with store.assessments.lock:
            return not any(
                record["result"]["status"] in {"queued", "running"}
                for record in store.assessments.records.values()
            )

    def watch_owner(self):
        # eof also arrives after a force quit; no quit-time request can be lost.
        sys.stdin.buffer.read()
        self.disconnected.set()
        LOGGER.info("app disconnected; waiting for processing and local clients to finish")

    async def monitor(self, server):
        while not server.should_exit:
            await asyncio.sleep(1)
            if server.started and self.should_stop():
                LOGGER.info("managed engine idle; shutting down")
                server.should_exit = True


async def serve(port=8891):
    lifetime = ManagedLifetime(create_app())
    server = uvicorn.Server(uvicorn.Config(lifetime, host="127.0.0.1", port=port, access_log=False))
    threading.Thread(target=lifetime.watch_owner, daemon=True, name="app-lifetime").start()
    monitor = asyncio.create_task(lifetime.monitor(server))
    try:
        await server.serve()
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


def main():
    root = Path(os.environ.get("EXACT_COVER_DATA_DIR", Path.home() / ".local/share/exact-cover"))
    root.mkdir(parents=True, exist_ok=True)
    # serialize app launches before loading the library or recovering interrupted jobs.
    with (root / ".managed-engine.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        asyncio.run(serve())


if __name__ == "__main__":
    main()
