"""The retained evaluator must work on a teammate's fresh checkout."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_reference_validation_uses_the_tracked_pack_without_legacy_data(tmp_path):
    root = Path(__file__).resolve().parents[1]
    package = tmp_path / "dg_agent"
    package.mkdir()
    (package / "__init__.py").touch()
    shutil.copy(root / "dg_agent/models.py", package / "models.py")
    shutil.copytree(
        root / "docs/participant-pack/json/schemas",
        tmp_path / "docs/participant-pack/json/schemas",
    )
    payload = {
        "insight_id": "I-test",
        "farmer_id": "F-test",
        "decision": "no_survey",
        "reason_code": "generic_information",
        "reason": "No action was advised.",
    }
    command = (
        "import json; from dg_agent.models import validate; "
        f"print(json.dumps(validate('no_survey', {payload!r})))"
    )
    result = subprocess.run(
        [sys.executable, "-c", command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_installed_reference_package_reads_the_working_project_pack(tmp_path):
    root = Path(__file__).resolve().parents[1]
    library = tmp_path / "site-packages" / "dg_agent"
    library.mkdir(parents=True)
    (library / "__init__.py").touch()
    for name in ["models.py", "checks.py"]:
        shutil.copy(root / "dg_agent" / name, library / name)
    project = tmp_path / "project"
    shutil.copytree(
        root / "docs/participant-pack/json/rules", project / "docs/participant-pack/json/rules"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dg_agent.checks import rules; print(rules()['send_hours']['from'])",
        ],
        cwd=project,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(library.parent)},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "07:00"
