"""Container build hygiene (M8 Task 1).

These assert the three properties the rebuild plan names for M8 -- multi-stage,
non-root, a verified .dockerignore -- plus the two that make them stick: the runtime
stage must not carry a compiler, and it must not `COPY . .`.

Parsed from the Dockerfiles rather than checked against a built image, so they run in
seconds and fail on the pull request rather than after a deploy. The one property that
genuinely needs a built image -- that the process really runs as uid 10001 -- is checked
by scripts/verify_container.py and reported in docs/M8_FINDINGS.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DOCKERFILES = {
    "api": PROJECT_ROOT / "Dockerfile.api",
    "ui": PROJECT_ROOT / "Dockerfile.ui",
    "demo": PROJECT_ROOT / "Dockerfile.demo",
}

# The rebuild plan names these explicitly. `.env` and reports/ are the two that would
# actually hurt: one is credentials, the other is every answer the LLM has produced.
REQUIRED_IGNORES = (
    "reports/",
    "tests/eval/results/",
    ".env",
    "*.pyc",
    "__pycache__/",
    ".git",
)

COMPILER_PACKAGES = ("build-essential", "gcc", "g++", "make", "cmake")


def instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, argument) pairs, with line continuations joined."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    parsed = []

    for line in joined.splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        parts = stripped.split(None, 1)
        parsed.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))

    return parsed


def runtime_stage(text: str) -> list[tuple[str, str]]:
    """Everything after the final FROM -- the stage that actually ships."""
    parsed = instructions(text)
    starts = [i for i, (op, _) in enumerate(parsed) if op == "FROM"]

    assert starts, "no FROM instruction"

    return parsed[starts[-1]:]


@pytest.fixture(params=sorted(DOCKERFILES), ids=sorted(DOCKERFILES))
def dockerfile(request):
    path = DOCKERFILES[request.param]

    assert path.exists(), f"{path.name} is missing"

    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------
# Multi-stage
# --------------------------------------------------------------------------------


def test_the_build_is_multi_stage(dockerfile):
    stages = [arg for op, arg in instructions(dockerfile) if op == "FROM"]

    assert len(stages) >= 2, (
        f"single-stage build: {stages}. Everything the build needed ships to "
        f"production, including the compiler."
    )


def test_the_runtime_stage_carries_no_compiler(dockerfile):
    """A toolchain in a runtime image is weight and it is surface.

    The version this replaces installed build-essential in the only stage it had,
    so a shell in that container had a working GCC.
    """
    offenders = [
        (op, arg)
        for op, arg in runtime_stage(dockerfile)
        if op == "RUN" and any(package in arg for package in COMPILER_PACKAGES)
    ]

    assert not offenders, offenders


def test_the_runtime_stage_does_not_copy_the_whole_context(dockerfile):
    """`COPY . .` is how a build context leak becomes an image layer.

    .dockerignore is the other half of this control; both are kept, because a COPY
    that widens later should not be able to leak on its own.
    """
    offenders = [
        arg
        for op, arg in runtime_stage(dockerfile)
        if op == "COPY" and re.match(r"^(--\S+\s+)*\.\s+\.?/?$", arg.strip())
    ]

    assert not offenders, f"COPY of the entire context: {offenders}"


# --------------------------------------------------------------------------------
# Non-root
# --------------------------------------------------------------------------------


def test_the_container_does_not_run_as_root(dockerfile):
    stage = runtime_stage(dockerfile)
    users = [arg.strip() for op, arg in stage if op == "USER"]

    assert users, "no USER instruction: the container runs as root"

    final = users[-1]

    assert not final.startswith(("root", "0:", "0 ")), f"USER {final}"
    assert final != "0"


def test_the_user_switch_happens_before_the_entrypoint(dockerfile):
    """A USER above a later RUN or COPY is decoration, not a control."""
    stage = runtime_stage(dockerfile)
    positions = [i for i, (op, _) in enumerate(stage) if op == "USER"]

    assert positions

    after = [
        (op, arg)
        for op, arg in stage[positions[-1]:]
        if op in {"RUN", "COPY", "ADD"}
    ]

    assert not after, f"privileged instructions after the USER switch: {after}"


# --------------------------------------------------------------------------------
# .dockerignore
# --------------------------------------------------------------------------------


def test_dockerignore_excludes_secrets_results_and_caches():
    path = PROJECT_ROOT / ".dockerignore"

    assert path.exists(), ".dockerignore is missing"

    entries = {
        line.strip().rstrip("/")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    missing = [
        required
        for required in REQUIRED_IGNORES
        if required.rstrip("/") not in entries
    ]

    assert not missing, f"not excluded from the build context: {missing}"


# --------------------------------------------------------------------------------
# The two requirement files must not drift
# --------------------------------------------------------------------------------


def parse_pins(path: Path) -> dict[str, str]:
    pins = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            continue

        match = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]+\])?==(\S+)$", stripped)

        if match:
            pins[match.group(1).lower()] = match.group(3)

    return pins


# Anything that could reach a model or a database. The UI talks HTTP to the API and
# the demo API replays a JSON file; neither has a use for any of these.
MODEL_AND_DATABASE_STACK = {
    "torch", "transformers", "sentence-transformers", "sqlalchemy",
    "psycopg2-binary", "neo4j", "qdrant-client", "google-genai", "langgraph",
}


@pytest.mark.parametrize("filename", ["requirements-ui.txt", "requirements-demo.txt"])
def test_the_derived_requirements_do_not_drift_from_the_api_requirements(filename):
    """Three requirement files is three chances to pin different versions.

    Every package named in both a derived file and requirements.txt must carry the
    same pin, or the images stop being the same system.
    """
    api = parse_pins(PROJECT_ROOT / "requirements.txt")
    derived = parse_pins(PROJECT_ROOT / filename)

    shared = set(api) & set(derived)

    assert shared, f"{filename} shares no packages with requirements.txt"

    drift = {name: (api[name], derived[name]) for name in shared
             if api[name] != derived[name]}

    assert not drift, f"version drift between requirements.txt and {filename}: {drift}"


@pytest.mark.parametrize("filename", ["requirements-ui.txt", "requirements-demo.txt"])
def test_the_derived_images_do_not_install_the_model_stack(filename):
    """For requirements-demo.txt this is the M8 Task 3 exit criterion.

    "Zero LLM calls and zero database queries" is enforced by not installing anything
    that could make one. If a package from this set ever appears in the demo image,
    the claim stops being structural and becomes a promise about a code path.
    """
    present = MODEL_AND_DATABASE_STACK & set(parse_pins(PROJECT_ROOT / filename))

    assert not present, f"{filename} installs packages it never imports: {present}"
