"""Create a skill-local runtime and install pinned BLE dependencies."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(args: list[str]) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="also install pytest and run tests")
    args = parser.parse_args()

    if not venv_python().exists():
        print(f"Creating local runtime at {VENV}")
        venv.EnvBuilder(with_pip=True).create(VENV)

    requirement = ROOT / ("requirements-dev.txt" if args.dev else "requirements.txt")
    python = str(venv_python())
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirement)])
    run([python, str(ROOT / "scripts" / "xbloom.py"), "doctor"])
    if args.dev:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "scripts")
        subprocess.run(
            [python, "-m", "pytest", "-q"], cwd=ROOT, env=env, check=True
        )

    print("Bootstrap complete. Run: python scripts/xbloom.py scan")


if __name__ == "__main__":
    main()
