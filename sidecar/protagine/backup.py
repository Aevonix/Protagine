"""Full-state backup and restore for Protagine.

Creates a compressed, optionally encrypted archive of all Protagine state:
- All SQLite databases in PROTAGINE_STATE_DIR (auto-discovered)
- Original source images referenced by the canonical ledger snapshot
- The instance identity (instance-id, identity.yaml, protagine.yaml, api.key)
- Config files (scrubbed of secrets)
- LanceDB vector store

Usage::

    # Backup
    protagine backup --full --output ~/backups/

    # Restore
    protagine restore --full protagine-backup-20260627T120000.tar.gz
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

from protagine.instance import INSTANCE_ID_FILE, instance_id, read_instance_id

logger = logging.getLogger(__name__)

BACKUP_VERSION = 2

_SECRET_KEY_PATTERN = re.compile(
    r"(_KEY|_SECRET|_PASSWORD|_TOKEN|_PASSPHRASE)$", re.IGNORECASE
)


# ── Backup ───────────────────────────────────────────────────────────────


def create_full_backup(
    state_dir: str | Path,
    output_dir: str | Path,
    *,
    passphrase: Optional[bytes] = None,
    include_vectors: bool = True,
    include_host_paths: Optional[list[str]] = None,
) -> Path:
    """Create a full backup archive of Protagine state.

    Returns the path to the created archive.
    """
    state_dir = Path(state_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # The archive is bound to the instance it was taken from.
    instance = instance_id(state_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"protagine-backup-{timestamp}"

    with tempfile.TemporaryDirectory(prefix="protagine-backup-") as tmp:
        staging = Path(tmp) / base_name
        staging.mkdir()

        db_manifest = _snapshot_databases(state_dir, staging / "databases")
        source_images = _snapshot_source_images(state_dir, staging)
        _snapshot_identity(state_dir, staging / "identity")
        _snapshot_config(state_dir, staging / "config")

        if include_vectors:
            _snapshot_vectors(state_dir, staging / "vector")

        if include_host_paths:
            _snapshot_host_state(include_host_paths, staging / "host")

        meta = {
            "backup_version": BACKUP_VERSION,
            "protagine_version": _get_protagine_version(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "instance_id": instance,
            "instance_id_hmac": _compute_identity_hmac(instance),
            "database_manifest": db_manifest,
            "source_images": source_images,
            "encrypted": passphrase is not None,
        }
        (staging / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n"
        )

        archive_path = output_dir / f"{base_name}.tar.gz"
        _create_archive(staging, archive_path)

        if passphrase is not None:
            encrypted_path = output_dir / f"{base_name}.tar.gz.enc"
            _encrypt_file(archive_path, encrypted_path, passphrase)
            archive_path.unlink()
            archive_path = encrypted_path

    logger.info("Full backup created: %s", archive_path)
    return archive_path


# ── Restore ──────────────────────────────────────────────────────────────


def restore_full_backup(
    archive_path: str | Path,
    state_dir: str | Path,
    *,
    passphrase: Optional[bytes] = None,
) -> dict[str, Any]:
    """Restore Protagine state from a full backup archive.

    The archive restores into a fresh state directory or into the instance it
    was taken from; another instance's directory is refused (remove its
    ``instance-id`` first if that is really what you want).

    Returns a summary dict of what was restored.
    """
    archive_path = Path(archive_path)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="protagine-restore-") as tmp:
        staging = Path(tmp) / "restore"
        staging.mkdir()

        actual_archive = archive_path
        if archive_path.suffix == ".enc":
            if passphrase is None:
                raise ValueError("Archive is encrypted; passphrase required")
            actual_archive = Path(tmp) / "decrypted.tar.gz"
            _decrypt_file(archive_path, actual_archive, passphrase)

        _extract_archive(actual_archive, staging)

        root = _find_backup_root(staging)
        meta = json.loads((root / "meta.json").read_text())

        if meta.get("backup_version", 0) > BACKUP_VERSION:
            raise ValueError(
                f"Backup version {meta['backup_version']} is newer than "
                f"this Protagine supports (max {BACKUP_VERSION})"
            )

        existing_id = read_instance_id(state_dir)
        backup_id = _archive_instance_id(meta)

        if existing_id and existing_id != backup_id:
            raise ValueError(
                f"Backup is from instance {backup_id} but this directory is "
                f"instance {existing_id}. Restore into a fresh directory, or "
                f"remove its {INSTANCE_ID_FILE} file first."
            )

        # Validate every referenced original before writing any restored state.
        # A caption or an embedding cannot reconstruct the original evidence.
        source_images = _verified_source_images(root)

        summary: dict[str, Any] = {
            "instance_id": backup_id, "databases": [], "errors": [],
            "serving_admitted": False,
        }

        identity_dir = root / "identity"
        if identity_dir.is_dir():
            _restore_directory(identity_dir, state_dir)
            summary["identity"] = True
        if backup_id and not read_instance_id(state_dir):
            # Archives taken before the instance id existed carry the same UUID
            # under the chain's name in meta; the restored instance keeps it.
            (state_dir / INSTANCE_ID_FILE).write_text(backup_id + "\n")
            (state_dir / INSTANCE_ID_FILE).chmod(0o600)

        db_dir = root / "databases"
        if db_dir.is_dir():
            for db_file in sorted(db_dir.glob("*.db")):
                relative = db_file.relative_to(db_dir)
                dest = state_dir / relative
                # A crashed destination can still have committed WAL.
                # SQLite replaces its logical database consistently;
                # copying the main file alone can replay the old WAL over it.
                with closing(sqlite3.connect(db_file.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                    with closing(sqlite3.connect(dest)) as target:
                        source.backup(target)
                relative_name = relative.as_posix()
                summary["databases"].append(relative_name)
                logger.info("Restored database: %s", relative_name)

        if source_images:
            from protagine.vector.image_store import LocalImageStore
            images = LocalImageStore(str(state_dir), source_evidence=True)
            images._ensure_dirs()
            for record, source in source_images:
                destination = images._original_path(record['asset_hash'], record['mime_type'])
                shutil.copyfile(source, destination)
                destination.chmod(0o600)
            summary['source_images'] = len(source_images)

        config_dir = root / "config"
        if config_dir.is_dir():
            _restore_directory(config_dir, state_dir)
            summary["config"] = True

        vector_dir = root / "vector"
        if vector_dir.is_dir():
            dest = state_dir / "lancedb"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(vector_dir / "lancedb", dest, dirs_exist_ok=True)
            summary["vectors"] = True

    logger.info("Restore complete: %s", summary)
    return summary


def restore_source_memory(
    archive_path: str | Path,
    destination: str | Path,
    *,
    current_state: str | Path,
    passphrase: Optional[bytes] = None,
) -> dict[str, Any]:
    """Recover current canonical memory, without restoring runtime authority.

    The caller must select a surviving authoritative source ledger. Its current
    membership, revisions, scopes and erasure history replace the old archive's
    memory metadata. The archive supplies only still-owned original image bytes
    missing from that surviving state. No old grants, effects, identity, config,
    contact databases, tasks or native host state are installed.
    """
    archive_path = Path(archive_path).resolve(strict=True)
    current_state = Path(current_state).resolve()
    destination = Path(destination).absolute()
    resolved_destination = destination.resolve()
    if (destination.exists() or destination.is_symlink()
            or current_state == resolved_destination
            or current_state in resolved_destination.parents
            or resolved_destination in current_state.parents):
        raise ValueError("Memory recovery requires a fresh destination outside current state")
    current_id = read_instance_id(current_state)
    ledger = current_state / "turn-idempotency.db"
    if not current_id or not ledger.is_file():
        raise ValueError("Memory recovery requires the surviving instance id and source ledger")

    from protagine.vector.image_store import LocalImageStore

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="protagine-memory-recovery-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        actual_archive = archive_path
        if archive_path.suffix == ".enc":
            if passphrase is None:
                raise ValueError("Archive is encrypted; passphrase required")
            actual_archive = staging / "decrypted.tar.gz"
            _decrypt_file(archive_path, actual_archive, passphrase)
        unpacked = staging / "archive"
        unpacked.mkdir()
        _extract_archive(actual_archive, unpacked)
        archived = _find_backup_root(unpacked)
        meta = json.loads((archived / "meta.json").read_text())
        if meta.get("backup_version", 0) > BACKUP_VERSION:
            raise ValueError("Memory archive version is newer than this Protagine supports")
        if _archive_instance_id(meta) != current_id:
            raise ValueError("Memory archive and surviving state have different instance ids")

        selected = staging / "selected"
        (selected / "databases").mkdir(parents=True)
        snapshot = selected / "databases" / "turn-idempotency.db"
        try:
            with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Surviving source ledger failed its integrity check")
            snapshot.chmod(0o600)
            with closing(sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)) as current:
                source_count = current.execute("SELECT count(*) FROM turn_sources").fetchone()[0]
                heads = current.execute(
                    "SELECT contact_id,max(sequence) FROM source_erasures GROUP BY contact_id"
                ).fetchall()
                _check_erasure_ancestry(archived / "databases" / "turn-idempotency.db", current)
        except sqlite3.Error as error:
            raise ValueError("Surviving canonical source history is unavailable") from error
        if read_instance_id(current_state) != current_id:
            raise ValueError("Surviving instance id changed during memory recovery")

        bundle = staging / "bundle"
        bundle.mkdir(mode=0o700)
        shutil.copyfile(snapshot, bundle / "turn-idempotency.db")
        (bundle / "turn-idempotency.db").chmod(0o600)
        output_images = LocalImageStore(str(bundle), source_evidence=True)
        live_images = LocalImageStore(str(current_state), source_evidence=True)
        old_images = LocalImageStore(str(archived), source_evidence=True)
        recovered_from_archive = 0
        records = _source_image_records(selected)
        if records:
            output_images._ensure_dirs()
        for record in records:
            chosen = None
            for source_images, from_archive in ((live_images, False), (old_images, True)):
                candidate = source_images._original_path(record["asset_hash"], record["mime_type"])
                try:
                    data = candidate.read_bytes()
                except FileNotFoundError:
                    continue
                if (len(data) != record["size_bytes"]
                        or hashlib.sha256(data).hexdigest() != record["asset_hash"]):
                    continue
                chosen = data
                recovered_from_archive += int(from_archive)
                break
            if chosen is None:
                raise ValueError("Current source image has no matching original in surviving state or archive")
            target = output_images._original_path(record["asset_hash"], record["mime_type"])
            target.write_bytes(chosen)
            target.chmod(0o600)

        files = []
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                files.append({"path": path.relative_to(bundle).as_posix(),
                              "sha256": _file_sha256(path), "bytes": path.stat().st_size})
        summary = {
            "recovery_mode": "source_memory_only",
            "instance_id": current_id,
            "source_count": source_count,
            "source_images": len(records),
            "images_recovered_from_archive": recovered_from_archive,
            "erasure_head": max((row[1] for row in heads), default=0),
            "erasure_contacts": len(heads),
            "current_source_ledger_sha256": _file_sha256(snapshot),
            "archive_sha256": _file_sha256(archive_path),
            "files": files,
            "runtime_authority_restored": False,
            "serving_admitted": False,
        }
        receipt = bundle / "source-memory-recovery.json"
        receipt.write_text(json.dumps(summary, indent=2) + "\n")
        receipt.chmod(0o600)
        # Reserve the destination only after all source and image checks pass.
        # A publication failure leaves an incomplete bundle, never a success.
        destination.mkdir(mode=0o700)
        _restore_directory(bundle, destination)
    return summary


def _file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _check_erasure_ancestry(archived_ledger: Path, current: sqlite3.Connection) -> None:
    """A selected surviving history cannot rewind or replace captured erasures."""
    if not archived_ledger.is_file():
        return
    with closing(sqlite3.connect(archived_ledger.resolve().as_uri() + "?mode=ro", uri=True)) as old:
        for table in ("source_erasures", "source_erasure_revisions"):
            if not old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            columns = [row[1] for row in old.execute(f"PRAGMA table_info({table})")]
            selected = ",".join('"' + column.replace('"', '""') + '"' for column in columns)
            for row in old.execute(f"SELECT {selected} FROM {table}"):
                sequence = row[columns.index("sequence")]
                current_row = current.execute(
                    f"SELECT {selected} FROM {table} WHERE sequence=?", (sequence,)
                ).fetchone()
                if current_row != row:
                    raise ValueError("Surviving source erasure history does not extend the archive")


# ── Database snapshot ────────────────────────────────────────────────────


def _snapshot_databases(
    state_dir: Path, dest: Path,
) -> list[dict[str, str]]:
    """Snapshot the top-level databases."""
    dest.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []

    for db_path in sorted(state_dir.glob("*.db")):
        relative = db_path.relative_to(state_dir)
        relative_name = relative.as_posix()
        snap_path = dest / relative
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                escaped_snap_path = str(snap_path).replace("'", "''")
                conn.execute(f"VACUUM INTO '{escaped_snap_path}'")
            finally:
                conn.close()
            manifest.append({
                "filename": relative_name,
                "size_bytes": str(snap_path.stat().st_size),
            })
            logger.info("Snapshotted database: %s", relative_name)
        except Exception:
            logger.warning("VACUUM snapshot failed for %s; trying SQLite backup", relative_name)
            try:
                # Copying only the main file can lose committed WAL records.
                snap_path.unlink(missing_ok=True)
                with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                    with closing(sqlite3.connect(snap_path)) as target:
                        source.backup(target)
                manifest.append({
                    "filename": relative_name,
                    "size_bytes": str(snap_path.stat().st_size),
                    "method": "sqlite_backup",
                })
            except Exception as exc2:
                raise RuntimeError(f"Database could not be snapshotted consistently: {relative_name}") from exc2

    return manifest


def _source_image_records(root: Path) -> list[dict]:
    """Read asset ownership from the captured database, never a later live view."""
    ledger = root / 'databases' / 'turn-idempotency.db'
    if not ledger.is_file():
        return []
    conn = sqlite3.connect(ledger.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='source_media'").fetchone():
            return []
        rows = conn.execute('''SELECT DISTINCT m.asset_hash,m.mime_type,m.size_bytes
            FROM source_media m JOIN source_media_links l ON l.asset_hash=m.asset_hash
            JOIN turn_sources s ON s.turn_id=l.turn_id WHERE m.status != 'orphan' ''').fetchall()
        records = [dict(row) for row in rows]
        for row in records:
            if not re.fullmatch('[0-9a-f]{64}', row['asset_hash']):
                raise ValueError('Invalid original source image identifier')
        return records
    finally:
        conn.close()


def _verified_source_images(root: Path) -> list[tuple[dict, Path]]:
    from protagine.vector.image_store import LocalImageStore
    images = LocalImageStore(str(root), source_evidence=True)
    result = []
    for record in _source_image_records(root):
        path = images._original_path(record['asset_hash'], record['mime_type'])
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError('Backup is missing an original source image') from exc
        if len(data) != record['size_bytes'] or hashlib.sha256(data).hexdigest() != record['asset_hash']:
            raise ValueError('Backup original source image does not match its evidence hash')
        result.append((record, path))
    return result


def _snapshot_source_images(state_dir: Path, staging: Path) -> int:
    from protagine.vector.image_store import LocalImageStore
    source = LocalImageStore(str(state_dir), source_evidence=True)
    target = LocalImageStore(str(staging), source_evidence=True)
    records = _source_image_records(staging)
    if records:
        target._ensure_dirs()
    for record in records:
        try:
            shutil.copyfile(source._original_path(record['asset_hash'], record['mime_type']),
                            target._original_path(record['asset_hash'], record['mime_type']))
        except OSError as exc:
            # Concurrent erasure can remove an original after the DB snapshot.
            # Retry the backup later instead of publishing a broken archive.
            raise RuntimeError('Original source image changed during backup; retry') from exc
    return len(_verified_source_images(staging))


# ── Identity snapshot ────────────────────────────────────────────────────


IDENTITY_FILES = (INSTANCE_ID_FILE, "identity.yaml", "protagine.yaml", "api.key")


def _snapshot_identity(state_dir: Path, dest: Path) -> None:
    """The instance id and the owner-authored identity and configuration."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in IDENTITY_FILES:
        src = state_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)


# ── Config snapshot (with secret scrubbing) ──────────────────────────────


def _snapshot_config(state_dir: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    config_files = [
        ".env", "channels.json", ".protagine-llm-config.json",
    ]

    for name in config_files:
        src = state_dir / name
        if not src.exists():
            src = state_dir.parent / name
        if src.exists():
            if name == ".env":
                scrubbed = _scrub_env_file(src.read_text())
                (dest / name).write_text(scrubbed)
            else:
                shutil.copy2(src, dest / name)


def _scrub_env_file(content: str) -> str:
    lines = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if "=" in stripped:
            key, _, _ = stripped.partition("=")
            if _SECRET_KEY_PATTERN.search(key.strip()):
                lines.append(f"{key.strip()}=<REDACTED>")
                continue
        lines.append(line)
    return "\n".join(lines) + "\n"


# ── Vector store snapshot ────────────────────────────────────────────────


def _snapshot_vectors(state_dir: Path, dest: Path) -> None:
    lance_dir = state_dir / "lancedb"
    if not lance_dir.is_dir():
        return
    dest_lance = dest / "lancedb"
    shutil.copytree(lance_dir, dest_lance)
    logger.info("Snapshotted LanceDB directory")


# ── Host state ───────────────────────────────────────────────────────────


def _snapshot_host_state(paths: list[str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for p in paths:
        src = Path(p).expanduser()
        if src.is_file():
            shutil.copy2(src, dest / src.name)
        elif src.is_dir():
            shutil.copytree(src, dest / src.name, dirs_exist_ok=True)


# ── Archive creation / extraction ────────────────────────────────────────


def _create_archive(source_dir: Path, archive_path: Path) -> None:
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(source_dir, arcname=source_dir.name)


def _extract_archive(archive_path: Path, dest: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(dest, filter="data")


def _find_backup_root(staging: Path) -> Path:
    """Find the root directory inside the extracted archive."""
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return staging


# ── Encryption (AES-256-GCM) ────────────────────────────────────────────


def _encrypt_file(
    src: Path, dest: Path, passphrase: bytes,
) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    salt = os.urandom(16)
    kdf = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1)
    key = kdf.derive(passphrase)

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    plaintext = src.read_bytes()
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    with dest.open("wb") as f:
        f.write(b"PROTAGINE_ENC_V1\n")
        f.write(salt)
        f.write(nonce)
        f.write(ciphertext)


def _decrypt_file(
    src: Path, dest: Path, passphrase: bytes,
) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    data = src.read_bytes()
    header = b"PROTAGINE_ENC_V1\n"
    if not data.startswith(header):
        raise ValueError("Not a Protagine encrypted backup (invalid header)")

    offset = len(header)
    salt = data[offset:offset + 16]
    nonce = data[offset + 16:offset + 28]
    ciphertext = data[offset + 28:]

    kdf = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1)
    key = kdf.derive(passphrase)

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception:
        raise ValueError("Decryption failed -- wrong passphrase?")

    dest.write_bytes(plaintext)


# ── Helpers ──────────────────────────────────────────────────────────────


def _archive_instance_id(meta: dict) -> str:
    """The instance an archive was taken from (older archives named it protagine_id)."""
    return str(meta.get("instance_id") or meta.get("protagine_id") or "")


def _compute_identity_hmac(instance: str) -> str:
    return hmac.new(
        instance.encode(), b"protagine-backup-binding", hashlib.sha256
    ).hexdigest()


def _get_protagine_version() -> str:
    try:
        from importlib.metadata import version
        return version("protagine")
    except Exception:
        return "unknown"


def _restore_directory(src: Path, dest: Path) -> None:
    for item in src.rglob("*"):
        if item.is_file():
            rel = item.relative_to(src)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
