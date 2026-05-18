"""Lint checks. Every tracked .py file under defmon_driver/ and tests/
must pass `ruff check` AND `ruff format --check`.

The project's pyproject.toml configures ruff (line-length 100, ruleset
"E,F,I,W,B"); this test enforces that configuration on every commit.

We invoke the `ruff` binary as a subprocess rather than importing
ruff's Python API because (a) ruff is not a stable Python library and
(b) the binary is what developers run locally — keeping the test on the
same code path avoids surprises where the API and the CLI disagree.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Scope: the importable package + the unit-test suite. CLI smoke scripts
# under defmon_driver/ are included (they live in the package). The
# excluded directories (.venv, build, etc.) are skipped by ruff itself
# via its default exclude list.
LINT_DIRS = ("defmon_driver", "tests")


def _python_files() -> list[Path]:
    """All project .py paths under LINT_DIRS, excluding generated dirs."""
    skip_dirs = {".git", "__pycache__", "build", "dist", ".venv", "venv"}
    files: list[Path] = []
    for top in LINT_DIRS:
        top_path = REPO_ROOT / top
        if not top_path.is_dir():
            continue
        for path in top_path.rglob("*.py"):
            if any(part in skip_dirs for part in path.relative_to(REPO_ROOT).parts):
                continue
            files.append(path)
    return sorted(files)


def _ruff_subprocess_env() -> dict[str, str]:
    """Strip pytest-cov subprocess-injection env vars before spawning ruff.
    Otherwise ruff inherits ``COV_CORE_*`` and pytest-cov's sitecustomize
    hook fires inside the ruff subprocess, importing pygments which may
    not be installed and which would make the subprocess exit non-zero
    with an error that masquerades as a lint failure."""
    return {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}


class TestRuffLint(unittest.TestCase):
    """Run ``ruff check`` and ``ruff format --check`` against the project."""

    def setUp(self) -> None:
        ruff = shutil.which("ruff")
        if ruff is None:
            self.skipTest("ruff not installed")
        self.ruff: str = ruff
        self.files = _python_files()
        self.assertGreater(len(self.files), 0, "no .py files discovered")

    def test_ruff_check_clean(self) -> None:
        result = subprocess.run(
            [self.ruff, "check", *(str(p) for p in self.files)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=_ruff_subprocess_env(),
        )
        if result.returncode != 0:
            offenders = result.stdout.strip() or result.stderr.strip()
            self.fail("ruff check failed; run `ruff check --fix` to fix:\n" + offenders)

    def test_ruff_format_clean(self) -> None:
        result = subprocess.run(
            [self.ruff, "format", "--check", *(str(p) for p in self.files)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=_ruff_subprocess_env(),
        )
        if result.returncode != 0:
            offenders = result.stdout.strip() or result.stderr.strip()
            self.fail("ruff format --check failed; run `ruff format` to fix:\n" + offenders)


if __name__ == "__main__":
    unittest.main()
