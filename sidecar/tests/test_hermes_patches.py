"""Real Git patch staging, including rejection before an installed tree changes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from protagine import hermes_patches as patches


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "official"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Patch qualification")
    git(root, "config", "user.email", "qualification@example.invalid")
    original = b"def current_input():\n    return None\n"
    changed = b"def current_input():\n    return 'persisted-row'\n"
    (root / "runtime.py").write_bytes(original)
    (root / "untouched.py").write_text("unchanged = True\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Official test runtime")
    revision = git(root, "rev-parse", "HEAD").decode().strip()
    (root / "runtime.py").write_bytes(changed)
    patch = git(root, "diff", "--full-index", "--binary")
    git(root, "checkout", "--", "runtime.py")
    directory = tmp_path / "patchsets" / patches.DEFAULT_PATCHSET
    directory.mkdir(parents=True)
    (directory / "runtime.patch").write_bytes(patch)
    manifest = {"schema": "protagine.hermes-patchset.v1", "id": patches.DEFAULT_PATCHSET,
                "official_revision": revision,
                "official_tree": git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
                "patches": [{"path": "runtime.patch", "purpose": "runtime",
                             "sha256": hashlib.sha256(patch).hexdigest(), "files": [
                                 {"path": "runtime.py", "before_sha256": hashlib.sha256(original).hexdigest(),
                                  "after_sha256": hashlib.sha256(changed).hexdigest()}]}]}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(patches, "_PATCHSETS", directory.parent)
    return root, directory, manifest


def test_stages_effect_without_touching_source_or_copying_private_files(source, tmp_path):
    root, _, _ = source
    (root / "credentials.env").write_text("PRIVATE_FIXTURE=never-copy\n")
    original = (root / "runtime.py").read_bytes()
    destination = tmp_path / "staged"
    receipt = patches.stage_runtime(root, destination)
    namespace = {}
    exec((destination / "runtime.py").read_text(), namespace)
    assert namespace["current_input"]() == "persisted-row"
    assert (root / "runtime.py").read_bytes() == original
    assert not (destination / "credentials.env").exists()
    assert (destination / "untouched.py").read_bytes() == (root / "untouched.py").read_bytes()
    assert not (destination / ".git").exists()
    assert receipt["activation_state"] == "not_activated"
    assert patches.inspect_runtime(destination)["status"] == "patched"
    assert patches.inspect_runtime(destination) == patches.inspect_runtime(destination)
    with pytest.raises(ValueError, match="must not exist"):
        patches.stage_runtime(root, destination)


def test_modified_target_refused_before_staging(source, tmp_path):
    root, _, _ = source
    (root / "runtime.py").write_text("raise RuntimeError('local change')\n")
    destination = tmp_path / "staged"
    assert patches.inspect_runtime(root)["status"] == "conflict"
    with pytest.raises(ValueError, match="tracked modifications"):
        patches.stage_runtime(root, destination)
    assert not destination.exists()
    assert "local change" in (root / "runtime.py").read_text()


def test_changed_untouched_source_is_also_refused(source, tmp_path):
    root, _, _ = source
    (root / "untouched.py").write_text("changed = True\n")
    with pytest.raises(ValueError, match="tracked modifications"):
        patches.stage_runtime(root, tmp_path / "staged")


def test_new_upstream_revision_needs_new_qualification(source, tmp_path):
    root, _, _ = source
    git(root, "commit", "--allow-empty", "-qm", "A different upstream release")
    with pytest.raises(ValueError, match="Unsupported Hermes revision"):
        patches.stage_runtime(root, tmp_path / "staged")


def test_ci_can_test_an_unlisted_revision_without_admitting_it_for_install(source, tmp_path):
    root, _, manifest = source
    (root/'untouched.py').write_text('upstream_changed = True\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Upstream update outside the patch surface')
    revision = git(root, 'rev-parse', 'HEAD').decode().strip()
    receipt = patches.stage_runtime(root, tmp_path/'candidate', candidate_upstream=True)
    assert receipt['source_revision'] == revision != manifest['official_revision']
    assert receipt['source_tree'] == git(root, 'rev-parse', 'HEAD^{tree}').decode().strip()
    assert receipt['qualification_only'] is True
    assert receipt['activation_state'] == 'not_activated'
    with pytest.raises(ValueError, match='Unsupported Hermes revision'):
        patches.stage_runtime(root, tmp_path/'install')


def test_ci_reports_exact_conflicting_files_without_fuzzy_application(source, tmp_path):
    root, _, _ = source
    (root/'runtime.py').write_text('upstream_changed_the_interface = True\n')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'Incompatible upstream interface')
    with pytest.raises(ValueError, match='preimage conflict: runtime.py'):
        patches.stage_runtime(root, tmp_path/'candidate', candidate_upstream=True)
    assert not (tmp_path/'candidate').exists()


def test_patch_asset_tampering_is_refused(source, tmp_path):
    root, directory, _ = source
    with (directory / "runtime.patch").open("ab") as handle:
        handle.write(b"\nchanged\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        patches.stage_runtime(root, tmp_path / "staged")


def test_incorrect_postimage_leaves_no_destination(source, tmp_path):
    root, directory, manifest = source
    manifest["patches"][0]["files"][0]["after_sha256"] = "0" * 64
    (directory / "manifest.json").write_text(json.dumps(manifest))
    destination = tmp_path / "staged"
    with pytest.raises(ValueError, match="postimages"):
        patches.stage_runtime(root, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".protagine-hermes-*"))


def test_symlink_target_is_not_followed(source, tmp_path):
    root, _, _ = source
    outside = tmp_path / "outside.py"
    outside.write_bytes((root / "runtime.py").read_bytes())
    (root / "runtime.py").unlink()
    (root / "runtime.py").symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        patches.inspect_runtime(root)


def test_bundled_manifest_has_official_provenance():
    manifest = patches.describe_patchset()
    assert manifest["official_repository"] == "https://github.com/NousResearch/hermes-agent"
    assert manifest["official_revision"] == "345cd2b057a452236de401d3534b8502a7465e8d"
    assert "LICENSE" in manifest["assets"] and "provenance.json" in manifest["assets"]
    runtime = next(patch for patch in manifest["patches"] if patch["purpose"] == "runtime")
    assert not any(row["path"].startswith(("tests/", "website/")) for row in runtime["files"])


def test_staging_inside_another_repository_does_not_patch_that_repository(source, tmp_path):
    root, _, _ = source
    enclosing = tmp_path / "other-project"
    enclosing.mkdir()
    git(enclosing, "init", "-q")
    (enclosing / "runtime.py").write_text("Do not edit this unrelated runtime.\n")
    destination = enclosing / "staged"
    assert patches.stage_runtime(root, destination)["status"] == "patched"
    assert (enclosing / "runtime.py").read_text() == "Do not edit this unrelated runtime.\n"


def test_cli_uses_the_same_staging_and_inspection_contract(source, tmp_path, capsys):
    root, _, _ = source
    destination = tmp_path / "staged"
    assert patches.main(["--source", str(root), "--destination", str(destination)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "patched"
    assert patches.main(["--source", str(destination), "--inspect"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "patched"
