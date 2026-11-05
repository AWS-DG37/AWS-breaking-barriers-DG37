"""Exercise the installed proxy with real Nginx and a disappearing upstream."""

import json
import shutil
import subprocess
import time
import uuid

import httpx
import pytest

from deploy import fieldecho
from deploy.fieldecho import MAINTENANCE_PROXY, probe_maintenance_proxy

NGINX = "nginx@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236"


@pytest.mark.parametrize("layout", ["bootstrap", "included"])
def test_missing_upstream_serves_maintenance_and_recovers_without_caching(tmp_path, layout):
    if not shutil.which("docker"):
        pytest.skip("Docker is required for the real Nginx check")

    def docker(*args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    name = "fieldecho-proxy-test-" + uuid.uuid4().hex
    config = MAINTENANCE_PROXY
    if layout == "included":
        config = fieldecho.INCLUDED_PROXY_ROOT.replace(
            "/etc/nginx/conf.d/*.conf", "/test/site.conf"
        )
        (tmp_path / "site.conf").write_text(fieldecho.MAINTENANCE_SITE)
    (tmp_path / "proxy.conf").write_text(config)
    (tmp_path / "backend.conf").write_text("""events {} http { server {
      listen 8877;
      location / { return 200 'healthy'; }
      location /api/health { return 401; }
      location /api/authorization { return 200 '[$http_authorization]'; }
      location /api/unavailable {
        default_type application/json;
        return 503 '{"detail":"Specific application error"}';
      }
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
            if layout == "included":
                assert response.headers["x-content-type-options"] == "nosniff"
                assert response.headers["referrer-policy"] == "same-origin"

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
        error = httpx.get(base + "/api/unavailable")
        assert error.status_code == 503
        assert error.json() == {"detail": "Specific application error"}
        assert "retry-after" not in error.headers
        assert "retry-after" not in httpx.get(base).headers
        if layout == "included":
            healthy = httpx.get(base)
            assert healthy.headers["x-content-type-options"] == "nosniff"
            assert healthy.headers["referrer-policy"] == "same-origin"
            assert (
                httpx.get(
                    base + "/api/authorization", headers={"Authorization": "test-authorization"}
                ).text
                == "[]"
            )
            assert httpx.post(base, content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413

        docker("stop", "--time", "2", name + "-backend")
        assert wait_for(503).headers["cache-control"] == "no-store"
    finally:
        subprocess.run(["docker", "rm", "-f", name + "-backend"], capture_output=True, timeout=30)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
