"""Stage a known Hermes source release with Protagine's packaged interfaces.

This module never edits an installed runtime, profile, or service. Unknown source
revisions require a newly qualified patchset, not fuzzy patch application.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile

DEFAULT_PATCHSET = "hermes-0.21.3-protagine-1"
_PATCHSETS = Path(__file__).with_name("hermes_patchsets")
_SCHEMA = "protagine.hermes-patchset.v1"
_RECEIPT_SCHEMA = "protagine.hermes-patch-stage.v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: str) -> Path:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("Patchset contains an invalid relative path")
    return Path(*path.parts)


def _bundle(patchset_id: str) -> tuple[Path, dict]:
    if _relative(patchset_id).name != patchset_id:
        raise ValueError("Invalid Hermes patchset identifier")
    directory = _PATCHSETS / patchset_id
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
    except (OSError, ValueError) as error:
        raise ValueError(f"Unknown or unreadable Hermes patchset: {patchset_id}") from error
    if manifest.get("schema") != _SCHEMA or manifest.get("id") != patchset_id:
        raise ValueError("Invalid Hermes patchset manifest")
    seen = set()
    for patch in manifest["patches"]:
        path = directory / _relative(patch["path"])
        if _sha256(path.read_bytes()) != patch["sha256"]:
            raise ValueError("Hermes patchset asset digest mismatch")
        for entry in patch["files"]:
            relative = _relative(entry["path"])
            if relative in seen:
                raise ValueError("Overlapping patchset file ownership")
            seen.add(relative)
    for name, digest in manifest.get("assets", {}).items():
        if _sha256((directory / _relative(name)).read_bytes()) != digest:
            raise ValueError("Hermes patchset attribution digest mismatch")
    return directory, manifest


def describe_patchset(patchset_id: str = DEFAULT_PATCHSET) -> dict:
    """Return the validated manifest; no subprocess or source checkout required."""
    return _bundle(patchset_id)[1]


def _patches(manifest: dict, include_regressions: bool) -> list[dict]:
    return [patch for patch in manifest["patches"]
            if patch["purpose"] == "runtime" or include_regressions]


def _file_digest(root: Path, relative: str) -> str | None:
    path = root / _relative(relative)
    # A file or parent symlink must never redirect a patch outside its tree.
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root.parent):
        raise ValueError(f"Symlink in Hermes patch target: {relative}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"Non-file Hermes patch target: {relative}")
    return _sha256(path.read_bytes())


def inspect_runtime(root, patchset_id: str = DEFAULT_PATCHSET, *,
                    include_regressions: bool = False) -> dict:
    """Classify all selected patch preimages/postimages without writing files.

    This checks the patch surface, not the behavior of the installed application.
    A `patched` result recognizes an exact reapplication and is not activation.
    """
    root = Path(root).resolve(strict=True)
    directory, manifest = _bundle(patchset_id)
    states, conflicts = set(), []
    for patch in _patches(manifest, include_regressions):
        for entry in patch["files"]:
            actual = _file_digest(root, entry["path"])
            if actual == entry["after_sha256"]:
                states.add("patched")
            elif actual == entry["before_sha256"]:
                states.add("unpatched")
            else:
                conflicts.append(entry["path"])
    status = next(iter(states)) if len(states) == 1 and not conflicts else "conflict"
    return {"schema": _RECEIPT_SCHEMA, "patchset_id": patchset_id,
            "manifest_sha256": _sha256((directory / "manifest.json").read_bytes()),
            "official_revision": manifest["official_revision"],
            "status": status, "conflicting_files": conflicts,
            "mixed_patch_state": len(states) > 1,
            "include_regressions": include_regressions,
            "activation_state": "not_activated"}


def _git(root: Path, *arguments: str) -> bytes:
    environment = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(key, None)
    # A staging directory may sit inside the Protagine checkout in CI. Do not
    # let `git apply` discover and address that surrounding repository.
    environment["GIT_CEILING_DIRECTORIES"] = str(root.parent)
    try:
        result = subprocess.run(["git", "-C", str(root), *arguments],
                                capture_output=True, check=True, timeout=120, env=environment)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError("Could not inspect or stage the selected Hermes Git checkout") from error
    return result.stdout


def stage_runtime(source, destination, patchset_id: str = DEFAULT_PATCHSET, *,
                  include_regressions: bool = False, candidate_upstream: bool = False) -> dict:
    """Copy a clean official Git revision, patch, verify, then publish a new directory.

    Untracked files, profiles and virtual environments are never copied. The
    caller owns dependency installation, behavioral qualification and any later
    activation. Installers require a qualified revision. CI can stage an unlisted
    candidate only when every exact preimage matches; it must then run behavior
    qualification. Neither path alters an existing runtime selection.
    """
    source = Path(source).resolve(strict=True)
    destination = Path(destination).absolute()
    directory, manifest = _bundle(patchset_id)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Hermes staging destination must not exist")
    if destination == source or source in destination.parents:
        raise ValueError("Hermes staging destination must be outside the source checkout")
    revision = _git(source, "rev-parse", "HEAD").decode().strip()
    if revision != manifest["official_revision"] and not candidate_upstream:
        raise ValueError("Unsupported Hermes revision; qualify an updated patchset before upgrading")
    if _git(source, "status", "--porcelain", "--untracked-files=no").strip():
        raise ValueError("Hermes source has tracked modifications; source was not changed")
    inspection = inspect_runtime(source, patchset_id, include_regressions=include_regressions)
    if inspection["status"] != "unpatched":
        raise ValueError("Hermes patch preimage conflict: " +
                         ", ".join(inspection['conflicting_files'] or ['mixed or already patched source']))
    if not destination.parent.is_dir():
        raise ValueError("Hermes staging parent directory must already exist")
    with tempfile.TemporaryDirectory(prefix=".protagine-hermes-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        archive = temporary / "source.tar"
        staged = temporary / "runtime"
        staged.mkdir()
        _git(source, "archive", "--format=tar", f"--output={archive}", revision)
        # Extract only regular tracked source files and directories. Git archive
        # avoids copying untracked credentials or racing mutable worktree bytes.
        with tarfile.open(archive) as source_archive:
            for member in source_archive:
                relative = _relative(member.name)
                path = staged / relative
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source_archive.extractfile(member) as content, path.open("wb") as output:
                        shutil.copyfileobj(content, output)
                    path.chmod(member.mode & 0o777)
                else:
                    raise ValueError("Unsupported non-file entry in official Hermes source")
        for patch in _patches(manifest, include_regressions):
            patch_path = str(directory / patch["path"])
            _git(staged, "apply", "--check", patch_path)
            _git(staged, "apply", "--whitespace=nowarn", patch_path)
        receipt = inspect_runtime(staged, patchset_id, include_regressions=include_regressions)
        if receipt["status"] != "patched":
            raise ValueError("Staged Hermes postimages do not match the qualified patchset")
        receipt.update(source_revision=revision,
                       source_tree=_git(source, "rev-parse", "HEAD^{tree}").decode().strip(),
                       qualification_only=bool(candidate_upstream),
                       patches=[{"path": patch["path"], "sha256": patch["sha256"]}
                                for patch in _patches(manifest, include_regressions)])
        (staged / ".protagine-patch-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        # Refuse a destination created while staging. No live path is replaced.
        if destination.exists() or destination.is_symlink():
            raise ValueError("Hermes staging destination appeared during preparation")
        os.rename(staged, destination)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--patchset", default=DEFAULT_PATCHSET)
    parser.add_argument("--include-regressions", action="store_true")
    parser.add_argument("--candidate-upstream", action="store_true",
                        help="CI only: stage an unlisted revision with exact matching preimages for qualification; never selects a runtime")
    parser.add_argument("--inspect", action="store_true", help="Check patch preimages/postimages without staging")
    args = parser.parse_args(argv)
    try:
        if args.inspect:
            receipt = inspect_runtime(args.source, args.patchset,
                                      include_regressions=args.include_regressions)
        elif args.destination is not None:
            receipt = stage_runtime(args.source, args.destination, args.patchset,
                                    include_regressions=args.include_regressions,
                                    candidate_upstream=args.candidate_upstream)
        else:
            parser.error("--destination is required unless --inspect is selected")
        print(json.dumps(receipt, sort_keys=True))
        return 1 if receipt["status"] == "conflict" else 0
    except (OSError, ValueError) as error:
        parser.exit(2, f"Hermes compatibility staging failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
