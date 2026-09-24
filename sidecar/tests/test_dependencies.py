"""The documented install (``pipx install protagine``) must produce a sidecar with a vector store."""
import tomllib
from pathlib import Path

SIDECAR = Path(__file__).resolve().parents[1] / "pyproject.toml"
ADAPTER = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _requirement_names(items):
    names = set()
    for item in items:
        name = item.split(";")[0].strip()
        for separator in ("[", ">", "<", "=", "!", "~", " "):
            name = name.split(separator)[0]
        names.add(name.lower())
    return names


def test_vector_store_is_a_base_dependency_not_an_extra():
    project = tomllib.loads(SIDECAR.read_text())["project"]
    base = _requirement_names(project["dependencies"])
    assert {"lancedb", "pyarrow", "pandas"} <= base
    extras = project.get("optional-dependencies", {})
    assert "lancedb" not in extras, "a redundant extra would keep the old two-step install alive"
    # In-process models stay optional: a remote embeddings endpoint needs none of them.
    assert "sentence-transformers" not in base
    assert "sentence-transformers" in _requirement_names(extras["vectors"])


def test_sidecar_and_adapter_agree_on_the_interpreter_range():
    sidecar = tomllib.loads(SIDECAR.read_text())["project"]["requires-python"]
    adapter = tomllib.loads(ADAPTER.read_text())["project"]["requires-python"]
    assert sidecar == adapter == ">=3.11,<3.14"
