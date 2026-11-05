import asyncio
import threading
from types import SimpleNamespace

import pytest

from mel.config import Settings
from mel.jobs import run_queue

AUTH = dict(
    auth_domain="https://signin.example",
    auth_client_id="console-client",
    auth_issuer="https://issuer.example/pool",
    console_url="https://console.example",
)


class QueueStore:
    def __init__(self, settings=None):
        self.closed = False
        self.claims = 0
        self.leased = None

    def initialize(self):
        pass

    def acquire_hosted_lease(self, owner):
        self.leased = owner

    def claim_job(self, *args, **kwargs):
        assert not self.closed
        self.claims += 1
        return None

    def close(self):
        self.closed = True


def test_stop_prevents_claim_and_wakes_idle_queue():
    async def check():
        store = QueueStore()
        stop = asyncio.Event()
        stop.set()
        assert await run_queue(store, Settings(), stop=stop) == 0
        assert store.claims == 0
        stop.clear()
        task = asyncio.create_task(run_queue(store, Settings(), stop=stop, poll_seconds=60))
        while not store.claims:
            await asyncio.sleep(0)
        stop.set()
        assert await asyncio.wait_for(task, 1) == 0

    asyncio.run(check())


def test_cancel_awaits_threaded_claim_and_stop_after_claim_never_runs_job(monkeypatch):
    from mel import jobs

    async def check(cancel):
        entered, release = threading.Event(), threading.Event()
        stop = asyncio.Event()
        store = QueueStore()

        def claim(*args, **kwargs):
            entered.set()
            assert release.wait(3)
            assert not store.closed
            return {"job_id": "claimed"}

        async def unexpected(*args, **kwargs):
            pytest.fail("job started after stop")

        store.claim_job = claim
        monkeypatch.setattr(jobs, "run_job", unexpected)
        task = asyncio.create_task(run_queue(store, Settings(), stop=stop))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            stop.set()
            if cancel:
                task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            release.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                assert await task == 0
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            store.close()

    asyncio.run(check(False))
    asyncio.run(check(True))


@pytest.mark.parametrize("failure", [False, True])
def test_runner_exit_closes_admission_and_stops_actual_server(failure):
    from mel.explorer.hosted import HostedRunner

    async def check():
        finish = asyncio.Event()
        store = QueueStore()

        async def queue(*args, **kwargs):
            await finish.wait()
            if failure:
                raise RuntimeError("queue lost")
            return 0

        runner = HostedRunner(Settings(**AUTH), store_factory=lambda _: store, queue_runner=queue)
        runner.server = SimpleNamespace(should_exit=False)
        await runner.start()
        assert runner.ready and store.leased
        finish.set()
        await asyncio.gather(runner.task, return_exceptions=True)
        await asyncio.sleep(0)
        assert not runner.ready and runner.failed
        assert runner.server.should_exit
        assert store.closed
        await runner.stop()

    asyncio.run(check())


def test_hosted_start_requires_auth_and_rejects_ecs():
    from mel.explorer.hosted import HostedRunner

    with pytest.raises(ValueError, match="Cognito"):
        HostedRunner(Settings())
    for cluster in ("other", ""):
        with pytest.raises(ValueError, match="ECS"):
            HostedRunner(Settings(**AUTH, job_cluster=cluster))


def test_start_waits_for_initialization_and_cancellation_never_closes_under_thread():
    from mel.explorer.hosted import HostedRunner

    async def check():
        entered, release = threading.Event(), threading.Event()
        store = QueueStore()

        def initialize():
            entered.set()
            assert release.wait(3)
            assert not store.closed

        store.initialize = initialize
        runner = HostedRunner(Settings(**AUTH), store_factory=lambda _: store)
        start = asyncio.create_task(runner.start())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not runner.ready and not start.done()
            start.cancel()
            await asyncio.sleep(0.02)
            assert not store.closed
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await start
        finally:
            release.set()
            await asyncio.gather(start, return_exceptions=True)
            await runner.stop()
        assert store.closed

    asyncio.run(check())


def test_shutdown_bounds_active_job_drain_and_keeps_interrupted_work(monkeypatch):
    from mel import jobs
    from mel.explorer.hosted import HostedRunner

    async def check():
        active = asyncio.Event()
        store = QueueStore()

        def claim(*args, **kwargs):
            store.claims += 1
            return {"job_id": "running"}

        async def job(*args, **kwargs):
            active.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert not store.closed

        store.claim_job = claim
        monkeypatch.setattr(jobs, "run_job", job)
        runner = HostedRunner(Settings(**AUTH), store_factory=lambda _: store, drain_seconds=0.01)
        await runner.start()
        await active.wait()
        await asyncio.wait_for(runner.stop(), 1)
        assert not runner.ready and not runner.failed
        assert store.closed and store.claims == 1

    asyncio.run(check())


def test_offline_flag_wins_over_integrated_runner(monkeypatch):
    import uvicorn

    from mel.explorer import api

    calls = []
    monkeypatch.setattr("sys.argv", ["mel-explorer", "--run-jobs", "--no-live"])
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(app))
    api.main()
    assert calls
    from fastapi.testclient import TestClient

    with TestClient(calls[0]) as client:
        assert (
            client.post(
                "/api/jobs/batch", json={"dataset_id": "00000000-0000-0000-0000-000000000001"}
            ).status_code
            == 403
        )
