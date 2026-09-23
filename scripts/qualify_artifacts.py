"""Qualify prebuilt release wheels in a disposable environment outside the tree.

Build with ``python -m build`` first (its default builds each wheel from its
sdist). This command never rebuilds an artifact or contacts a model/agent.
It records package versions and artifact hashes, without exporting pip's URLs
or local direct-install metadata.
"""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


PROJECTS = {"protagine", "protagine-hermes", "protagine-hostworker"}


def run(args: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=600,
    )
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}\n{result.stdout}")
    return result.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dist = args.dist.resolve()
    constraints = args.constraints.resolve()
    output = args.output.resolve()
    # A failed new run must never leave an earlier passing receipt in place.
    output.mkdir(parents=True, exist_ok=False)
    wheels: dict[str, Path] = {}
    versions: dict[str, str] = {}
    for path in sorted(dist.glob("*.whl")):
        with zipfile.ZipFile(path) as archive:
            metadata_path, = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            metadata = BytesParser().parsebytes(archive.read(metadata_path))
        name = str(metadata["Name"]).lower().replace("_", "-")
        if name not in PROJECTS or name in wheels:
            raise ValueError(f"Unexpected or duplicate distribution: {path.name}")
        wheels[name] = path
        versions[name] = str(metadata["Version"])
    if set(wheels) != PROJECTS:
        raise ValueError(f"Expected wheels for {sorted(PROJECTS)}; found {sorted(wheels)}")
    sdists = list(dist.glob("*.tar.gz"))
    expected_sdists = {f"{name.replace('-', '_')}-{versions[name]}.tar.gz" for name in PROJECTS}
    if {path.name for path in sdists} != expected_sdists:
        raise ValueError("Each wheel needs its matching source distribution")

    with tempfile.TemporaryDirectory(prefix="protagine-artifacts-") as temporary:
        work = Path(temporary)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PYTHON", "HERMES", "PROTAGINE"))}
        (work / "home").mkdir()
        env.update(HOME=str(work / "home"), HERMES_HOME=str(work / "hermes"),
                   PYTHONNOUSERSITE="1", PYTHON_DOTENV_DISABLED="1")
        run([sys.executable, "-m", "venv", str(work / "venv")], cwd=work, env=env)
        python = str(work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        pip = [python, "-I", "-m", "pip"]
        # Check the standalone worker before installing any sidecar dependencies.
        run(pip + ["install", "--no-deps", str(wheels["protagine-hostworker"])], cwd=work, env=env)
        run(pip + ["check"], cwd=work, env=env)
        worker_packages = json.loads(run(pip + ["list", "--format=json"], cwd=work, env=env))
        if any(p["name"].lower() not in {"pip", "setuptools", "protagine-hostworker"}
               for p in worker_packages):
            raise ValueError("Standalone hostworker environment contains unexpected packages")
        conformance = run([python, "-I", "-m", "protagine_hostworker.conformance"], cwd=work, env=env)
        (output / "hostworker-conformance.txt").write_text(conformance)

        run(pip + ["install", "--constraint", str(constraints),
                   str(wheels["protagine-hermes"]) + "[native-memory]",
                   str(wheels["protagine"]) + "[hermes]"], cwd=work, env=env)
        consistency = run(pip + ["check"], cwd=work, env=env)
        (output / "pip-check.txt").write_text(consistency)
        smoke = run([python, "-I", "-c", """
import importlib, importlib.metadata as metadata, json, pathlib, sys
for name in ('protagine', 'protagine_hermes', 'protagine_memory.provider', 'protagine_hostworker'):
    module = importlib.import_module(name)
    assert pathlib.Path(module.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()), name
entries = metadata.distribution('protagine-hermes').entry_points
assert any(e.group == 'hermes_agent.plugins' and e.name == 'protagine' for e in entries)
assert any(e.group == 'hermes_agent.memory_providers' and e.name == 'protagine-memory' for e in entries)
print(json.dumps({'installed_imports': True, 'adapter_entry_points': True}))
from protagine.hermes_patches import describe_patchset
assert describe_patchset()['official_revision']
"""], cwd=work, env=env)
        run([python, "-I", "-m", "protagine", "--help"], cwd=work, env=env)
        packages = json.loads(run(pip + ["list", "--format=json"], cwd=work, env=env))

    receipt = {
        "schema": 1,
        "source_commit": os.environ.get("GITHUB_SHA"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "components": versions,
        "constraints_sha256": sha256(constraints),
        "artifacts": {path.name: {"bytes": path.stat().st_size, "sha256": sha256(path)}
                      for path in sorted([*wheels.values(), *sdists])},
        "installed_packages": packages,
        "checks": {"standalone_hostworker_conformance": True, "pip_check": True,
                   "sidecar_cli_help": True, **json.loads(smoke)},
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print("Qualified three installed distributions; receipt:", output / "receipt.json")


if __name__ == "__main__":
    main()
