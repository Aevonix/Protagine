"""Tests for the full-state backup and restore system."""

import json
import sqlite3

import pytest

from protagine.backup import (
    create_full_backup,
    restore_full_backup,
    _scrub_env_file,
    _snapshot_databases,
)


@pytest.fixture
def protagine_state(tmp_path):
    """Create a minimal Protagine state directory for testing."""
    state = tmp_path / "state"
    state.mkdir()

    (state / "instance-id").write_text("test-instance-abc123\n")
    (state / "identity.yaml").write_text("owner: {name: Ada}\nagent: {name: Sol}\n")
    (state / "api.key").write_text("fixture-key\n")

    conn = sqlite3.connect(str(state / "protagine-contacts.db"))
    conn.execute("CREATE TABLE contacts (id TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO contacts VALUES ('c1', 'Alice')")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(state / "protagine-affect.db"))
    conn.execute("CREATE TABLE affect (id TEXT PRIMARY KEY, mood TEXT)")
    conn.commit()
    conn.close()

    (state / ".env").write_text(
        "PROTAGINE_API_KEY=super-secret-key\n"
        "PROTAGINE_EMBED_BASE_URL=http://embed.local/v1\n"
        "PROTAGINE_EMBED_API_KEY=embed-secret\n"
        "PROTAGINE_SIDECAR_PORT=7777\n"
    )

    return state


@pytest.fixture
def output_dir(tmp_path):
    return tmp_path / "backups"


# ── Secret scrubbing ────────────────────────────────────────────────────


class TestSecretScrubbing:
    def test_scrubs_key_values(self):
        env = "API_KEY=abc123\nNORMAL=value\nDB_PASSWORD=secret\n"
        result = _scrub_env_file(env)
        assert "API_KEY=<REDACTED>" in result
        assert "NORMAL=value" in result
        assert "DB_PASSWORD=<REDACTED>" in result
        assert "abc123" not in result
        assert "secret" not in result

    def test_preserves_comments(self):
        env = "# This is a comment\nAPI_KEY=secret\n"
        result = _scrub_env_file(env)
        assert "# This is a comment" in result

    def test_preserves_blank_lines(self):
        env = "A=1\n\nB=2\n"
        result = _scrub_env_file(env)
        assert "\n\n" in result

    def test_scrubs_token(self):
        env = "CHANNEL_TOKEN=tok_abc\n"
        result = _scrub_env_file(env)
        assert "CHANNEL_TOKEN=<REDACTED>" in result


# ── Database snapshot ────────────────────────────────────────────────────


class TestDatabaseSnapshot:
    def test_snapshots_all_dbs(self, protagine_state, tmp_path):
        dest = tmp_path / "db_snap"
        manifest = _snapshot_databases(protagine_state, dest)
        filenames = {m["filename"] for m in manifest}
        assert "protagine-contacts.db" in filenames
        assert "protagine-affect.db" in filenames

    def test_snapshot_is_consistent_copy(self, protagine_state, tmp_path):
        dest = tmp_path / "db_snap"
        _snapshot_databases(protagine_state, dest)
        conn = sqlite3.connect(str(dest / "protagine-contacts.db"))
        cur = conn.execute("SELECT name FROM contacts WHERE id = 'c1'")
        assert cur.fetchone()[0] == "Alice"
        conn.close()

    def test_backup_creates_archive(self, protagine_state, output_dir):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_vectors=False,
        )
        assert archive.exists()
        assert archive.suffix == ".gz"

    def test_restore_recovers_state(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        summary = restore_full_backup(archive, restore_dir)

        assert summary["instance_id"] == "test-instance-abc123"
        assert "protagine-contacts.db" in summary["databases"]
        assert (restore_dir / "instance-id").read_text().strip() == "test-instance-abc123"
        assert (restore_dir / "identity.yaml").read_text().startswith("owner:")
        assert (restore_dir / "api.key").read_text().strip() == "fixture-key"

        conn = sqlite3.connect(str(restore_dir / "protagine-contacts.db"))
        cur = conn.execute("SELECT name FROM contacts WHERE id = 'c1'")
        assert cur.fetchone()[0] == "Alice"
        conn.close()

    def test_restore_rejects_identity_mismatch(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_vectors=False,
        )

        restore_dir = tmp_path / "other"
        restore_dir.mkdir()
        (restore_dir / "instance-id").write_text("different-instance-xyz")

        with pytest.raises(ValueError, match="fresh directory"):
            restore_full_backup(archive, restore_dir)
        assert (restore_dir / "instance-id").read_text() == "different-instance-xyz"

    def test_backup_adopts_a_chain_era_protagine_id(self, tmp_path, output_dir):
        """A state directory from before the instance id backs up under the id it already had."""
        state = tmp_path / "legacy"
        state.mkdir()
        (state / "protagine-id").write_text("11111111-2222-3333-4444-555555555555")
        archive = create_full_backup(state, output_dir, include_vectors=False)

        import tarfile
        with tarfile.open(archive, "r:gz") as tar:
            meta_member = next(m for m in tar.getmembers() if m.name.endswith("meta.json"))
            meta = json.load(tar.extractfile(meta_member))
        assert meta["instance_id"] == "11111111-2222-3333-4444-555555555555"
        assert "protagine_id" not in meta
        assert (state / "instance-id").read_text().strip() == "11111111-2222-3333-4444-555555555555"
        restore_dir = tmp_path / "restored"
        assert restore_full_backup(archive, restore_dir)["instance_id"] == meta["instance_id"]
        assert (restore_dir / "instance-id").read_text().strip() == meta["instance_id"]

    def test_restore_leaves_the_retired_state_of_an_old_archive_behind(self, protagine_state, output_dir, tmp_path):
        """An archive taken before the graph, world model and chain went still carries their files; restoring
        it must not put them back (``protagine upgrade`` retired them: init.RETIRED_STATE)."""
        import io
        import tarfile

        archive = create_full_backup(protagine_state, output_dir, include_vectors=False)
        old = tmp_path / "old-archive.tar.gz"
        with tarfile.open(archive, "r:gz") as source, tarfile.open(old, "w:gz") as target:
            root = next(m for m in source.getmembers() if m.name.endswith("meta.json")).name.rsplit("/", 1)[0]
            for member in source.getmembers():
                target.addfile(member, source.extractfile(member) if member.isfile() else None)

            def add(name, data=b"retired"):
                info = tarfile.TarInfo(f"{root}/{name}")
                info.size = len(data)
                target.addfile(info, io.BytesIO(data))

            for name in ("identity/protagine-id", "identity/genesis.json", "identity/protagine-keys/private.pem",
                         "identity/node-cert.json", "config/protagine-manifest.json"):
                add(name)
            chain = tmp_path / "chain.db"
            with sqlite3.connect(chain) as conn:
                conn.execute("CREATE TABLE blocks (id INTEGER)")
            add("databases/chain.db", chain.read_bytes())

        restore_dir = tmp_path / "restored-old"
        summary = restore_full_backup(old, restore_dir)
        restored = {path.relative_to(restore_dir).as_posix() for path in restore_dir.rglob("*")}
        assert not {"protagine-id", "genesis.json", "protagine-keys", "node-cert.json", "protagine-manifest.json",
                    "chain.db"} & restored
        assert "chain.db" not in summary["databases"] and "protagine-contacts.db" in summary["databases"]
        assert sorted(summary["retired_skipped"]) == ["chain.db", "genesis.json", "node-cert.json",
                                                      "protagine-id", "protagine-keys", "protagine-manifest.json"]
        assert (restore_dir / "instance-id").read_text().strip() == "test-instance-abc123"
        assert (restore_dir / "identity.yaml").is_file() and (restore_dir / "api.key").is_file()

    def test_env_file_is_scrubbed_in_backup(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        restore_full_backup(archive, restore_dir)

        env_content = (restore_dir / ".env").read_text()
        assert "super-secret-key" not in env_content
        assert "embed-secret" not in env_content
        assert "PROTAGINE_EMBED_BASE_URL=http://embed.local/v1" in env_content
        assert "PROTAGINE_API_KEY=<REDACTED>" in env_content
        assert "PROTAGINE_SIDECAR_PORT=7777" in env_content

    def test_meta_json_in_archive(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_vectors=False,
        )

        import tarfile
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
        assert any("meta.json" in n for n in names)


# ── Encryption ───────────────────────────────────────────────────────────


class TestEncryption:
    def test_encrypted_backup_restore(self, protagine_state, output_dir, tmp_path):
        passphrase = b"test-passphrase-123"
        archive = create_full_backup(
            protagine_state, output_dir,
            passphrase=passphrase,
            include_vectors=False,
        )
        assert archive.suffix == ".enc"

        restore_dir = tmp_path / "restored"
        summary = restore_full_backup(
            archive, restore_dir, passphrase=passphrase,
        )
        assert summary["instance_id"] == "test-instance-abc123"
        assert "protagine-contacts.db" in summary["databases"]

    def test_wrong_passphrase_fails(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            passphrase=b"correct-pass",
            include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="wrong passphrase"):
            restore_full_backup(
                archive, restore_dir, passphrase=b"wrong-pass",
            )

    def test_encrypted_without_passphrase_fails(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            passphrase=b"correct-pass",
            include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="passphrase required"):
            restore_full_backup(archive, restore_dir)
