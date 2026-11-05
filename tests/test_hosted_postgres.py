import os
import time
from uuid import uuid4

import pytest
from postgres import requires_postgres

from mel.config import Settings
from mel.store import Store

pytestmark = requires_postgres


def test_queue_lease_is_exclusive_and_probe_requires_this_owner():
    from mel.explorer.hosted import lease_owned

    settings = Settings(
        database_port=int(os.environ["MEL_TEST_DB_PORT"]), database_sslmode="disable"
    )
    first, second = Store(settings), Store(settings)
    try:
        first.initialize()
        first.acquire_hosted_lease("fieldecho:container-first:1")
        assert lease_owned(second.connection, "fieldecho:container-first:1")
        assert not lease_owned(second.connection, "fieldecho:container-second:1")
        with pytest.raises(RuntimeError, match="runner"):
            second.acquire_hosted_lease("fieldecho:container-second:1")
        first.close()
        deadline = time.monotonic() + 2
        while lease_owned(second.connection, "fieldecho:container-first:1"):
            assert time.monotonic() < deadline, "closed backend retained its lease"
            time.sleep(0.01)
        second.acquire_hosted_lease("fieldecho:container-second:1")
        assert lease_owned(second.connection, "fieldecho:container-second:1")
    finally:
        first.close()
        second.close()


def test_ordinary_queue_does_not_resume_running_job(database):
    import asyncio

    from mel.jobs import run_queue

    job = database.create_job(str(uuid4()), "batch", {})
    database.claim_job(job["job_id"])
    assert asyncio.run(run_queue(database, Settings(), job_id=job["job_id"])) == 0
    assert database.job(job["job_id"])["status"] == "running"


@pytest.mark.parametrize("outcome", ["return", "error", "signal"])
def test_real_server_fails_closed_or_drains_on_signal(tmp_path, outcome):
    import signal
    import socket
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    trigger, closed = tmp_path / "trigger", tmp_path / "closed"
    script = """
import asyncio, os
from pathlib import Path
from mel.config import Settings
from mel.store import Store
from mel.explorer import hosted
trigger, closed = Path(os.environ['TEST_TRIGGER']), Path(os.environ['TEST_CLOSED'])
class OwnedStore(Store):
    def close(self):
        super().close()
        closed.write_text('closed')
async def queue(store, settings, **kwargs):
    while not trigger.exists():
        await asyncio.sleep(.01)
    if os.environ['TEST_OUTCOME']=='error':
        store.connection.close()
        store.claim_job()
    return 0
original=hosted.HostedRunner
hosted.HostedRunner=lambda s: original(
    s,store_factory=OwnedStore,queue_runner=queue,drain_seconds=.05)
s=Settings(database_port=int(os.environ['MEL_TEST_DB_PORT']), database_sslmode='disable',
    auth_domain='https://signin.example', auth_client_id='console-client',
    auth_issuer='https://issuer.example/pool', console_url='https://console.example')
asyncio.run(hosted.serve(s,host='127.0.0.1',port=int(os.environ['TEST_PORT'])))
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "TEST_TRIGGER": str(trigger),
            "TEST_CLOSED": str(closed),
            "TEST_PORT": str(port),
            "TEST_OUTCOME": outcome,
        },
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.2)
            except urllib.error.HTTPError as error:
                assert error.code == 401
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("server did not become ready: " + process.communicate()[1].decode())
                time.sleep(0.02)
        if outcome == "signal":
            process.send_signal(signal.SIGTERM)
        else:
            trigger.write_text("finish")
        _, stderr = process.communicate(timeout=5)
        expected = (0, -signal.SIGTERM) if outcome == "signal" else (1,)
        assert process.returncode in expected, stderr.decode()
        assert closed.read_text() == "closed"
        with pytest.raises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.2)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_cancelled_active_job_preserves_unknown_cost_and_running_claim(database, monkeypatch):
    import asyncio
    from decimal import Decimal

    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.function import FunctionModel
    from test_costs_postgres import capped_run, setup_run

    from mel import jobs
    from mel.costs import CapUpdate, CostedModel, cap_view, lookup, report, set_cap

    _, model, _ = setup_run(database, budget=None)
    reservation = lookup(database, model, "us-east-1")[1].reservation(model.max_tokens)
    set_cap(
        database,
        CapUpdate(expected_version=cap_view(database)["version"], budget_usd=reservation * 2),
    )
    costs = capped_run(database)
    job = database.create_job(str(uuid4()), "batch", {})

    async def check():
        active = asyncio.Event()

        async def provider(*args):
            active.set()
            await asyncio.Event().wait()

        metered = CostedModel(FunctionModel(provider), costs, model, "us-east-1", "generate")

        async def run(*args, **kwargs):
            await metered.request([], {}, ModelRequestParameters())

        monkeypatch.setattr(jobs, "run_job", run)
        task = asyncio.create_task(jobs.run_queue(database, Settings(), job_id=job["job_id"]))
        await active.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(check())
    record = report(database, costs.run_id, limit=1)
    assert record["calls"][0]["state"] == "unknown"
    assert Decimal(cap_view(database)["committed_usd"]) == reservation
    assert database.job(job["job_id"])["status"] == "running"


def test_duplicate_supervisor_never_becomes_ready():
    import asyncio

    from mel.explorer.hosted import HostedRunner

    async def idle(store, settings, *, stop):
        await stop.wait()

    async def check():
        settings = Settings(
            database_port=int(os.environ["MEL_TEST_DB_PORT"]),
            database_sslmode="disable",
            auth_domain="https://signin.example",
            auth_client_id="console-client",
            auth_issuer="https://issuer.example/pool",
            console_url="https://console.example",
        )
        first = HostedRunner(settings, queue_runner=idle)
        second = HostedRunner(settings, queue_runner=idle)
        try:
            await first.start()
            with pytest.raises(RuntimeError, match="runner"):
                await second.start()
            assert first.ready and not second.ready and second.failed
        finally:
            await second.stop()
            await first.stop()

    asyncio.run(check())
