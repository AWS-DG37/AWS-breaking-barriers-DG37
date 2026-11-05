"""Public deployment evidence, refreshed lazily from one fixed GitHub workflow."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

REPOSITORY = "AWS-DG37/AWS-breaking-barriers-DG37"
RUNS_URL = (
    f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/main.yml/runs"
    "?branch=main&per_page=20"
)
REFRESH_SECONDS = 90
MAX_BODY_BYTES = 1_000_000
SHA = re.compile(r"[0-9a-f]{40}\Z")


def read_image_revision(path: Path = Path("/app/REVISION")) -> str | None:
    try:
        with path.open() as stream:
            value = stream.read(43)
    except (OSError, UnicodeError):
        return None
    return value.rstrip("\n") if re.fullmatch(r"[0-9a-f]{40}\n?", value) else None


@dataclass(frozen=True)
class Run:
    id: int
    number: int
    attempt: int
    sha: str
    state: str
    conclusion: str | None


def latest_run(payload: object) -> Run:
    if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
        raise ValueError("Invalid workflow response")
    runs = payload["workflow_runs"]
    if len(runs) > 20:
        raise ValueError("Unbounded workflow response")
    eligible = []
    for row in runs:
        if not isinstance(row, dict):
            raise ValueError("Invalid workflow run")
        if row.get("event") not in {"push", "workflow_dispatch"}:
            continue
        if row.get("head_branch") != "main":
            continue
        repository = row.get("repository")
        if not isinstance(repository, dict) or not isinstance(repository.get("full_name"), str):
            raise ValueError("Invalid workflow repository")
        if repository["full_name"] != REPOSITORY:
            continue
        if any(
            type(row.get(key)) is not int or row[key] <= 0
            for key in ("id", "run_number", "run_attempt")
        ):
            raise ValueError("Invalid workflow order")
        eligible.append(row)
    if not eligible:
        raise ValueError("No eligible workflow run")
    row = max(eligible, key=lambda run: (run["run_number"], run["run_attempt"]))
    sha, status, conclusion = row.get("head_sha"), row.get("status"), row.get("conclusion")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise ValueError("Invalid workflow revision")
    if status == "completed":
        if conclusion not in {
            "success",
            "failure",
            "neutral",
            "cancelled",
            "skipped",
            "timed_out",
            "action_required",
            "stale",
            "startup_failure",
        }:
            raise ValueError("Invalid workflow conclusion")
        state = "success" if conclusion == "success" else "failed"
    elif status in {"queued", "requested", "waiting", "pending", "in_progress"}:
        if conclusion is not None:
            raise ValueError("Invalid active workflow conclusion")
        state = "running" if status == "in_progress" else "queued"
    else:
        raise ValueError("Invalid workflow status")
    return Run(row["id"], row["run_number"], row["run_attempt"], sha, state, conclusion)


class DeploymentStatus:
    def __init__(
        self,
        console_url: str | None,
        serving_sha: str | None,
        *,
        transport=None,
        clock=time.monotonic,
        wall_clock=time.time,
    ):
        self.enabled = console_url == "https://fieldecho.org"
        self.serving_sha = (
            serving_sha if isinstance(serving_sha, str) and SHA.fullmatch(serving_sha) else None
        )
        self.transport = transport
        self.clock = clock
        self.wall_clock = wall_clock
        self._lock = asyncio.Lock()
        self._next_refresh = 0.0
        self._run: Run | None = None
        self._failures = 0

    async def snapshot(self) -> dict:
        if not self.enabled:
            return {"state": "disabled"}
        if self.serving_sha is None:
            return {"state": "unknown"}
        if self.clock() >= self._next_refresh:
            async with self._lock:
                if self.clock() >= self._next_refresh:
                    await self._refresh()
        run = self._run
        if run is None:
            return {"state": "unknown"}
        state = run.state
        if state == "success":
            state = "current" if run.sha == self.serving_sha else "unconfirmed"
        return {
            "state": state,
            "serving_sha": self.serving_sha,
            "target_sha": run.sha,
            "run_id": run.id,
            "run_number": run.number,
            "run_attempt": run.attempt,
            "conclusion": run.conclusion,
        }

    async def _refresh(self):
        delay = REFRESH_SECONDS
        try:
            # One overall timeout also bounds slow streaming, not just each socket read.
            async with (
                asyncio.timeout(5),
                httpx.AsyncClient(
                    transport=self.transport,
                    timeout=5,
                    follow_redirects=False,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "FieldEcho-deployment-status",
                    },
                ) as client,
            ):
                async with client.stream("GET", RUNS_URL) as response:
                    if response.status_code in {403, 429}:
                        delay = max(delay, self._retry_delay(response.headers))
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BODY_BYTES:
                            raise ValueError("Workflow response too large")
                    self._run = latest_run(json.loads(body))
            self._failures = 0
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError, TypeError):
            # Failed fresh evidence cannot preserve a previously green observation.
            self._run = None
            self._failures = min(self._failures + 1, 6)
            delay = max(delay, min(3600, REFRESH_SECONDS * 2 ** (self._failures - 1)))
        self._next_refresh = self.clock() + delay

    def _retry_delay(self, headers: httpx.Headers) -> float:
        delays = [0.0]
        retry = headers.get("retry-after", "")
        try:
            delays.append(float(retry))
        except ValueError:
            try:
                delays.append(parsedate_to_datetime(retry).timestamp() - self.wall_clock())
            except (ValueError, TypeError, OverflowError):
                pass
        try:
            delays.append(float(headers.get("x-ratelimit-reset", "0")) - self.wall_clock())
        except ValueError:
            pass
        return min(3600, max(delays))
