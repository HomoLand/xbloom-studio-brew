"""Create an external per-user runtime and install pinned BLE dependencies."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import venv

from xbloom_paths import (
    RUNTIME_DIR_ENV,
    environment_copy,
    runtime_python_path,
    skill_runtime_dir,
)


ROOT = Path(__file__).resolve().parents[1]


def venv_python(runtime: Path | None = None) -> Path:
    return runtime_python_path(skill_runtime_dir() if runtime is None else runtime)


def run(args: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", action="store_true", help="also install pytest and run tests")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="override the external runtime directory for this bootstrap",
    )
    args = parser.parse_args()
    runtime = (args.runtime_dir or skill_runtime_dir()).expanduser().resolve()

    if not venv_python(runtime).exists():
        print(f"Creating external runtime at {runtime}")
        venv.EnvBuilder(with_pip=True).create(runtime)

    requirement = ROOT / ("requirements-dev.txt" if args.dev else "requirements.txt")
    python = str(venv_python(runtime))
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirement)])
    runtime_env = environment_copy()
    runtime_env[RUNTIME_DIR_ENV] = str(runtime)
    run(
        [python, str(ROOT / "scripts" / "xbloom.py"), "doctor"],
        env=runtime_env,
    )
    if args.dev:
        runtime_env["PYTHONPATH"] = str(ROOT / "scripts")
        subprocess.run(
            [python, "-m", "pytest", "-q"], cwd=ROOT, env=runtime_env, check=True
        )

    if args.runtime_dir is not None:
        print(
            f"Persist {RUNTIME_DIR_ENV}={runtime} for future CLI and bridge calls."
        )
    print("Bootstrap complete. Run: python scripts/xbloom.py scan")


if __name__ == "__main__":
    main()
