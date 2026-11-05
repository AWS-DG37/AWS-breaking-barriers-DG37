from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager

from mel.config import Settings
from mel.jobs import run_queue
from mel.store import Store

logger = logging.getLogger("mel.explorer.hosted")


def runner_owner() -> str:
    return f"fieldecho:{socket.gethostname()}:{os.getpid()}"


def lease_owned(connection, owner: str) -> bool:
    return connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
        "WHERE l.locktype='advisory' AND l.granted AND l.classid=0 AND l.objid=31890242 "
        "AND l.objsubid=1 AND a.datname=current_database() AND a.application_name=%s)",
        (owner,),
    ).fetchone()[0]


class HostedRunner:
    def __init__(
        self, settings: Settings, *, store_factory=Store, queue_runner=run_queue, drain_seconds=25
    ):
        if not all(
            (
                settings.auth_domain,
                settings.auth_client_id,
                settings.auth_issuer,
                settings.console_url,
            )
        ):
            raise ValueError("hosted jobs require complete Cognito authentication")
        if settings.job_cluster is not None:
            raise ValueError("hosted jobs cannot dispatch to ECS")
        self.settings = settings
        self.store_factory = store_factory
        self.queue_runner = queue_runner
        self.drain_seconds = drain_seconds
        self.ready = False
        self.failed = False
        self.server = None
        self.task = None
        self.stopping = asyncio.Event()
        self.initialized = asyncio.Event()

    async def _run(self):
        store = None

        def initialize():
            nonlocal store
            store = self.store_factory(self.settings)
            store.initialize()
            store.acquire_hosted_lease(runner_owner())

        initialization = asyncio.create_task(asyncio.to_thread(initialize))
        try:
            try:
                await asyncio.shield(initialization)
            except asyncio.CancelledError:
                await initialization
                raise
            if self.stopping.is_set():
                return
            self.ready = True
            self.initialized.set()
            await self.queue_runner(store, self.settings, stop=self.stopping)
        finally:
            self.ready = False
            if store is not None:
                store.close()

    def _exited(self, task):
        self.ready = False
        if not self.stopping.is_set():
            self.failed = True
            logger.error("Hosted queue stopped unexpectedly; shutting down explorer")
            if self.server is not None:
                self.server.should_exit = True
        if not task.cancelled():
            task.exception()
        self.initialized.set()

    async def start(self):
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._exited)
        try:
            await self.initialized.wait()
            if not self.ready:
                await self.task
                raise RuntimeError("hosted runner did not become ready")
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        self.ready = False
        self.stopping.set()
        if self.task is None:
            return
        done, _ = await asyncio.wait([self.task], timeout=self.drain_seconds)
        if not done:
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    @asynccontextmanager
    async def lifespan(self, app):
        await self.start()
        try:
            yield
        finally:
            await self.stop()


async def serve(settings: Settings, *, host: str, port: int):
    import uvicorn

    from mel.explorer.api import create_app

    runner = HostedRunner(settings)
    app = create_app(
        settings=settings, admission_check=lambda: runner.ready, lifespan=runner.lifespan
    )

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            runner.ready = False
            runner.stopping.set()
            super().handle_exit(sig, frame)

    runner.server = Server(uvicorn.Config(app, host=host, port=port, timeout_graceful_shutdown=5))
    try:
        await runner.server.serve()
    finally:
        await runner.stop()
    if runner.failed:
        raise SystemExit(1)
