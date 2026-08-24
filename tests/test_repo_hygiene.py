"""M0 safety net: properties of the repository itself, not of the running system.

Each M0 task adds its guard test to this file as it lands, so CI stays green
between tasks and every task leaves behind something that can fail later.
"""

from __future__ import annotations

import re
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


# --- Task 2: dependency and model pinning -------------------------------------

PIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]+\])?==[^=\s]+$")
PINNED_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


def _requirement_lines() -> list[str]:
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_every_requirement_is_pinned_to_an_exact_version():
    """AUDIT.md F-11: 20 requirement lines, zero version constraints."""
    unpinned = [line for line in _requirement_lines() if not PIN_RE.match(line)]
    assert unpinned == [], unpinned


def test_requirements_covers_the_whole_stack():
    names = {re.split(r"[\[=]", line)[0].lower() for line in _requirement_lines()}
    for required in [
        "fastapi",
        "streamlit",
        "sqlalchemy",
        "psycopg2-binary",
        "neo4j",
        "qdrant-client",
        "sentence-transformers",
        "torch",
        "transformers",
        "google-genai",
        "langgraph",
    ]:
        assert required in names, f"{required} missing from requirements.txt"


def test_installed_versions_match_the_pins():
    """The pins must describe an environment that actually exists."""
    from importlib.metadata import PackageNotFoundError, version

    dist_names = {"psycopg2-binary": "psycopg2-binary", "pyyaml": "PyYAML"}
    mismatches = []
    for line in _requirement_lines():
        name, pinned = line.split("==")
        name = re.split(r"\[", name)[0]
        try:
            installed = version(dist_names.get(name.lower(), name))
        except PackageNotFoundError:
            continue  # not installed in this environment; CI installs them all
        if installed != pinned:
            mismatches.append(f"{name}: pinned {pinned}, installed {installed}")
    assert mismatches == [], mismatches


def test_embedding_model_revision_is_pinned_in_env_example():
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^EMBEDDING_MODEL_REVISION=([0-9a-f]{40})$", text, re.MULTILINE)
    assert match is not None, "EMBEDDING_MODEL_REVISION must be a 40-char commit sha"
    assert match.group(1) == PINNED_MODEL_REVISION


def test_pinned_revision_matches_the_locally_cached_model():
    """Skipped where the HuggingFace cache is absent (e.g. CI).

    Locally this catches the case where the machine that produced the Qdrant
    collection is silently running a different snapshot from the pinned one.
    """
    ref = (
        Path.home()
        / ".cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/refs/main"
    )
    if not ref.exists():
        pytest.skip("HuggingFace cache for the embedding model is not present")
    assert ref.read_text(encoding="utf-8").strip() == PINNED_MODEL_REVISION


# --- Task 3: the data-cleaning stage is committed -----------------------------


def test_data_cleaning_script_is_committed():
    """AUDIT.md P4: stage 1 of the pipeline lived only in a gitignored notebook."""
    tracked = _tracked()
    assert "scripts/clean_data.py" in tracked
    assert "scripts/verify_clean_data.py" in tracked


# --- Task 4: no compiled bytecode, and the rule that let it in is gone --------


def test_no_compiled_bytecode_is_tracked():
    """AUDIT.md F-20: 8 .pyc files were committed."""
    tracked = [path for path in _tracked() if path.endswith((".pyc", ".pyo", ".pyd"))]
    assert tracked == [], tracked


@pytest.mark.parametrize(
    "probe",
    [
        "src/retrieval/__pycache__/hybrid_retriever.cpython-312.pyc",
        "src/api/__pycache__/main.cpython-312.pyc",
        "scripts/__pycache__/clean_data.cpython-312.pyc",
        "tests/__pycache__/conftest.cpython-312.pyc",
    ],
)
def test_bytecode_paths_are_ignored_everywhere(probe):
    result = subprocess.run(
        ["git", "check-ignore", "-v", probe],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{probe} is NOT ignored. check-ignore said: {result.stdout or result.stderr!r}"
    )


@pytest.mark.parametrize(
    "probe",
    [
        "database/postgres/data/base",
        "database/neo4j/data/databases",
        "database/qdrant/storage/collections",
    ],
)
def test_database_volume_directories_are_ignored(probe):
    """`!database/**` used to re-include these, one `git add -A` from committing
    a database volume."""
    result = subprocess.run(
        ["git", "check-ignore", "-v", probe],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"{probe} is NOT ignored. check-ignore said: {result.stdout or result.stderr!r}"
    )


def test_gitignore_has_no_blanket_negations():
    """A negation re-includes everything it matches, including bytecode."""
    lines = [
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("!") and not line.strip().startswith("#")
    ]
    blanket = [line for line in lines if line.endswith("/**") or line.endswith("/*")]
    assert blanket == [], blanket


# --- Task 5: the frozen baseline ---------------------------------------------


def test_baseline_and_its_tooling_are_committed():
    tracked = _tracked()
    for path in [
        "tests/baseline/baseline_results.json",
        "scripts/capture_baseline.py",
        "scripts/check_template_determinism.py",
    ]:
        assert path in tracked, path


# --- Task 6: the setup path is documented ------------------------------------


def test_setup_document_is_committed():
    assert "docs/SETUP.md" in _tracked()


def test_setup_documents_the_steps_the_readme_omits():
    """AUDIT.md P4 listed four undocumented prerequisites for a clean clone."""
    text = (PROJECT_ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")
    for required in [
        "kaggle.com/datasets/olistbr/brazilian-ecommerce",  # the raw download
        "scripts/clean_data.py",                            # the cleaning stage
        "src/synthetic/synthetic_data_generator.py",        # the missing pipeline step
        "database/postgres/schema.sql",                     # never invoked before
    ]:
        assert required in text, f"docs/SETUP.md does not mention {required}"


def test_test_dependency_is_pinned():
    """A clean clone resolved a different pytest than the author's machine until this
    was pinned (M0 task 6)."""
    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'test = \["pytest==[\d.]+"\]', text), text
