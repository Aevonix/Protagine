"""``protagine upgrade``: idempotent, and it repairs what drifted."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
import yaml

from protagine import init
from protagine.init import WORKER_PROFILE
from test_init import STOCK_CONFIG, _args, _snapshot

HERMES_PYTHON = os.environ.get("PROTAGINE_TEST_HERMES_PYTHON") or sys.executable
if not os.environ.get("PROTAGINE_TEST_HERMES_PYTHON"):
    pytest.importorskip("hermes_cli", reason="stock Hermes is not installed in this interpreter")


@pytest.fixture
def installed(tmp_path, monkeypatch):
    home = tmp_path / "protagine"
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(STOCK_CONFIG, sort_keys=False))
    monkeypatch.setenv("PROTAGINE_HOME", str(home))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    assert init.run_init(_args(home, hermes_home)) == 0
    return home, hermes_home


def _upgrade_args(home, **overrides):
    values = {"home": str(home), "hermes_home": None, "hermes_python": HERMES_PYTHON, "adapter_source": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_upgrade_twice_changes_nothing(installed, capsys):
    home, hermes_home = installed
    before_home, before_hermes = _snapshot(home), _snapshot(hermes_home)
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _snapshot(home) == before_home
    assert _snapshot(hermes_home) == before_hermes
    assert not (home / "backups").exists() or not any(p.is_dir() and p.name[:1].isdigit() for p in (home / "backups").iterdir())
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert _snapshot(home) == before_home


def test_upgrade_repairs_drift_after_a_backup(installed, capsys):
    home, hermes_home = installed
    config_path = hermes_home / "config.yaml"
    drifted = yaml.safe_load(config_path.read_text())
    del drifted["memory"]
    drifted["plugins"]["enabled"] = []
    config_path.write_text(yaml.safe_dump(drifted, sort_keys=False))
    (hermes_home / "profiles" / WORKER_PROFILE / "config.yaml").unlink()

    assert init.run_upgrade(_upgrade_args(home)) == 0
    output = capsys.readouterr().out
    assert "backup taken" in output
    assert "memory.provider: protagine-memory" in output
    assert "plugins.enabled += protagine" in output
    assert f"profiles/{WORKER_PROFILE} written" in output
    repaired = yaml.safe_load(config_path.read_text())
    assert repaired["memory"]["provider"] == "protagine-memory"
    assert repaired["plugins"]["enabled"] == ["protagine"]
    assert (hermes_home / "profiles" / WORKER_PROFILE / "config.yaml").is_file()
    backups = [p for p in (home / "backups").iterdir() if p.is_dir()]
    assert len(backups) == 1
    assert (backups[0] / "protagine.yaml").is_file() and (backups[0] / "api.key").is_file()

    before = _snapshot(hermes_home)
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _snapshot(hermes_home) == before


def test_upgrade_records_a_moved_hermes_binding(installed, tmp_path, capsys):
    home, hermes_home = installed
    moved = tmp_path / "hermes-moved"
    moved.mkdir()
    (moved / "config.yaml").write_text(yaml.safe_dump(STOCK_CONFIG, sort_keys=False))
    assert init.run_upgrade(_upgrade_args(home, hermes_home=str(moved))) == 0
    output = capsys.readouterr().out
    assert "Hermes binding recorded" in output
    from protagine.config import load_config
    assert load_config(home, environ={}).get("hermes.home") == str(moved)
    assert yaml.safe_load((moved / "config.yaml").read_text())["plugins"]["enabled"] == ["protagine"]


def test_upgrade_reconciles_a_changed_worker_toolset(installed, capsys):
    """Editing ``mind.worker_toolsets`` or ``mind.deny.commands`` is not "nothing to do"."""
    from protagine.config import load_config, save_config
    home, hermes_home = installed
    cfg = load_config(home, environ={})
    cfg.data["mind"]["worker_toolsets"] = ["web", "file"]
    cfg.data["mind"]["deny"]["commands"] = ["shutdown*"]
    save_config(cfg.data, home)
    assert init.run_upgrade(_upgrade_args(home)) == 0
    output = capsys.readouterr().out
    assert f"profiles/{WORKER_PROFILE} written" in output and "nothing to do" not in output
    profile = yaml.safe_load((hermes_home / "profiles" / WORKER_PROFILE / "config.yaml").read_text())
    assert profile["toolsets"] == ["web", "file"]
    assert profile["approvals"] == {"deny": ["shutdown*"]}
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_upgrade_retires_old_stores_and_adds_the_intention_columns(installed, capsys):
    """State owned by deleted code moves into the backup; the initiatives table gains
    the intention and audit columns in place (architecture 5.2)."""
    import sqlite3
    home, _ = installed
    with sqlite3.connect(home / "initiatives.db") as db:
        db.execute("CREATE TABLE initiatives (id TEXT PRIMARY KEY, dedup_key TEXT UNIQUE, type TEXT NOT NULL, "
                   "description TEXT NOT NULL, priority REAL, rationale TEXT, action_hint TEXT, entity_id TEXT, "
                   "source_type TEXT, source_id TEXT, created_by TEXT, status TEXT, assigned_agent_id TEXT, "
                   "assigned_agent_name TEXT, assigned_at TIMESTAMP, acknowledged_at TIMESTAMP, completed_at TIMESTAMP, "
                   "cancelled_at TIMESTAMP, cancelled_by TEXT, cancelled_reason TEXT, failed_at TIMESTAMP, "
                   "failed_reason TEXT, attempt_count INTEGER, max_attempts INTEGER, timeout_seconds INTEGER, "
                   "last_attempt_at TIMESTAMP, created_at TIMESTAMP, expires_at TIMESTAMP, delivery_mode TEXT, "
                   "delivery_attempts INTEGER, last_delivery_at TIMESTAMP, delivery_failed_at TIMESTAMP, "
                   "delivery_failed_reason TEXT, result TEXT, result_metadata TEXT, preferred_agent_id TEXT, "
                   "stale_reason TEXT, recovery_reason TEXT, job_id TEXT, context TEXT)")
        db.execute("INSERT INTO initiatives (id, type, description, status, created_at) VALUES "
                   "('old-1', 'relationship', 'old row', 'completed', '2026-01-01T00:00:00+00:00')")
    for name in ("approval_authority.db", "schedules.db", "task_queue.db", "protagine-projects.db",
                 "protagine-directives.db"):
        with sqlite3.connect(home / name) as db:
            db.execute("CREATE TABLE t (x TEXT)")
            db.execute("INSERT INTO t VALUES ('row')")
    (home / "standing_approvals.json").write_text("{}")
    (home / "governed-actions").mkdir()
    with sqlite3.connect(home / "governed-actions" / "ledger.db") as db:
        db.execute("CREATE TABLE actions (id TEXT)")
    (home / "bridge").mkdir()
    (home / "bridge" / "seen_initiatives.txt").write_text("init-1\n")
    assert set(init.retired_state_present(home)) == {
        "approval_authority.db", "schedules.db", "standing_approvals.json", "task_queue.db",
        "protagine-projects.db", "protagine-directives.db", "governed-actions", "bridge"}
    assert "initiatives.db:kind" in init.pending_initiative_columns(home)

    assert init.run_upgrade(_upgrade_args(home)) == 0
    out = capsys.readouterr().out
    assert "retired approval_authority.db" in out and "migration applied: initiatives.db:kind" in out
    assert "retired task_queue.db" in out and "retired governed-actions" in out
    for name in ("approval_authority.db", "schedules.db", "standing_approvals.json", "task_queue.db",
                 "protagine-projects.db", "protagine-directives.db", "governed-actions", "bridge"):
        assert not (home / name).exists()
    retired = {p.name for p in (home / "backups").rglob("retired/*")}
    assert retired >= {"approval_authority.db", "schedules.db", "standing_approvals.json", "task_queue.db",
                       "protagine-projects.db", "governed-actions"}
    assert next((home / "backups").rglob("retired/governed-actions/ledger.db")).is_file()
    assert next((home / "backups").rglob("retired/bridge/seen_initiatives.txt")).read_text() == "init-1\n"
    with sqlite3.connect(next((home / "backups").rglob("retired/task_queue.db"))) as db:
        assert db.execute("SELECT x FROM t").fetchone()[0] == "row"
    with sqlite3.connect(home / "initiatives.db") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(initiatives)")}
        assert {"kind", "cls", "decision", "hermes_ref", "outcome", "verified", "verdict", "ask_code"} <= columns
        assert db.execute("SELECT status FROM initiatives WHERE id='old-1'").fetchone()[0] == "completed"
    assert init.pending_initiative_columns(home) == [] and init.retired_state_present(home) == []
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_upgrade_retires_the_drives_milestone_stores_and_tables(installed, capsys):
    """The workspace, cognition, evidence, drive-governance, external-event and surprise
    stores move into the backup; the goal subtask and DAG tables and the legacy perspective
    tables are dropped from their surviving stores after the backup keeps their rows."""
    import sqlite3
    home, _ = installed
    for name in ("protagine-workspace.db", "protagine-cognition.db", "protagine-surprise.db"):
        with sqlite3.connect(home / name) as db:
            db.execute("CREATE TABLE t (x TEXT)")
    with sqlite3.connect(home / "protagine-goals.db") as db:
        db.execute("CREATE TABLE goals (goal_id TEXT PRIMARY KEY, title TEXT)")
        db.execute("CREATE TABLE subtasks (subtask_id TEXT PRIMARY KEY, goal_id TEXT)")
        db.execute("CREATE TABLE goal_dag_versions (id INTEGER PRIMARY KEY, goal_id TEXT)")
        db.execute("INSERT INTO goals VALUES ('g-1', 'keep me')")
        db.execute("INSERT INTO subtasks VALUES ('s-1', 'g-1')")
    with sqlite3.connect(home / "turn-idempotency.db") as db:
        db.execute("CREATE TABLE self_preference_events (id INTEGER PRIMARY KEY, owner_id TEXT)")
        db.execute("CREATE TABLE self_attention (slot INTEGER PRIMARY KEY, snapshot_json TEXT, observed_at REAL)")
        db.execute("INSERT INTO self_attention VALUES (1, '{}', 1.0)")
    assert {"protagine-workspace.db", "protagine-cognition.db", "protagine-surprise.db"} <= set(init.retired_state_present(home))
    assert set(init.retired_tables_present(home)) == {"protagine-goals.db:subtasks", "protagine-goals.db:goal_dag_versions",
                                                      "turn-idempotency.db:self_attention"}

    assert init.run_upgrade(_upgrade_args(home)) == 0
    out = capsys.readouterr().out
    assert "retired protagine-workspace.db" in out and "retired table subtasks in protagine-goals.db (1 rows" in out
    for name in ("protagine-workspace.db", "protagine-cognition.db", "protagine-surprise.db"):
        assert not (home / name).exists()
    with sqlite3.connect(home / "protagine-goals.db") as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "goals" in tables and "subtasks" not in tables and "goal_dag_versions" not in tables
        assert db.execute("SELECT title FROM goals").fetchone()[0] == "keep me"
    with sqlite3.connect(home / "turn-idempotency.db") as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "self_preference_events" in tables and "self_attention" not in tables
    backup = next(p for p in (home / "backups").iterdir() if (p / "protagine-goals.db").exists())
    with sqlite3.connect(backup / "protagine-goals.db") as db:
        assert db.execute("SELECT count(*) FROM subtasks").fetchone()[0] == 1   # the rows survive in the backup
    assert init.retired_tables_present(home) == [] and init.retired_state_present(home) == []
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_upgrade_retires_the_people_milestone_stores(installed, capsys):
    """The theory-of-mind stores the per-contact digest replaced (second-order inferences,
    their exposure ledger, the engagement profiles, the relationship briefs and the P8
    shadow stores) move into the backup instead of staying behind as orphans."""
    import sqlite3
    home, _ = installed
    names = ("protagine-tom2.db", "protagine-tom2-exposure.db", "protagine-engagement.db",
             "protagine-relationships.db", "protagine-p8-visibility.db", "protagine-p8-arcs.db",
             "protagine-p8-recipient-audit.db")
    for name in names:
        with sqlite3.connect(home / name) as db:
            db.execute("CREATE TABLE t (x TEXT)")
            db.execute("INSERT INTO t VALUES ('row')")
    assert set(names) <= set(init.retired_state_present(home))

    assert init.run_upgrade(_upgrade_args(home)) == 0
    out = capsys.readouterr().out
    for name in names:
        assert f"retired {name}" in out and not (home / name).exists()
    with sqlite3.connect(next((home / "backups").rglob("retired/protagine-engagement.db"))) as db:
        assert db.execute("SELECT x FROM t").fetchone()[0] == "row"
    assert init.retired_state_present(home) == []


def test_upgrade_adopts_the_ingress_rows_of_retired_producers(installed, capsys):
    """An earlier line stamped durable intake rows with its client principals; this line
    authenticates one key, so the upgrade re-scopes those rows to the instance producer and
    the transport can read, hand off and settle the receipts it journaled. The backup keeps
    the rows as they were; a second upgrade finds nothing to do."""
    import sqlite3
    from protagine.api.auth import KEY_PRINCIPAL
    from protagine.contacts.transport_ingress import TransportIngress, ensure_schema
    home, _ = installed
    path = home / "protagine-comms.db"

    def opened():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection, TransportIngress(connection)

    def admit(store, producer, sequence, **changes):
        return store.admit(**(dict(producer=producer, account_id="account", epoch="epoch", sequence=sequence,
            event_id="event-" + str(sequence), contact_id="contact", occurred_at=100, journal_ref="journal:" + str(sequence),
            payload_digest="a" * 64, media_available=True, metadata={"channel": "whatsapp", "sender_ref": "fixture"},
            now=200) | changes))

    conn, store = opened()
    ensure_schema(conn)
    old = [admit(store, "legacy-outreach", n) for n in (1, 2, 3)]
    other = admit(store, "legacy-bridge", 1, account_id="second-account")
    store.handoff(producer="legacy-outreach", receipt_ids=[old[1]["receipt_id"]], batch_id="old-batch")
    for producer, account, watermark in (("legacy-outreach", "account", 3), ("legacy-bridge", "second-account", 1)):
        store.observe_coverage(producer=producer, account_id=account, epoch="epoch", connected_since=50,
                               observed_at=201, watermark=watermark, connected=True, unavailable=0, now=201)
    conn.close()
    assert init.pending_ingress_adoption(home) == [
        "protagine-comms.db:transport_ingress (4 receipts from 2 retired producers)",
        "protagine-comms.db:transport_ingress_coverage (2 rows)"]

    assert init.run_upgrade(_upgrade_args(home)) == 0
    out = capsys.readouterr().out
    assert "backup taken" in out
    assert ("migration applied: protagine-comms.db: 4 transport ingress receipts and 2 coverage rows re-scoped "
            "from retired producers (legacy-bridge, legacy-outreach) to the instance key") in out
    conn, store = opened()
    assert {row[0] for row in conn.execute("SELECT DISTINCT producer FROM transport_ingress")} == {KEY_PRINCIPAL}
    assert {row[0] for row in conn.execute("SELECT DISTINCT producer FROM transport_ingress_coverage")} == {KEY_PRINCIPAL}
    ids = [row["receipt_id"] for row in old]
    assert [row["state"] for row in store.receipts(producer=KEY_PRINCIPAL, receipt_ids=ids)] == ["admitted", "handed_off", "admitted"]
    assert store.handoff(producer=KEY_PRINCIPAL, receipt_ids=[ids[0]], batch_id="new-batch")["may_dispatch"] is True
    assert store.receipts(producer=KEY_PRINCIPAL, receipt_ids=[other["receipt_id"]])[0]["state"] == "admitted"
    assert store.coverage(producer=KEY_PRINCIPAL, account_id="account", contact_id="nobody", since=150, now=203)["observed"]
    conn.close()
    backup = next(p for p in (home / "backups").iterdir() if (p / "protagine-comms.db").exists())
    with sqlite3.connect(backup / "protagine-comms.db") as db:
        assert db.execute("SELECT COUNT(*) FROM transport_ingress WHERE producer='legacy-outreach'").fetchone()[0] == 3
    assert init.pending_ingress_adoption(home) == []
    assert init.run_upgrade(_upgrade_args(home)) == 0
    assert "nothing to do" in capsys.readouterr().out
