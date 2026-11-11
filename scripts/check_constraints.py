"""Check that what users install is exactly what the lock file pins.

`uvx --from <source>` ignores `uv.lock`, so the plugin ships `constraints.txt`, exported from
the lock. This check fails when `constraints.txt` differs from a fresh export of `uv.lock`, or
when a direct dependency in `pyproject.toml` is not pinned with `==` to its locked version.

Run from the repository root: `uv run python scripts/check_constraints.py`."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

EXPORT_COMMAND = [
    "uv",
    "export",
    "--frozen",
    "--no-hashes",
    "--no-dev",
    "--no-emit-project",
    "--format",
    "requirements-txt",
]
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;]+)\s*(;.*)?$")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_lines(text: str) -> list[str]:
    """The requirement lines of a requirements file, without comments or blank lines."""
    lines = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return [line for line in lines if line]


def compare_export(exported: str, committed: str) -> list[str]:
    fresh, shipped = requirement_lines(exported), requirement_lines(committed)
    problems = [f"constraints.txt lacks: {line}" for line in fresh if line not in shipped]
    problems += [f"constraints.txt has extra: {line}" for line in shipped if line not in fresh]
    return problems


def locked_versions(lock_text: str) -> dict[str, str]:
    lock = tomllib.loads(lock_text)
    return {
        normalise(p["name"]): str(p["version"]) for p in lock.get("package", []) if "version" in p
    }


def check_pins(pyproject_text: str, lock_text: str) -> list[str]:
    """Every direct dependency is pinned with == to the version in the lock."""
    project = tomllib.loads(pyproject_text)
    direct = list(project.get("project", {}).get("dependencies", []))
    for group in project.get("dependency-groups", {}).values():
        direct += [item for item in group if isinstance(item, str)]
    locked = locked_versions(lock_text)
    problems = []
    for requirement in direct:
        match = _PIN.match(requirement)
        if match is None:
            problems.append(f"not pinned with ==: {requirement}")
            continue
        name, version = normalise(match[1]), match[2]
        if locked.get(name) != version:
            problems.append(f"{name}=={version} but uv.lock has {locked.get(name)}")
    return problems


def main(root: Path | None = None) -> int:
    base = root or Path.cwd()
    exported = subprocess.run(
        EXPORT_COMMAND, cwd=base, capture_output=True, text=True, check=True
    ).stdout
    problems = compare_export(exported, (base / "constraints.txt").read_text())
    problems += check_pins((base / "pyproject.toml").read_text(), (base / "uv.lock").read_text())
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(
            "Regenerate with: uv export --no-hashes --no-dev --no-emit-project "
            "--format requirements-txt -o constraints.txt",
            file=sys.stderr,
        )
        return 1
    print("constraints.txt and the == pins match uv.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
