"""Source layout for isolated plugin import tests; setup uses packaged adapters."""

from pathlib import Path
import shutil


def copy_adapter_sources(home):
    root = Path(__file__).resolve().parents[2]
    for source, destination in (("hermes-plugin", "protagine"), ("protagine-memory", "protagine-memory")):
        shutil.copytree(root / "plugins" / source, home / "plugins" / destination,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    bundled = home / "plugins/protagine/protagine_hostworker"
    for name in ("catalog.py", "contract.py"):
        shutil.copyfile(root / "hostworker/protagine_hostworker" / name, bundled / name)
