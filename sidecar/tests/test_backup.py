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

    (state / "protagine-id").write_text("test-protagine-abc123")

    keys = state / "protagine-keys"
    keys.mkdir()
    (keys / "private.pem").write_text("FAKE_PRIVATE_KEY")
    (keys / "public.pem").write_text("FAKE_PUBLIC_KEY")

    (state / "genesis.json").write_text(json.dumps({"genesis": True}))

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
        "NEO4J_URI=bolt://localhost:7687\n"
        "NEO4J_PASSWORD=neo4j-secret\n"
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
            include_graph=False, include_vectors=False,
        )
        assert archive.exists()
        assert archive.suffix == ".gz"

    def test_restore_recovers_state(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        summary = restore_full_backup(archive, restore_dir)

        assert summary["protagine_id"] == "test-protagine-abc123"
        assert "protagine-contacts.db" in summary["databases"]
        assert (restore_dir / "protagine-id").read_text().strip() == "test-protagine-abc123"
        assert (restore_dir / "protagine-keys" / "public.pem").exists()

        conn = sqlite3.connect(str(restore_dir / "protagine-contacts.db"))
        cur = conn.execute("SELECT name FROM contacts WHERE id = 'c1'")
        assert cur.fetchone()[0] == "Alice"
        conn.close()

    def test_restore_rejects_identity_mismatch(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "other"
        restore_dir.mkdir()
        (restore_dir / "protagine-id").write_text("different-protagine-xyz")

        with pytest.raises(ValueError, match="force-identity"):
            restore_full_backup(archive, restore_dir)

    def test_restore_with_force_identity(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "other"
        restore_dir.mkdir()
        (restore_dir / "protagine-id").write_text("different-protagine-xyz")

        summary = restore_full_backup(
            archive, restore_dir, force_identity=True,
        )
        assert summary["protagine_id"] == "test-protagine-abc123"

    def test_env_file_is_scrubbed_in_backup(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        restore_full_backup(archive, restore_dir)

        env_content = (restore_dir / ".env").read_text()
        assert "super-secret-key" not in env_content
        assert "neo4j-secret" not in env_content
        assert "PROTAGINE_API_KEY=<REDACTED>" in env_content
        assert "PROTAGINE_SIDECAR_PORT=7777" in env_content

    def test_meta_json_in_archive(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            include_graph=False, include_vectors=False,
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
            include_graph=False, include_vectors=False,
        )
        assert archive.suffix == ".enc"

        restore_dir = tmp_path / "restored"
        summary = restore_full_backup(
            archive, restore_dir, passphrase=passphrase,
        )
        assert summary["protagine_id"] == "test-protagine-abc123"
        assert "protagine-contacts.db" in summary["databases"]

    def test_wrong_passphrase_fails(self, protagine_state, output_dir, tmp_path):
        archive = create_full_backup(
            protagine_state, output_dir,
            passphrase=b"correct-pass",
            include_graph=False, include_vectors=False,
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
            include_graph=False, include_vectors=False,
        )

        restore_dir = tmp_path / "restored"
        with pytest.raises(ValueError, match="passphrase required"):
            restore_full_backup(archive, restore_dir)
