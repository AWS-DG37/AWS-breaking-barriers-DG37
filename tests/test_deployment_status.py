"""Offline deployment observations: no AWS, database, or real HTTP."""

import asyncio
from pathlib import Path

import httpx
import pytest

from mel.explorer.deployment_status import DeploymentStatus, read_image_revision

SHA = "a" * 40
OTHER = "b" * 40
REPO = "AWS-DG37/AWS-breaking-barriers-DG37"


def run(number=1, *, sha=SHA, status="completed", conclusion="success", **changes):
    return dict(
        id=number,
        run_number=number,
        run_attempt=1,
        head_sha=sha,
        head_branch="main",
        event="push",
        status=status,
        conclusion=conclusion,
        repository={"full_name": REPO},
        **changes,
    )


def service(runs, *, console="https://fieldecho.org", revision=SHA, clock=None):
    calls = []

    async def handle(request):
        calls.append(request)
        await asyncio.sleep(0)
        value = runs() if callable(runs) else runs
        if isinstance(value, Exception):
            raise value
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, json={"workflow_runs": value})

    return DeploymentStatus(
        console, revision, transport=httpx.MockTransport(handle), clock=clock or (lambda: 0)
    ), calls


def snapshot(svc):
    return asyncio.run(svc.snapshot())


@pytest.mark.parametrize(
    ("status", "conclusion", "sha", "expected"),
    [
        ("queued", None, SHA, "queued"),
        ("in_progress", None, SHA, "running"),
        ("completed", "failure", SHA, "failed"),
        ("completed", "cancelled", SHA, "failed"),
        ("completed", "timed_out", SHA, "failed"),
        ("completed", "success", OTHER, "unconfirmed"),
        ("completed", "success", SHA, "current"),
    ],
)
def test_lifecycle_even_when_serving_same_revision(status, conclusion, sha, expected):
    svc, _ = service([run(status=status, conclusion=conclusion, sha=sha)])
    result = snapshot(svc)
    assert result["state"] == expected
    assert result["serving_sha"] == SHA
    assert result["target_sha"] == sha
    assert result["run_id"] == 1
    assert set(result) == {
        "state",
        "serving_sha",
        "target_sha",
        "run_id",
        "run_number",
        "run_attempt",
        "conclusion",
    }


def test_latest_intent_and_attempt_not_old_rerun_or_pr():
    old = run(1)
    old["run_attempt"] = 20
    newer = run(2, status="queued", conclusion=None)
    attempt = run(2, status="in_progress", conclusion=None)
    attempt["run_attempt"] = 2
    ignored = run(3)
    ignored["event"] = "pull_request"
    svc, calls = service([old, newer, attempt, ignored])
    result = snapshot(svc)
    assert (result["state"], result["run_number"], result["run_attempt"]) == ("running", 2, 2)
    assert str(calls[0].url) == (
        "https://api.github.com/repos/AWS-DG37/AWS-breaking-barriers-DG37/"
        "actions/workflows/main.yml/runs?branch=main&per_page=20"
    )
    assert "authorization" not in calls[0].headers


@pytest.mark.parametrize("console", [None, "", "http://127.0.0.1:8000", "https://evil.org"])
def test_noncanonical_is_disabled_with_zero_requests(console):
    svc, calls = service([], console=console)
    assert snapshot(svc) == {"state": "disabled"}
    assert not calls


def test_missing_canonical_revision_is_unknown():
    svc, calls = service([run()], revision=None)
    assert snapshot(svc) == {"state": "unknown"}
    assert not calls


def test_singleflight_cache_expiry_failure_and_recovery():
    async def scenario():
        now = [0]
        wire = [[run()]]
        svc, calls = service(lambda: wire[0], clock=lambda: now[0])
        results = await asyncio.gather(*(svc.snapshot() for _ in range(100)))
        assert all(r["state"] == "current" for r in results)
        assert len(calls) == 1
        now[0] = 89
        await svc.snapshot()
        assert len(calls) == 1
        now[0] = 90
        wire[0] = httpx.ConnectError("private host diagnostic")
        assert await svc.snapshot() == {"state": "unknown"}
        assert len(calls) == 2
        now[0] = 179
        assert await svc.snapshot() == {"state": "unknown"}
        assert len(calls) == 2
        now[0] = 180
        wire[0] = [run(status="queued", conclusion=None)]
        assert (await svc.snapshot())["state"] == "queued"
        assert len(calls) == 3

    asyncio.run(scenario())


def test_retry_after_and_rate_reset_backoff():
    for headers in ({"Retry-After": "300"}, {"X-RateLimit-Reset": "1300"}):
        now = [0]
        svc, calls = service(httpx.Response(429, headers=headers), clock=lambda: now[0])
        svc.wall_clock = lambda: 1000
        assert snapshot(svc) == {"state": "unknown"}
        now[0] = 299
        snapshot(svc)
        assert len(calls) == 1
        now[0] = 300
        snapshot(svc)
        assert len(calls) == 2


@pytest.mark.parametrize(
    "runs", [[], [run(sha="invalid")], [run(status="alien")], [run(conclusion=None)]]
)
def test_unusable_evidence_never_clears_banner(runs):
    svc, _ = service(runs)
    assert snapshot(svc) == {"state": "unknown"}


def test_newest_malformed_run_does_not_fall_back_to_old_success():
    svc, _ = service([run(), run(2, sha="bad")])
    assert snapshot(svc) == {"state": "unknown"}


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b" " * 1_000_001),
        httpx.Response(302, headers={"location": "https://evil.org"}),
    ],
)
def test_bad_bounded_or_redirected_upstream_response(response):
    svc, calls = service(response)
    assert snapshot(svc) == {"state": "unknown"}
    assert len(calls) == 1


def test_revision_file_is_strict_and_not_environment(tmp_path, monkeypatch):
    path = tmp_path / "REVISION"
    monkeypatch.setenv("FIELDECHO_REVISION", OTHER)
    assert read_image_revision(path) is None
    path.write_text(SHA + "\n")
    assert read_image_revision(path) == SHA
    path.write_text("bad")
    assert read_image_revision(path) is None


def test_public_get_only_boundary_and_no_settings_leak():
    from fastapi.testclient import TestClient

    from mel.config import Settings
    from mel.explorer.api import create_app

    app = create_app(
        settings=Settings(
            auth_domain="https://signin.example",
            auth_client_id="client",
            auth_issuer="https://issuer.example",
            console_url="https://example.org",
        )
    )
    with TestClient(app) as client:
        response = client.get("/api/deployment-status")
        assert response.status_code == 200
        assert response.json() == {"state": "disabled"}
        assert response.headers["cache-control"] == "no-store"
        for path in ("/api/deployment-status/private", "/api/config"):
            assert client.get(path).status_code == 401
        assert client.post("/api/deployment-status").status_code == 401


def test_three_entry_pages_load_one_shared_banner_and_image_stamp():
    root = Path(__file__).parents[1]
    for page in ("static/index.html", "static/sign-in.html", "talk/index.html"):
        text = (root / "src/mel/explorer" / page).read_text()
        assert "/static/deployment-banner.css" in text
        assert "/static/deployment-banner.mjs" in text
    docker = (root / "Dockerfile").read_text()
    assert "ARG FIELDECHO_REVISION" in docker
    assert "/app/REVISION" in docker
    workflow = (root / ".github/workflows/main.yml").read_text()
    assert '--build-arg FIELDECHO_REVISION="$GITHUB_SHA"' in workflow
    assert "docker exec fieldecho-smoke cat /app/REVISION" in workflow


def test_revision_rejects_trailing_content_past_read_limit(tmp_path):
    path = tmp_path / "REVISION"
    path.write_text(SHA + "\n\nforeign")
    assert read_image_revision(path) is None


def test_response_count_is_bounded_to_twenty():
    svc, _ = service([run(n) for n in range(1, 22)])
    assert snapshot(svc) == {"state": "unknown"}


def test_malformed_relevant_newer_repository_does_not_clear_banner():
    malformed = run(2)
    del malformed["repository"]
    svc, _ = service([run(), malformed])
    assert snapshot(svc) == {"state": "unknown"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("event", "pull_request"),
        ("head_branch", "other"),
        ("repository", {"full_name": "foreign/repository"}),
    ],
)
def test_foreign_workflow_evidence_does_not_supersede_main(field, value):
    ignored = run(2, status="in_progress", conclusion=None)
    ignored[field] = value
    svc, _ = service([run(), ignored])
    assert snapshot(svc)["state"] == "current"
