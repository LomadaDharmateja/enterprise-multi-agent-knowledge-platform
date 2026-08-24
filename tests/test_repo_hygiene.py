"""M0 safety net: properties of the repository itself, not of the running system.

Each M0 task adds its guard test to this file as it lands, so CI stays green
between tasks and every task leaves behind something that can fail later.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _tracked() -> list[str]:
    return _git("ls-files").splitlines()


# --- Task 1: packaging and CI -------------------------------------------------


def test_every_src_package_has_an_init():
    missing = [
        str(d.relative_to(PROJECT_ROOT))
        for d in [SRC, *SRC.iterdir()]
        if d.is_dir() and d.name != "__pycache__" and not (d / "__init__.py").exists()
    ]
    assert missing == [], missing


def test_pyproject_configures_pytest():
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in text


def test_ci_workflow_is_committed_and_runs_pytest():
    workflows = [
        f
        for f in _tracked()
        if f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml"))
    ]
    assert workflows, "no committed GitHub Actions workflow"
    assert any(
        "pytest" in (PROJECT_ROOT / w).read_text(encoding="utf-8") for w in workflows
    ), "no committed workflow actually invokes pytest"


@pytest.mark.parametrize("path", ["README.md", "REBUILD_PLAN.md", "AUDIT.md", "PROJECT_SUMMARY.md"])
def test_key_documents_are_tracked(path):
    assert path in _tracked(), f"{path} is not committed"


def test_no_real_secret_in_env_example():
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("GEMINI_API_KEY="):
            value = line.split("=", 1)[1].strip()
            assert not value.startswith("AIza"), "a real-looking Gemini key is in .env.example"


def test_dotenv_is_not_tracked():
    assert ".env" not in _tracked()
