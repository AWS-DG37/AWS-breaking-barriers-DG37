"""Exercise the installed proxy with real Nginx and a disappearing upstream."""

import json
import shutil
import subprocess
import time
import uuid

import httpx
import pytest

from deploy.fieldecho import MAINTENANCE_PROXY, probe_maintenance_proxy

NGINX = "nginx@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236"


def test_missing_upstream_serves_maintenance_and_recovers_without_caching(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker is required for the real Nginx check")

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    name = "fieldecho-proxy-test-" + uuid.uuid4().hex
    (tmp_path / "proxy.conf").write_text(MAINTENANCE_PROXY)
    (tmp_path / "backend.conf").write_text("""events {} http { server {
      listen 8877;
      location / { return 200 'healthy'; }
      location /api/health { return 401; }
    }}""")
    try:
        docker(
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1::80",
            "--mount",
            f"type=bind,src={tmp_path},dst=/test,readonly",
            "--entrypoint",
            "nginx",
            NGINX,
            "-c",
            "/test/proxy.conf",
            "-g",
            "daemon off;",
        )
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"]["80/tcp"][0][
            "HostPort"
        ]
        base = "http://127.0.0.1:" + port

        def wait_for(status):
            deadline = time.monotonic() + 10
            while True:
                try:
                    response = httpx.get(base, timeout=1)
                    if response.status_code == status:
                        return response
                except httpx.HTTPError:
                    pass
                assert time.monotonic() < deadline, docker("logs", name)
                time.sleep(0.05)

        wait_for(503)
        probe_maintenance_proxy(int(port))
        for method, path in [("GET", "/"), ("GET", "/talk/"), ("POST", "/api/jobs")]:
            response = httpx.request(method, base + path, timeout=2)
            assert response.status_code == 503
            assert response.headers["retry-after"] == "10"
            assert response.headers["cache-control"] == "no-store"
            assert "Field Echo is temporarily unavailable" in response.text
            assert "502 Bad Gateway" not in response.text
            assert "refresh" not in response.text.lower()  # Never replay a failed POST.

        docker(
            "run",
            "-d",
            "--name",
            name + "-backend",
            "--network",
            "container:" + name,
            "--mount",
            f"type=bind,src={tmp_path},dst=/test,readonly",
            "--entrypoint",
            "nginx",
            NGINX,
            "-c",
            "/test/backend.conf",
            "-g",
            "daemon off;",
        )
        assert wait_for(200).text == "healthy"
        probe_maintenance_proxy(int(port))
        assert httpx.get(base + "/api/health").status_code == 401
        assert "retry-after" not in httpx.get(base).headers

        docker("stop", "--time", "2", name + "-backend")
        assert wait_for(503).headers["cache-control"] == "no-store"
    finally:
        subprocess.run(["docker", "rm", "-f", name + "-backend"], capture_output=True, timeout=30)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
