"""Shared fixtures.

The modules under src/ are executed as scripts and import their siblings by bare
module name after patching sys.path themselves. Tests import them the same way so
that what is under test is the code as it actually runs -- not a repackaged variant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"


def _add_src_dirs_to_syspath() -> None:
    for directory in sorted(p for p in SRC.iterdir() if p.is_dir() and p.name != "__pycache__"):
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))


_add_src_dirs_to_syspath()


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def baseline_path() -> Path:
    return PROJECT_ROOT / "tests" / "baseline" / "baseline_results.json"
