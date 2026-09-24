"""Command-line setup, operation and diagnostics for Protagine."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="protagine",
        description="Protagine intelligence sidecar server",
    )
    parser.add_argument("--instance", help="Instance directory (default: $PROTAGINE_HOME or ~/.protagine)")
    sub = parser.add_subparsers(dest="command")
    from protagine.qualification.cli import add_parser as add_model_parser
    add_model_parser(sub)

    # --- init and upgrade ---
    from protagine.init import add_parsers as add_init_parsers
    add_init_parsers(sub)

    # --- start ---
    start_p = sub.add_parser("start", help="Start the sidecar server")
    start_p.add_argument("--host", default=None, help="Override listen host")
    start_p.add_argument("--port", type=int, default=None, help="Override listen port")
    start_p.add_argument("--detach", "-d", action="store_true", help="Run in background (daemon mode)")
    start_p.add_argument("--force", "-f", action="store_true", help="Kill existing process on port if needed")

    # --- stop ---
    sub.add_parser("stop", help="Stop the running sidecar")

    # --- status ---
    sub.add_parser("status", help="Check sidecar health and pipeline status")

    # --- service ---
    service_p = sub.add_parser("service", help="Manage the selected private instance user service")
    service_sub = service_p.add_subparsers(dest="service_command")
    service_sub.add_parser("install", help="Install and enable autostart (does not start the process)")
    service_sub.add_parser("uninstall", help="Remove the selected service; retain private data")
    service_sub.add_parser("start", help="Start the selected service and wait for HTTP readiness")
    service_sub.add_parser("stop", help="Stop the selected service")
    service_sub.add_parser("restart", help="Restart the selected service")
    service_sub.add_parser("status", help="Show manager state and HTTP readiness")

    # --- validate ---
    val_p = sub.add_parser("validate", help="Run end-to-end pipeline validation (uses LLM credits)")
    val_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    # --- doctor ---
    doctor_p = sub.add_parser("doctor", help="Check the install: Hermes version, keys, adapter, service, plugin")
    doctor_p.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    doctor_p.add_argument("--url", default=None, help="Sidecar URL (default: from protagine.yaml)")
    doctor_p.add_argument("--api-key", default=None, help="API key (default: api.key)")
    doctor_p.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds (default: 10)")
    doctor_p.add_argument("--fix", action="store_true",
                          help="Apply safe automatic fixes (LLM config baseUrl/apiKey), then re-check")

    # --- generate-types ---
    sub.add_parser("generate-types", help="Export OpenAPI spec (for TypeScript generation)")

    # --- backfill ---
    backfill_p = sub.add_parser("backfill", help="Re-embed all vectors with current model")
    backfill_p.add_argument("--collection", default=None, help="Specific collection to backfill (default: all)")
    backfill_p.add_argument("--batch-size", type=int, default=64, help="Batch size for embedding")

    # --- migrate-tier ---
    migrate_p = sub.add_parser("migrate-tier", help="Migrate vectors from old model to current")
    migrate_p.add_argument("--old-model", default=None, help="Old model ID to migrate from (default: all)")
    migrate_p.add_argument("--batch-size", type=int, default=64, help="Batch size for embedding")
    migrate_p.add_argument("--wait-seconds", type=float, default=600,
                           help="Maximum time to wait for completion; the server job continues (default: 600)")

    # --- activate-multimodal ---
    mm_p = sub.add_parser("activate-multimodal", help="Enable multimodal embeddings and rerank")
    mm_p.add_argument("--model", default=None, help="Multimodal model ID (default: auto-detect from tier)")
    mm_p.add_argument("--storage", default="local", choices=["local", "embed_only"], help="Image storage mode")

    # --- mcp ---
    mcp_p = sub.add_parser("mcp", help="Protagine MCP server and harness configuration")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_command")
    mcp_run = mcp_sub.add_parser("run", help="Start MCP server (stdio transport)")
    mcp_run.add_argument("--transport", choices=["stdio", "http"], default="stdio", help="Transport mode")
    mcp_run.add_argument("--host", default="127.0.0.1", help="HTTP host (for http transport)")
    mcp_run.add_argument("--port", type=int, default=7778, help="HTTP port (for http transport)")

    mcp_setup = mcp_sub.add_parser("setup", help="Configure a coding harness to use Protagine")
    mcp_setup.add_argument("--harness", choices=["claude-code", "codex", "crush", "opencode", "hermes", "all"], default=None, help="Specific harness to configure")
    mcp_setup.add_argument("--contact-id", default=None, help="Your identifier (skip prompt)")
    mcp_setup.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    mcp_setup.add_argument("--print-config", action="store_true", help="Print MCP config snippet (for distributed setups)")
    mcp_setup.add_argument("--sidecar-url", default=None, help="Sidecar URL (for remote Protagine, e.g., http://192.168.1.100:7777)")
    mcp_setup.add_argument("--mcp-command", dest="mcp_server_command", default=None, help="MCP server command (for standalone mode)")
    mcp_setup.add_argument("--mcp-args", default=None, help="MCP server args (for standalone mode)")

    mcp_remove = mcp_sub.add_parser("remove", help="Remove Protagine from a harness config")
    mcp_remove.add_argument("--harness", choices=["claude-code", "codex", "crush", "opencode", "hermes", "all"], default=None, help="Specific harness to remove")
    mcp_remove.add_argument("--dry-run", action="store_true", help="Show changes without writing")

    mcp_sub.add_parser("detect", help="Detect installed coding harnesses")

    # --- backup ---
    backup_p = sub.add_parser("backup", help="Export the instance state (databases, identity, config, vectors) as a portable archive")
    backup_p.add_argument("--full", action="store_true", help="Accepted for compatibility: every backup is a full-state backup")
    backup_p.add_argument("--output", "-o", default=None, help="Output directory (default ~/protagine-backups)")
    backup_p.add_argument("--passphrase", default=None, help="Encrypt backup with this passphrase (prompted if --encrypt)")
    backup_p.add_argument("--encrypt", action="store_true", help="Encrypt backup (prompts for passphrase)")
    backup_p.add_argument("--no-vectors", action="store_true", help="Skip LanceDB vector store")

    # --- restore ---
    restore_p = sub.add_parser("restore", help="Restore Protagine from a backup")
    restore_mode = restore_p.add_mutually_exclusive_group()
    restore_mode.add_argument("--full", action="store_true", help="Reconstruct full archive state; current authority and erasures still require reconciliation")
    restore_mode.add_argument("--memory-only", action="store_true", help="Recover canonical memory using a surviving current source ledger")
    restore_p.add_argument("--input", "-i", default=None, help="Backup file path (default: prompts for it)")
    restore_p.add_argument("--current-state", default=None, help="Surviving authoritative source state (--memory-only)")
    restore_p.add_argument("--output", "-o", default=None, help="Fresh memory bundle destination (--memory-only)")
    restore_p.add_argument("--passphrase", default=None, help="Passphrase to decrypt (default: prompts for it)")
    mm_p.add_argument("--safety", default="basic", choices=["off", "basic", "strict"], help="Image safety level")
    mm_p.add_argument("--skip-download", action="store_true", help="Skip model download")

    # --- secrets ---
    secrets_p = sub.add_parser("secrets", help="Manage the encrypted secrets store (connector credentials, API keys)")
    secrets_sub = secrets_p.add_subparsers(dest="secrets_cmd", required=True)
    secrets_sub.add_parser("list", help="Show configured secrets grouped by category")
    sget = secrets_sub.add_parser("get", help="Retrieve a secret value")
    sget.add_argument("key")
    sset = secrets_sub.add_parser("set", help="Store a secret (e.g. connector/imap/password)")
    sset.add_argument("key")
    sset.add_argument("value")
    sdel = secrets_sub.add_parser("delete", help="Remove a secret")
    sdel.add_argument("key")
    secrets_sub.add_parser("backend", help="Show the active secrets backend")
    secrets_sub.add_parser("status", help="Check backend availability")

    # --- mind ---
    from protagine.mind.cli import add_parser as add_mind_parser
    add_mind_parser(sub)

    # --- feeds ---
    feeds_p = sub.add_parser("feeds", help="Manage spec-driven intelligence feeds")
    feeds_p.add_argument("feeds_args", nargs=argparse.REMAINDER,
                         help="Feeds subcommand (create/validate/list/status/pause/resume/run/delete)")

    # --- agent ---
    agent_p = sub.add_parser("agent", help="Manage connected agents")
    agent_sub = agent_p.add_subparsers(dest="agent_command")

    agent_invite = agent_sub.add_parser("invite", help="Generate a setup code for remote agent")
    agent_invite.add_argument("--expires", type=int, default=900, help="Invite expiry in seconds (default: 900)")
    agent_invite.add_argument("--max-uses", type=int, default=1, help="Max uses (default: 1)")
    agent_invite.add_argument("--capabilities", default="messaging", help="Grant capabilities (comma-separated)")
    agent_invite.add_argument("--primary", action="store_true", help="Grant primary status")
    agent_invite.add_argument("--label", default=None, help="Label for this invite")

    agent_connect = agent_sub.add_parser("connect", help="Connect a remote agent using setup code")
    agent_connect.add_argument("--setup-code", required=True, help="Setup code from protagine agent invite")
    agent_connect.add_argument("--protagine-url", "--protagine-url", dest="protagine_url", default=None, help="Protagine URL (auto-detect if on Tailscale)")
    agent_connect.add_argument("--name", default=None, help="Agent name (default: hostname)")
    agent_connect.add_argument("--capabilities", default=None, help="Request capabilities (comma-separated)")

    agent_list = agent_sub.add_parser("list", help="List registered agents")
    agent_list.add_argument("--status", choices=["online", "busy", "offline", "suspended", "revoked"], default=None, help="Filter by status")
    agent_list.add_argument("--capability", default=None, help="Filter by capability")

    agent_show = agent_sub.add_parser("show", help="Show agent details")
    agent_show.add_argument("agent_id", help="Agent ID")

    agent_revoke = agent_sub.add_parser("revoke", help="Revoke an agent's access")
    agent_revoke.add_argument("agent_id", help="Agent ID to revoke")
    agent_revoke.add_argument("--reason", default=None, help="Reason for revocation")

    agent_sub.add_parser("disconnect", help="Disconnect this agent from Protagine")

    # --- initiative ---
    init_p = sub.add_parser("initiative", help="Manage initiatives")
    init_sub = init_p.add_subparsers(dest="initiative_command")

    init_list = init_sub.add_parser("list", help="List initiatives")
    init_list.add_argument("--status", default=None, help="Filter by status (pending, assigned, acknowledged, completed, failed, cancelled)")
    init_list.add_argument("--agent", default=None, help="Filter by assigned agent")
    init_list.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")

    init_show = init_sub.add_parser("show", help="Show initiative details")
    init_show.add_argument("initiative_id", help="Initiative ID")

    init_cancel = init_sub.add_parser("cancel", help="Cancel an initiative")
    init_cancel.add_argument("initiative_id", help="Initiative ID")
    init_cancel.add_argument("--reason", default=None, help="Reason for cancellation")

    bench_p = sub.add_parser(
        "benchmark", help="Selfhood benchmark: weekly self-improvement metrics")
    bench_p.add_argument("--weeks", type=int, default=8,
                         help="Weeks of rollups to show (default: 8)")
    bench_p.add_argument("--compute", action="store_true",
                         help="Compute rollups now (default: previous week)")
    bench_p.add_argument("--week", default="",
                         help="ISO week to compute (e.g. 2026-W27)")

    args = parser.parse_args()
    if args.instance:
        os.environ["PROTAGINE_INSTANCE_SELECTED"] = "1"
        selected = str(Path(args.instance).expanduser().resolve())
        os.environ["PROTAGINE_HOME"] = selected
        os.environ["PROTAGINE_STATE_DIR"] = selected

    if args.command == "models":
        from protagine.qualification.cli import run
        try:
            code = run(args)
        except (ValueError, OSError, KeyError) as exc:
            print(f"Model qualification failed: {type(exc).__name__}", file=sys.stderr)
            raise SystemExit(2) from None
        raise SystemExit(code)

    if args.command == "init":
        from protagine.init import run_init
        code = run_init(args)
        if code != 0 or args.uninstall:
            sys.exit(code)

    elif args.command == "upgrade":
        from protagine.init import run_upgrade
        sys.exit(run_upgrade(args))

    elif args.command == "start":
        _load_dotenv()
        host = args.host or os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
        port = args.port or int(os.environ.get("PROTAGINE_SIDECAR_PORT", "7777"))

        # Fail closed: never serve an unauthenticated API on a public interface.
        _guard_bind_auth(host)

        # Service-aware: error out if launchd is managing the sidecar
        if _is_service_loaded():
            print("❌ A user service is managing this sidecar.")
            print("  Use 'protagine service stop' and 'protagine service start' instead,")
            sys.exit(1)

        if args.detach:
            _cmd_start_daemon(host, port, args.force)
        else:
            # Foreground mode — check port first
            existing_pid = _find_pid_on_port(port)
            if existing_pid:
                if _configured_instance():
                    print("Selected port is already in use; no process was stopped.")
                    raise SystemExit(1)
                if args.force:
                    print(f"Killing existing process {existing_pid} on port {port}...")
                    try:
                        os.kill(existing_pid, 15)  # SIGTERM
                        time.sleep(2)
                        if _find_pid_on_port(port):
                            os.kill(existing_pid, 9)  # SIGKILL
                            time.sleep(1)
                        print("Process killed.")
                    except ProcessLookupError:
                        pass
                else:
                    print(f"Error: Port {port} is already in use (PID {existing_pid})")
                    print("Use --force to kill existing process, or stop it first with: protagine stop")
                    sys.exit(1)
            
            import uvicorn
            try:
                ws_max_size = int(
                    os.environ.get("PROTAGINE_MAX_WS_FRAME_BYTES", "") or 1 * 1024 * 1024
                )
            except ValueError:
                ws_max_size = 1 * 1024 * 1024
            from protagine.runtime_logging import configure_runtime_logging
            configure_runtime_logging(redirect_stdio=bool(os.environ.get('PROTAGINE_INSTANCE_SERVICE')))
            uvicorn.run(
                "protagine.server:app",
                host=host,
                port=port,
                log_level=os.environ.get("LOG_LEVEL", "info").lower(),
                log_config=None,
                ws_max_size=ws_max_size,
            )

    elif args.command == "stop":
        _load_dotenv()
        # Service-aware: error out if launchd is managing the sidecar
        if _is_service_loaded():
            print("❌ Sidecar is managed by a user service.")
            print("  Use 'protagine service stop' instead.")
            sys.exit(1)
        _cmd_stop()

    elif args.command == "status":
        _load_dotenv()
        _cmd_status()

    elif args.command == "service":
        _load_dotenv()
        if _configured_instance() and args.service_command:
            from protagine.services.instance import manage, ServiceError
            try:
                manage(args.service_command)
            except (ServiceError, ValueError, OSError) as exc:
                print(f"Service operation failed: {exc}", file=sys.stderr)
                raise SystemExit(1) from None
            return
        if not hasattr(args, "service_command") or not args.service_command:
            print("❌ No service subcommand given")
            print("  Usage: protagine service {install|uninstall|start|stop|restart|status}")
            sys.exit(1)
        elif args.service_command == "install":
            _cmd_service_install()
        elif args.service_command == "uninstall":
            _cmd_service_uninstall()
        elif args.service_command == "start":
            _cmd_service_start()
        elif args.service_command == "stop":
            _cmd_service_stop()
        elif args.service_command == "restart":
            _cmd_service_restart()
        elif args.service_command == "status":
            _cmd_service_status()

    elif args.command == "generate-types":
        _load_dotenv()
        import json
        from protagine.server import create_app
        app = create_app()
        spec = app.openapi()
        out = os.environ.get("PROTAGINE_OPENAPI_OUT", "openapi.json")
        with open(out, "w") as f:
            json.dump(spec, f, indent=2)
        n = len(spec.get("components", {}).get("schemas", {}))
        p = len(spec.get("paths", {}))
        print(f"✅ OpenAPI spec written to {out} ({n} schemas, {p} paths)")

    elif args.command == "backfill":
        _load_dotenv()
        import httpx
        host = os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
        port = os.environ.get("PROTAGINE_SIDECAR_PORT", "7777")
        api_key = os.environ.get("PROTAGINE_API_KEY", "")
        try:
            resp = httpx.post(
                f"http://{host}:{port}/v1/host/memory/backfill",
                json={"identity": {"host_id": "cli"}, "collection": args.collection, "batch_size": args.batch_size},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                task_id = data.get("task_id", "")
                print(f"Backfill started (task_id={task_id})")
                while True:
                    time.sleep(2)
                    status_resp = httpx.get(
                        f"http://{host}:{port}/v1/host/memory/backfill/{task_id}",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=10,
                    )
                    if status_resp.status_code == 200:
                        sd = status_resp.json()
                        if sd.get("status") == "completed":
                            print(f"Backfill complete: {sd.get('processed', 0)} processed, {sd.get('skipped', 0)} skipped, {sd.get('failed', 0)} failed")
                            break
                        elif sd.get("status") == "failed":
                            print(f"Backfill failed: {sd.get('errors', [])}")
                            break
                        else:
                            print(f"  ... {sd.get('processed', 0)} processed so far")
            else:
                print(f"Backfill failed: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"Could not connect to sidecar: {e}")

    elif args.command == "migrate-tier":
        if not 0 < args.wait_seconds < float("inf"):
            parser.error("--wait-seconds must be finite and greater than zero")
        _load_dotenv()
        import httpx
        host = os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
        port = os.environ.get("PROTAGINE_SIDECAR_PORT", "7777")
        api_key = os.environ.get("PROTAGINE_API_KEY", "")
        try:
            resp = httpx.post(
                f"http://{host}:{port}/v1/host/memory/migrate",
                json={"identity": {"host_id": "cli"}, "old_model_id": args.old_model, "batch_size": args.batch_size},
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                task_id = data.get("task_id", "")
                print(f"Migration started (task_id={task_id})")
                deadline = time.monotonic() + args.wait_seconds
                status_url = f"http://{host}:{port}/v1/host/memory/migrate/{task_id}"
                while time.monotonic() < deadline:
                    time.sleep(min(2, max(0, deadline - time.monotonic())))
                    status_resp = httpx.get(
                        status_url,
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=10,
                    )
                    if status_resp.status_code == 200:
                        sd = status_resp.json()
                        if sd.get("status") == "completed":
                            print(f"Migration complete: {sd.get('vectors_migrated', 0)} vectors migrated, {sd.get('collections_migrated', 0)} collections")
                            break
                        elif sd.get("status") == "failed":
                            print(f"Migration failed: {sd.get('errors', [])}")
                            print("Re-run protagine migrate-tier with the same settings to resume staged work.")
                            raise SystemExit(1)
                        elif sd.get("status") == "resumable":
                            print("Migration is interrupted, not complete. Re-run protagine migrate-tier with the same settings to resume staged work.")
                            raise SystemExit(2)
                        else:
                            print(f"  ... {sd.get('vectors_migrated', 0)} vectors migrated so far")
                    else:
                        print(f"Migration status unavailable (HTTP {status_resp.status_code}); completion is not confirmed.")
                        print("After checking the sidecar, re-run protagine migrate-tier with the same settings to resume staged work.")
                        raise SystemExit(2)
                else:
                    print(f"Stopped waiting; the server migration may still be running. Check GET {status_url}.")
                    print("After an interruption, re-run protagine migrate-tier with the same settings to resume staged work.")
                    raise SystemExit(2)
            else:
                print(f"Migration failed: {resp.status_code} {resp.text}")
                raise SystemExit(1)
        except Exception as e:
            print(f"Could not connect to sidecar: {e}")
            print("Completion is not confirmed. Check the sidecar before resuming protagine migrate-tier.")
            raise SystemExit(1)

    elif args.command == "activate-multimodal":
        _load_dotenv()
        env_path = Path(os.environ.get("PROTAGINE_STATE_DIR", ".")) / ".env"
        if not env_path.exists():
            # Try sidecar directory
            env_path = Path(__file__).parent / ".env"
        if not env_path.exists():
            print("No .env file found. Run 'protagine init' first.")
            return

        # Determine multimodal model from tier
        model = args.model
        reranker_model = ""
        dims = 0

        if not model:
            try:
                from protagine.vector.tiers import TIER_TABLE
                from protagine.vector.scanner import HardwareScanner
                scanner = HardwareScanner()
                scan = scanner.scan()
                tier = scanner.recommend_tier(scan)
                if tier and tier.multimodal_embedder:
                    model = tier.multimodal_embedder.model_id
                    dims = tier.multimodal_embedder.dims
                    if tier.multimodal_reranker:
                        reranker_model = tier.multimodal_reranker.model_id
                else:
                    print("Your hardware tier does not support multimodal embeddings.")
                    print("Available from Tier 1 (4GB+) with jina-clip-v2.")
                    return
            except Exception as exc:
                print(f"Could not auto-detect tier: {exc}")
                print("Use --model to specify a multimodal model ID.")
                return

        print(f"Activating multimodal embeddings:")
        print(f"  Model: {model}")
        print(f"  Dims: {dims}")
        if reranker_model:
            print(f"  Reranker: {reranker_model}")
        print(f"  Storage: {args.storage}")
        print(f"  Safety: {args.safety}")
        print()

        answer = input("Continue? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return

        # Update .env
        lines = env_path.read_text().splitlines()
        updates = {
            "PROTAGINE_MULTIMODAL": "true",
            "PROTAGINE_EMBED_MODEL": model,
            "PROTAGINE_IMAGE_STORAGE": args.storage,
            "PROTAGINE_IMAGE_SAFETY": args.safety,
            "PROTAGINE_STRIP_EXIF_GPS": "true",
        }
        if dims:
            updates["PROTAGINE_EMBED_DIMS"] = str(dims)
        if reranker_model:
            updates["PROTAGINE_RERANKER_MODEL"] = reranker_model

        existing_keys = set()
        for i, line in enumerate(lines):
            if "=" in line and not line.strip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                existing_keys.add(key)
                if key in updates:
                    lines[i] = f"{key}={updates[key]}"

        # Add new keys not yet in .env
        for key, value in updates.items():
            if key not in existing_keys:
                lines.append(f"{key}={value}")

        env_path.write_text("\n".join(lines) + "\n")
        print(f"\n✅ .env updated with multimodal config")

        # Download model
        if not args.skip_download:
            print(f"Downloading multimodal model {model}...")
            try:
                from sentence_transformers import SentenceTransformer
                SentenceTransformer(model)
                print(f"✅ Model downloaded and cached")
            except Exception as exc:
                print(f"⚠️ Model download failed: {exc}")
                print("The model will download on first start instead.")

        print()
        print("Restart the sidecar to activate multimodal: protagine start")
        print("If you have existing text vectors, run: protagine migrate-tier")

    elif args.command == "validate":
        _load_dotenv()
        _cmd_validate(args)

    elif args.command == "mcp":
        _load_dotenv()
        _cmd_mcp(args)

    elif args.command == "doctor":
        _cmd_doctor(args)

    elif args.command == "backup":
        _cmd_backup(args)

    elif args.command == "restore":
        _cmd_restore(args)

    elif args.command == "agent":
        _cmd_agent(args)

    elif args.command == "initiative":
        _cmd_initiative(args)

    elif args.command == "benchmark":
        _cmd_benchmark(args)

    elif args.command == "secrets":
        _load_dotenv()
        from protagine.secrets import cli as _secrets_cli
        _handler = getattr(_secrets_cli, f"cmd_secrets_{args.secrets_cmd}")
        _handler(args)

    elif args.command == "mind":
        _load_dotenv()
        from protagine.mind.cli import run as run_mind_command
        sys.exit(run_mind_command(args))

    elif args.command == "feeds":
        from protagine.feeds.cli import main as feeds_main
        feeds_main(args.feeds_args)

    else:
        parser.print_help()


def _cmd_backup(args) -> None:
    """Export the instance state as a portable archive."""
    _load_dotenv()
    state_dir = os.environ.get("PROTAGINE_STATE_DIR", os.getcwd())

    passphrase = None
    if args.passphrase:
        passphrase = args.passphrase.encode()
    elif args.encrypt:
        import getpass
        passphrase = getpass.getpass("Backup passphrase: ").encode()

    from protagine.backup import create_full_backup
    output_dir = args.output or os.path.expanduser("~/protagine-backups")
    try:
        archive = create_full_backup(
            state_dir, output_dir,
            passphrase=passphrase,
            include_vectors=not getattr(args, "no_vectors", False),
        )
        print(f"  Full backup saved to {archive}")
    except FileNotFoundError as e:
        print(f"  Error: {e}")
        print("  Run 'protagine init' first.")
        raise SystemExit(1)


def _cmd_restore(args) -> None:
    """Restore Protagine from a backup -- interactive by default."""
    _load_dotenv()
    state_dir = os.environ.get("PROTAGINE_STATE_DIR", os.getcwd())
    memory_only = getattr(args, "memory_only", False)
    if memory_only and (not getattr(args, "current_state", None)
                        or not getattr(args, "output", None)):
        print("  Memory recovery requires --current-state and a fresh --output.")
        raise SystemExit(2)
    if not memory_only and (getattr(args, "current_state", None) or getattr(args, "output", None)):
        print("  --current-state and --output require --memory-only.")
        raise SystemExit(2)

    if args.input:
        backup_path = args.input
    else:
        backup_path = input("  Backup file path: ").strip()

    if not backup_path or not Path(backup_path).exists():
        print(f"  Error: File not found: {backup_path}")
        raise SystemExit(1)

    passphrase = None
    if args.passphrase:
        passphrase = args.passphrase.encode()
    if passphrase is None and backup_path.endswith(".enc"):
        import getpass
        passphrase = getpass.getpass("  Backup passphrase: ").encode()

    from protagine.backup import restore_full_backup, restore_source_memory
    try:
        if memory_only:
            summary = restore_source_memory(
                backup_path, args.output, current_state=args.current_state,
                passphrase=passphrase,
            )
            print(f"\n  Current source memory recovered to {args.output}")
            print(f"  Sources: {summary['source_count']}; original images: {summary['source_images']}")
            print("  Files: turn-idempotency.db, owned images and source-memory-recovery.json.")
            print("  Install only these memory files with writers stopped and separately current runtime bindings.")
            print("  The bundle does not restore runtime authority or completed-effect state.")
            return
        summary = restore_full_backup(backup_path, state_dir, passphrase=passphrase)
        print(f"\n  Archive reconstructed: instance {summary['instance_id']}")
        print(f"  Databases: {', '.join(summary.get('databases', []))}")
        print("  Reconcile current authority, erasures and completed effects before starting services.")
        print("  Use --memory-only for bounded recovery from a surviving current source ledger.")
    except ValueError as e:
        print(f"  Error: {e}")
        raise SystemExit(1)


def _cmd_agent(args) -> None:
    """Handle agent subcommands."""
    _load_dotenv()

    import httpx
    host = os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("PROTAGINE_SIDECAR_PORT", "7777")
    api_key = os.environ.get("PROTAGINE_API_KEY", "")
    base_url = f"http://{host}:{port}/v1/host"
    headers = {"Authorization": f"Bearer {api_key}"}

    if args.agent_command == "invite":
        resp = httpx.post(
            f"{base_url}/agents/invite",
            json={
                "expires_in_seconds": args.expires,
                "max_uses": args.max_uses,
                "granted_capabilities": args.capabilities.split(",") if args.capabilities else ["messaging"],
                "granted_is_primary": args.primary,
                "label": args.label,
            },
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        print(f"Setup code: {data['code']}")
        print(f"Expires at: {data['expires_at']}")
        print(f"\nRun on remote agent:")
        print(f"  {data['setup_command']}")

    elif args.agent_command == "connect":
        import socket
        name = args.name or socket.gethostname()
        resp = httpx.post(
            f"{base_url}/agents/connect",
            json={
                "setup_code": args.setup_code,
                "name": name,
                "node_public_key": str(uuid.uuid4()),  # DEV ONLY: real keypair generated at first startup in server.py
                "capabilities": args.capabilities.split(",") if args.capabilities else None,
            },
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        # Save agent config
        agent_dir = Path.home() / ".protagine"
        agent_dir.mkdir(exist_ok=True)
        agent_config = agent_dir / "agent.json"
        agent_config.write_text(json.dumps(data, indent=2))
        print(f"Agent connected: {data['agent_id']}")
        print(f"Node ID: {data['node_id']}")
        print(f"Protagine ID: {data['protagine_id']}")
        print(f"WebSocket URL: {data.get('websocket_url', 'N/A')}")
        print(f"\nConfig saved to: {agent_config}")

    elif args.agent_command == "list":
        params = {}
        if args.status:
            params["status"] = args.status
        if args.capability:
            params["capability"] = args.capability
        resp = httpx.get(f"{base_url}/agents", params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        agents = data.get("agents", [])
        if not agents:
            print("No agents found.")
            return
        print(f"{'Agent ID':<36} {'Name':<20} {'Status':<10} {'Capabilities'}")
        print("-" * 100)
        for a in agents:
            caps = ", ".join(a.get("capabilities", []))
            print(f"{a['agent_id']:<36} {a['name']:<20} {a['status']:<10} {caps}")

    elif args.agent_command == "show":
        resp = httpx.get(f"{base_url}/agents/{args.agent_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        a = resp.json()
        print(f"Agent ID: {a['agent_id']}")
        print(f"Node ID: {a['node_id']}")
        print(f"Name: {a['name']}")
        print(f"Protagine ID: {a['protagine_id']}")
        print(f"Connection: {a['connection_mode']}")
        print(f"Status: {a['status']}")
        print(f"Primary: {a['is_primary']}")
        print(f"Priority: {a['priority']}")
        print(f"Capabilities: {', '.join(a['capabilities'])}")
        print(f"Current assignments: {a['current_assignments']}")
        print(f"Max concurrent: {a['max_concurrent']}")
        print(f"Registered: {a['registered_at']}")
        if a.get('last_seen_at'):
            print(f"Last seen: {a['last_seen_at']}")

    elif args.agent_command == "revoke":
        resp = httpx.delete(f"{base_url}/agents/{args.agent_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        print(f"Agent {args.agent_id} revoked.")

    elif args.agent_command == "disconnect":
        # Read agent config to get agent_id
        agent_config = Path.home() / ".protagine" / "agent.json"
        if not agent_config.exists():
            print("No agent config found. Not connected?")
            return
        data = json.loads(agent_config.read_text())
        agent_id = data.get("agent_id")
        if agent_id:
            resp = httpx.delete(f"{base_url}/agents/{agent_id}", headers=headers, timeout=10)
            if resp.status_code == 200:
                print(f"Disconnected agent {agent_id}")
        # Remove config
        agent_config.unlink(missing_ok=True)
        print("Agent config removed.")

    else:
        print("Usage: protagine agent [invite|connect|list|show|revoke|disconnect]")


def _cmd_benchmark(args) -> None:
    """Show (and optionally compute) the selfhood-benchmark scorecard."""
    _load_dotenv()
    import httpx
    host = os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("PROTAGINE_SIDECAR_PORT", "7777")
    api_key = os.environ.get("PROTAGINE_API_KEY", "")
    base_url = f"http://{host}:{port}/v1/host/self/benchmark"
    headers = {"Authorization": f"Bearer {api_key}"}

    if args.compute:
        resp = httpx.post(f"{base_url}/compute",
                          params={"week": args.week} if args.week else None,
                          headers=headers, timeout=120)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        if not data.get("available"):
            print("Benchmark unavailable (PROTAGINE_BENCHMARK_ENABLED=false?)")
            raise SystemExit(1)
        if data.get("error"):
            print(f"Error: {data['error']}")
            raise SystemExit(1)
        print(f"Computed {data.get('week')}: "
              f"{len(data.get('metrics') or {})} metrics")

    resp = httpx.get(base_url, params={"weeks": args.weeks},
                     headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        raise SystemExit(1)
    data = resp.json()
    if not data.get("available"):
        print("Benchmark unavailable (PROTAGINE_BENCHMARK_ENABLED=false?)")
        raise SystemExit(1)
    weeks = data.get("weeks") or []
    if not weeks:
        print("No rollups yet (first weekly compute pending). "
              "Run: protagine benchmark --compute")
        return
    rollups = data.get("rollups", {})
    trends = data.get("trends", {})
    metrics = sorted({m for wk in weeks for m in rollups.get(wk, {})})
    hdr = f"{'metric':<28}" + "".join(f"{wk:>12}" for wk in weeks)
    print(hdr)
    print("-" * len(hdr))
    for m in metrics:
        row = f"{m:<28}"
        for wk in weeks:
            v = (rollups.get(wk, {}).get(m) or {}).get("value")
            row += f"{v:>12.2f}" if v is not None else f"{'-':>12}"
        d = trends.get(m)
        if d is not None:
            row += f"   {'+' if d >= 0 else ''}{d:.2f} wow"
        print(row)


def _cmd_initiative(args) -> None:
    """Handle initiative subcommands."""
    _load_dotenv()

    import httpx
    host = os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("PROTAGINE_SIDECAR_PORT", "7777")
    api_key = os.environ.get("PROTAGINE_API_KEY", "")
    base_url = f"http://{host}:{port}/v1/host"
    headers = {"Authorization": f"Bearer {api_key}"}

    if args.initiative_command == "list":
        params = {"limit": args.limit}
        if args.status:
            params["status"] = args.status
        if args.agent:
            params["agent_id"] = args.agent
        resp = httpx.get(f"{base_url}/initiatives", params=params, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        data = resp.json()
        initiatives = data.get("initiatives", [])
        if not initiatives:
            print("No initiatives found.")
            return
        print(f"{'ID':<36} {'Type':<15} {'Status':<12} {'Priority':<8} {'Description'}")
        print("-" * 120)
        for i in initiatives:
            desc = i['description'][:60] + "..." if len(i['description']) > 60 else i['description']
            print(f"{i['id']:<36} {i['initiative_type']:<15} {i['status']:<12} {i['priority']:<8} {desc}")

    elif args.initiative_command == "show":
        resp = httpx.get(f"{base_url}/initiatives/{args.initiative_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        i = resp.json()
        print(f"Initiative ID: {i['id']}")
        print(f"Type: {i['initiative_type']}")
        print(f"Status: {i['status']}")
        print(f"Priority: {i['priority']}")
        print(f"Description: {i['description']}")
        if i.get('assigned_agent_id'):
            print(f"Assigned to: {i['assigned_agent_id']}")
        if i.get('result'):
            print(f"Result: {i['result']}")
        if i.get('error_message'):
            print(f"Error: {i['error_message']}")
        print(f"Created: {i['created_at']}")
        if i.get('completed_at'):
            print(f"Completed: {i['completed_at']}")
        if i.get('failed_at'):
            print(f"Failed: {i['failed_at']}")

    elif args.initiative_command == "cancel":
        resp = httpx.post(
            f"{base_url}/initiatives/{args.initiative_id}/cancel",
            json={"reason": args.reason},
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"Error: {resp.text}")
            raise SystemExit(1)
        print(f"Initiative {args.initiative_id} cancelled.")

    else:
        print("Usage: protagine initiative [list|show|cancel]")


def _is_loopback_host(host: str) -> bool:
    """True if the bind host only accepts local connections."""
    from protagine.api.auth import is_loopback_host
    return is_loopback_host(host)


def _guard_bind_auth(host: str) -> None:
    """Refuse to start an unauthenticated sidecar on a non-loopback interface.

    Without a key the API runs in dev mode and ApiKeyMiddleware serves loopback
    callers only, so a non-loopback bind would answer every remote request
    with 403. Fail at startup with the fix instead.
    """
    from protagine.api.auth import configured_api_key
    if _is_loopback_host(host):
        return
    if configured_api_key():
        return
    print(
        f"Refusing to start: binding to {host!r} (non-loopback) with no API key; "
        "without a key the API only serves loopback clients.\n"
        "  Fix one of:\n"
        "    - write the key to api.key in the instance directory (protagine init "
        "does this) or set PROTAGINE_API_KEY=<secret>, or\n"
        "    - bind to 127.0.0.1 (default) and reach it via SSH/proxy.",
        file=sys.stderr,
    )
    sys.exit(2)


def _find_pid_on_port(port: int) -> int | None:
    """Find the PID of a process listening on the given port.

    NOTE: This only finds LISTEN sockets, not client connections.
    """
    pids = _find_pids_on_port(port)
    return pids[0] if pids else None


def _find_pids_on_port(port: int) -> list[int]:
    """Find ALL PIDs in LISTEN state on the given port."""
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        )
        return [int(p) for p in result.stdout.strip().splitlines() if p.isdigit()]
    except Exception:
        return []


def _is_service_loaded() -> bool:
    """Check the selected user service, retaining legacy launchd behavior."""
    if _configured_instance():
        from protagine.services.instance import InstanceService
        service = InstanceService.selected()
        if os.environ.get("PROTAGINE_INSTANCE_SERVICE") == service.label:
            return False  # This foreground invocation belongs to that manager.
        if not service.link.exists() and not service.link.is_symlink():
            return False
        state = service.status()
        return (state["loaded"] if service.platform == "darwin" else
                state.get("state") in {"active", "activating", "deactivating"})
    if not shutil.which("launchctl"):
        return False
    result = subprocess.run(
        ["launchctl", "list", "ai.aevonix.protagine-sidecar"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False  # Label not known to launchd
    # Output format: "PID\tStatus\tLabel" or "-\tStatus\tLabel"
    parts = result.stdout.strip().split()
    return len(parts) >= 1 and parts[0].isdigit()


def _get_plist_path() -> Path:
    """Return the path to the launchd plist file."""
    return Path.home() / "Library" / "LaunchAgents" / "ai.aevonix.protagine-sidecar.plist"


def _get_uvicorn_path() -> str:
    """Return the path to the uvicorn executable."""
    venv_uvicorn = Path.home() / ".protagine-venv" / "bin" / "uvicorn"
    if venv_uvicorn.exists():
        return str(venv_uvicorn)
    # Fallback: find in PATH
    for path_dir in os.environ.get("PATH", "/usr/bin:/bin").split(":"):
        candidate = Path(path_dir) / "uvicorn"
        if candidate.exists():
            return str(candidate)
    return "uvicorn"


def _get_state_dir() -> Path:
    """Return the Protagine state directory."""
    from protagine import get_state_dir
    return get_state_dir()


def _find_orphan_processes() -> list[int]:
    """Find orphaned protagine sidecar processes (parent died).
    
    Returns list of PIDs that are:
    - Running uvicorn/protagine
    - Have parent PID 1 (init) or parent doesn't exist
    """
    orphans = []
    try:
        # Find all python processes running uvicorn or protagine
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return orphans
        
        for line in result.stdout.splitlines():
            # Look for protagine sidecar processes
            if "uvicorn" in line and "protagine" in line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1])
                        ppid = int(parts[2]) if len(parts) > 2 else 0
                        # Parent PID 1 = orphan (reparented to init)
                        # Also check if parent exists
                        if ppid == 1:
                            orphans.append(pid)
                        elif ppid > 1:
                            try:
                                os.kill(ppid, 0)  # Check if parent exists
                            except OSError:
                                # Parent doesn't exist, this is an orphan
                                orphans.append(pid)
                    except (ValueError, OSError):
                        pass
    except Exception:
        pass
    return orphans


def _cleanup_orphans(kill: bool = False) -> int:
    """Find and optionally kill orphaned protagine processes.
    
    Args:
        kill: If True, kill the orphans; if False, just report them
    
    Returns:
        Number of orphans found
    """
    orphans = _find_orphan_processes()
    if not orphans:
        return 0
    
    if kill:
        for pid in orphans:
            try:
                os.kill(pid, 15)  # SIGTERM
                print(f"  Killed orphan process {pid}")
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"  Failed to kill {pid}: {e}")
        time.sleep(1)
        # Check if any survived
        for pid in orphans:
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)  # SIGKILL
                print(f"  Force-killed stubborn process {pid}")
            except ProcessLookupError:
                pass
    
    return len(orphans)


def _cmd_start_daemon(host: str, port: int, force: bool) -> None:
    """Start the sidecar as a background daemon."""
    # Clean up any orphaned processes first
    local_instance = _configured_instance()
    if local_instance:
        from protagine.setup import _check_port
        if _check_port(port):
            print("Selected port is already in use; no process was stopped.")
            raise SystemExit(1)
    orphan_count = 0 if local_instance else _cleanup_orphans(kill=True)
    if orphan_count:
        print(f"  Cleaned up {orphan_count} orphan process(es)")

    # Check if port is already in use
    existing_pids = _find_pids_on_port(port)
    if existing_pids:
        if local_instance:
            print("Selected port is already in use; no process was stopped.")
            raise SystemExit(1)
        if force:
            for pid in existing_pids:
                print(f"  Killing existing process {pid} on port {port}...")
                try:
                    os.kill(pid, 15)  # SIGTERM
                except ProcessLookupError:
                    pass
            # Wait up to 5s for all to die
            for _ in range(10):
                if not _find_pids_on_port(port):
                    break
                time.sleep(0.5)
            # Escalate to SIGKILL for any survivors
            for pid in _find_pids_on_port(port):
                try:
                    os.kill(pid, 9)  # SIGKILL
                except ProcessLookupError:
                    pass
            time.sleep(0.5)
            print(f"  ✅ Process(es) killed")
        else:
            print(f"  ⚠️ Port {port} is already in use (PIDs {existing_pids})")
            try:
                answer = input("  Kill existing process and restart? [Y/n] ").strip().lower()
            except EOFError:
                answer = "y"  # Default to yes when no stdin (e.g. scripts)
            if answer in ("n", "no"):
                print("  Cancelled.")
                return
            for pid in existing_pids:
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass
            for _ in range(10):
                if not _find_pids_on_port(port):
                    break
                time.sleep(0.5)
            for pid in _find_pids_on_port(port):
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
            time.sleep(0.5)
            print(f"  ✅ Process(es) killed")

    # Build env from .env values
    env = {**os.environ}
    env["PROTAGINE_SIDECAR_HOST"] = host
    env["PROTAGINE_SIDECAR_PORT"] = str(port)

    # Start uvicorn
    from protagine.runtime_logging import runtime_log_directory
    log_path = runtime_log_directory() / "sidecar.log"
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    env['PROTAGINE_LOG_PATH'] = str(log_path)
    env['PROTAGINE_RUNTIME_LOGGING'] = '1'
    print(f"  Starting Protagine sidecar on {host}:{port}...")
    print(f"  Log: {log_path}")

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "protagine.server:app",
         "--host", host,
         "--port", str(port)],
        stdin=subprocess.DEVNULL,
        stdout=open(log_path, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=env,
    )

    # Write PID file
    pid_path = Path(os.environ.get("PROTAGINE_STATE_DIR", ".")) / "sidecar.pid"
    pid_path.write_text(str(proc.pid))
    # Wait for health check
    _load_dotenv()
    api_key = os.environ.get("PROTAGINE_CLIENT_API_KEY") or os.environ.get("PROTAGINE_API_KEY", "dev-mode-no-key")
    try:
        healthy = _wait_for_sidecar(host, port, api_key, timeout=20.0)
    except BaseException:
        if local_instance and proc.poll() is None:
            proc.terminate()
        raise
    finally:
        if local_instance and proc.poll() is None:
            # macOS Python launchers exec the framework interpreter during startup.
            # Retain the serving child, including after an interrupted wait.
            record = pid_path.with_name("sidecar-process.json")
            record.write_text(json.dumps({"pid": proc.pid, "signature": _process_signature(proc.pid)}))
            record.chmod(0o600)
    if healthy:
        import httpx
        try:
            r = httpx.get(
                f"http://{host}:{port}/v1/host/health",
                headers={"X-API-Key": api_key},
                timeout=2,
            )
            data = r.json()
            caps = len(data.get("capabilities", []))
            print(f"  ✅ Sidecar running (PID {proc.pid}, {caps} capabilities)")
            # Check E2E validation status
            stamp = Path(os.environ.get("PROTAGINE_STATE_DIR", ".")) / ".protagine-e2e-validated"
            if not stamp.exists():
                print(f"  ⚠️ E2E pipeline not validated — run 'protagine validate' to test")
            return
        except Exception:
            pass
    else:
        print(f"  ❌ Sidecar didn't become healthy within 20s")
        # Print log tail
        if log_path.exists():
            try:
                lines = log_path.read_text().splitlines()
                tail = lines[-20:] if len(lines) > 20 else lines
                print(f"\n  Last log lines:")
                for line in tail:
                    print(f"    {line}")
            except Exception:
                pass
        print(f"  PID: {proc.pid}")
        sys.exit(1)


def _wait_for_sidecar(host: str, port: int, api_key: str, timeout: float = 10.0) -> bool:
    """Poll /health until the sidecar responds or timeout."""
    import httpx
    headers = {"X-API-Key": api_key}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(
                f"http://{host}:{port}/v1/host/health",
                headers=headers,
                timeout=2,
            )
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _process_signature(pid: int) -> str:
    """PID creation time and command prevent a stale file targeting PID reuse."""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                            capture_output=True, text=True, timeout=5)
    return result.stdout.strip() if result.returncode == 0 else ""


def _cmd_stop() -> None:
    """Stop the running sidecar."""
    # Try PID file first
    pid_path = Path(os.environ.get("PROTAGINE_STATE_DIR", ".")) / "sidecar.pid"
    port = int(os.environ.get("PROTAGINE_SIDECAR_PORT", "7777"))

    pid = None
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
        except ValueError:
            pass

    # New private instances never infer process ownership from a port alone.
    if not pid and _configured_instance():
        print("No recorded process for this instance.")
        return
    if _configured_instance():
        try:
            record_path = pid_path.with_name("sidecar-process.json")
            record = json.loads(record_path.read_text())
            expected = record.get("signature")
            if record.get("pid") != pid or not expected or _process_signature(pid) != expected:
                raise ValueError()
        except (OSError, ValueError, subprocess.SubprocessError):
            print("Recorded process is gone or changed; no process was stopped.")
            return
        os.kill(pid, 15)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _process_signature(pid) == expected:
            time.sleep(.1)
        if _process_signature(pid) == expected:
            print("Sidecar has not stopped yet; inspect this instance's log before retrying.")
            raise SystemExit(1)
        pid_path.unlink(missing_ok=True)
        record_path.unlink(missing_ok=True)
        print("Private instance stopped.")
        return
    # Fallback: find by port
    if not pid:
        pid = _find_pid_on_port(port)

    if not pid:
        print(f"  No sidecar process found on port {port}")
        return

    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"  Stopping sidecar (PID {pid})...")
        time.sleep(2)

        # Check if still alive
        if _find_pid_on_port(port):
            print(f"  Process didn't stop gracefully, killing...")
            os.kill(pid, 9)  # SIGKILL
            time.sleep(1)

        print(f"  ✅ Sidecar stopped")

        # Clean up PID file
        if pid_path.exists():
            pid_path.unlink()

    except ProcessLookupError:
        print(f"  Process {pid} already gone")
        if pid_path.exists():
            pid_path.unlink()
        
        # Check for orphan cleanup
        orphan_count = _cleanup_orphans(kill=True)
        if orphan_count:
            print(f"  Cleaned up {orphan_count} orphan process(es)")


def _cmd_status() -> None:
    """Check sidecar health and pipeline status."""
    import httpx

    host = os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("PROTAGINE_SIDECAR_PORT", "7777")
    url = f"http://{host}:{port}"
    api_key = os.environ.get("PROTAGINE_CLIENT_API_KEY") or os.environ.get("PROTAGINE_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        resp = httpx.get(f"{url}/v1/host/health", headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status", "unknown")
        caps = data.get("capabilities", [])
        notes = data.get("notes", {})
        if _configured_instance():
            print(f"Instance: {os.environ.get('PROTAGINE_STATE_DIR', '')}")
            print(f"Sidecar HTTP status: {status}; endpoint: {url}")
            print("Verify recollection by telling Hermes a fact, then asking in a new session.")
            return

        # Status icon
        icon = "🟢" if status == "ok" else "🔴"
        print(f"{icon} Protagine Sidecar — {status}")
        print(f"  URL: {url}")
        print(f"  Capabilities: {len(caps)}")

        # Show notable notes
        for k, v in notes.items():
            if "fail" in str(v).lower() or "error" in str(v).lower() or "not wired" in str(v).lower():
                print(f"  ⚠️  {k}: {v}")

        # Check E2E validation stamp
        stamp = Path(os.environ.get("PROTAGINE_STATE_DIR", ".")) / ".protagine-e2e-validated"
        if stamp.exists():
            stamp_data = json.loads(stamp.read_text())
            validated_at = stamp_data.get("validated_at", "unknown")
            print(f"  ✅ E2E validated: {validated_at}")
        else:
            print(f"  ⚠️  E2E pipeline not validated")
            print(f"     Run 'protagine validate' to test the full pipeline")

    except Exception as exc:
        print(f"🔴 Sidecar not reachable: {exc}")
        # Check if process exists
        pid = _find_pid_on_port(int(port))
        if pid:
            print(f"  Process {pid} is on port {port} but not responding")
        else:
            print(f"  No process on port {port}")
            print(f"  Start with: protagine start")
        sys.exit(1)


def _cmd_service_install() -> None:
    """Install the launchd service."""
    plist_path = _get_plist_path()
    log_dir = _get_state_dir().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Validate uvicorn exists
    uvicorn_path = _get_uvicorn_path()
    if not Path(uvicorn_path).exists():
        print(f"❌ uvicorn not found at {uvicorn_path}")
        print("Make sure the Protagine virtual environment is set up.")
        sys.exit(1)

    _load_dotenv()
    host = os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("PROTAGINE_SIDECAR_PORT", "7777")
    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    working_dir = str(Path(__file__).parent.parent)
    home = str(Path.home())
    state_dir = str(_get_state_dir())
    pythonpath = working_dir

    # Build PATH with venv first
    venv_bin = str(Path.home() / ".protagine-venv" / "bin")
    path_env = f"{venv_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    # Read template
    template_path = Path(__file__).parent / "service_template.plist"
    if not template_path.exists():
        print(f"❌ Template not found: {template_path}")
        sys.exit(1)
    template = template_path.read_text()

    plist_content = template.format(
        uvicorn_path=uvicorn_path,
        host=host,
        port=port,
        log_level=log_level,
        working_dir=working_dir,
        home=home,
        path=path_env,
        state_dir=state_dir,
        pythonpath=pythonpath,
        log_path=str(log_dir / "sidecar.log"),
    )

    plist_path.write_text(plist_content)
    print(f"✅ Plist written to {plist_path}")

    # Check if already loaded
    result = subprocess.run(
        ["launchctl", "list", "ai.aevonix.protagine-sidecar"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("  Service already loaded, reloading...")
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        time.sleep(0.5)

    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print("✅ Service loaded")
    print(f"  Logs: {log_dir / 'sidecar.log'}")
    print(f"  Check status: protagine service status")


def _cmd_service_uninstall() -> None:
    """Uninstall the launchd service."""
    plist_path = _get_plist_path()
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        print(f"✅ Service uninstalled: {plist_path}")
    else:
        print("ℹ️  Service not installed")


def _cmd_service_start() -> None:
    """Start the launchd service."""
    plist_path = _get_plist_path()
    if not plist_path.exists():
        print("❌ Service not installed. Run: protagine service install")
        sys.exit(1)

    # Check if already loaded
    if _is_service_loaded():
        print("ℹ️  Service already loaded and running")
        return

    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print("✅ Service started")


def _cmd_service_stop() -> None:
    """Stop the launchd service (unload)."""
    plist_path = _get_plist_path()
    if not plist_path.exists():
        print("ℹ️  Service not installed")
        return

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    print("✅ Service stopped")


def _cmd_service_restart() -> None:
    """Restart the launchd service."""
    plist_path = _get_plist_path()
    if not plist_path.exists():
        print("❌ Service not installed. Run: protagine service install")
        sys.exit(1)

    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    time.sleep(0.5)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print("✅ Service restarted")


def _cmd_service_status() -> None:
    """Show launchd service status."""
    result = subprocess.run(
        ["launchctl", "list", "ai.aevonix.protagine-sidecar"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("🔴 Service not installed or not loaded")
        print("  Install: protagine service install")
        return

    lines = result.stdout.strip().splitlines()
    if not lines:
        print("⚠️  Unexpected output from launchctl")
        return

    parts = lines[0].split()
    if len(parts) >= 3:
        pid_str, status, label = parts[0], parts[1], parts[2]
        if pid_str == "-":
            print(f"🟡 Service loaded but not running")
            print(f"  Label: {label}")
            print(f"  Status: {status}")
        else:
            print(f"🟢 Service running")
            print(f"  PID: {pid_str}")
            print(f"  Label: {label}")
    else:
        print(f"⚠️  Unexpected format: {lines[0]}")


def _cmd_mcp(args) -> None:
    """Handle protagine mcp subcommands."""
    from protagine.mcp.server import create_server, run_stdio, run_http
    from protagine.mcp.config import (
        HARNESS_DEFS, detect_harnesses, add_to_harness, remove_from_harness,
    )

    if not hasattr(args, "mcp_command") or not args.mcp_command:
        # Default: run the MCP server
        run_stdio()
        return

    if args.mcp_command == "run":
        if args.transport == "http":
            run_http(host=args.host, port=args.port)
        else:
            run_stdio()

    elif args.mcp_command == "detect":
        detected = detect_harnesses()
        print("Detected coding harnesses:")
        for hid, installed in detected.items():
            status = "installed" if installed else "not found"
            icon = "  \u2705" if installed else "  \u274c"
            print(f"{icon} {HARNESS_DEFS[hid]['display']:15s} {status}")

    elif args.mcp_command == "setup":
        # Explicit launch overrides apply once at this command boundary.
        overrides = {"MCP_COMMAND": getattr(args, "mcp_server_command", None),
                     "MCP_ARGS": getattr(args, "mcp_args", None)}
        if any(value is not None for value in overrides.values()):
            for key, value in overrides.items():
                if value is not None:
                    os.environ.pop("PROTAGINE_" + key, None)
                    os.environ["PROTAGINE_" + key] = value
        # Handle --print-config (for distributed setups)
        if getattr(args, 'print_config', False):
            import json
            from protagine.mcp.config import _mcp_config
            
            contact_id = args.contact_id or os.environ.get("USER", "user")
            harness = args.harness or "crush"
            
            # Get harness definition
            hdef = HARNESS_DEFS.get(harness)
            if not hdef:
                print(f"Unknown harness: {harness}")
                print(f"Available: {', '.join(HARNESS_DEFS.keys())}")
                return
            
            # Build MCP config with optional overrides
            sidecar_url = getattr(args, 'sidecar_url', None)
            needs_type = hdef.get("mcp_type") == "stdio"
            mcp_config = _mcp_config(contact_id, hdef["source_tag"], include_type=needs_type, sidecar_url=sidecar_url)
            
            # Print the full config snippet
            full_config = {hdef.get("mcp_key", "mcp_servers"): {"protagine": mcp_config}}
            print(json.dumps(full_config, indent=2))
            print()
            print(f"# Add this to {hdef['config_path']}")
            print(f"# Contact ID: {contact_id}")
            print(f"# Source: {hdef['source_tag']}")
            return
        
        detected = detect_harnesses()
        installed = {k: v for k, v in detected.items() if v}

        if not installed:
            print("  No coding harnesses detected.")
            print("  Install one of: Claude Code, Codex, Crush, OpenCode, or Hermes")
            print("  Then run: protagine mcp setup")
            print()
            print("  For distributed setups (Protagine on remote machine):")
            print("    protagine mcp setup --print-config --sidecar-url http://HOST:7777 --harness crush")
            return

        # Get contact ID
        contact_id = args.contact_id
        if not contact_id:
            try:
                contact_id = input("  What should Protagine call you? ").strip()
            except EOFError:
                contact_id = os.environ.get("USER", "user")
            if not contact_id:
                contact_id = os.environ.get("USER", "user")

        # Determine which harnesses to configure
        if args.harness == "all":
            selected = list(installed.keys())
        elif args.harness:
            if args.harness not in installed:
                print(f"  {HARNESS_DEFS[args.harness]['display']} is not installed")
                return
            selected = [args.harness]
        else:
            # Interactive selection
            print("  Detected coding harnesses:")
            options = list(installed.keys())
            for i, hid in enumerate(options, 1):
                print(f"    [{i}] {HARNESS_DEFS[hid]['display']}")
            print()
            try:
                choice = input("  Which should Protagine connect? (comma-separated, or 'all') [all]: ").strip()
            except EOFError:
                choice = "all"

            if not choice or choice.lower() == "all":
                selected = options
            else:
                indices = [int(x.strip()) for x in choice.split(",") if x.strip().isdigit()]
                selected = [options[i - 1] for i in indices if 1 <= i <= len(options)]

        if not selected:
            print("  No harnesses selected. Run 'protagine mcp setup' again when ready.")
            return

        # Configure each selected harness
        for hid in selected:
            hdef = HARNESS_DEFS[hid]
            print(f"  Configuring {hdef['display']}...")
            diff = add_to_harness(hid, contact_id, dry_run=args.dry_run,
                                  sidecar_url=getattr(args, "sidecar_url", None))
            if diff is None:
                print(f"  Already configured — skipping")
            elif args.dry_run:
                print(f"  Would add (dry run):")
                print(diff)
            else:
                print(f"  Added Protagine MCP (source: {hdef['source_tag']})")
                
                # Write skill
                from protagine.harness_integration import write_protagine_skill
                if write_protagine_skill(hid):
                    print(f"  ✅ Diagnostic skill installed")

        if args.dry_run:
            print("  Run without --dry-run to apply changes")
        else:
            print(f"  Contact ID: {contact_id}")
            print(f"  Start the sidecar with: protagine start")

    elif args.mcp_command == "remove":
        if args.harness == "all":
            targets = list(HARNESS_DEFS.keys())
        elif args.harness:
            targets = [args.harness]
        else:
            detected = detect_harnesses()
            targets = [k for k, v in detected.items() if v]

        if not targets:
            print("  No harnesses to remove from.")
            return

        for hid in targets:
            hdef = HARNESS_DEFS.get(hid)
            if not hdef:
                continue
            diff = remove_from_harness(hid, dry_run=args.dry_run)
            if diff:
                prefix = "  Would remove" if args.dry_run else "  Removed"
                print(f"{prefix}: {hdef['display']}")
                
                # Remove skill (not dry_run)
                if not args.dry_run:
                    from protagine.harness_integration import remove_protagine_skill
                    if remove_protagine_skill(hid):
                        print(f"  ✅ Diagnostic skill removed")
            else:
                print(f"  {hdef['display']} — Protagine not configured, skipping")

        if args.dry_run:
            print("  Run without --dry-run to apply changes")
        else:
            print("  Protagine MCP removed from harness configs")


def _cmd_validate(args) -> None:
    """Run end-to-end pipeline validation."""
    import httpx

    host = os.environ.get("PROTAGINE_SIDECAR_HOST", "127.0.0.1")
    port = os.environ.get("PROTAGINE_SIDECAR_PORT", "7777")
    url = f"http://{host}:{port}"
    api_key = os.environ.get("PROTAGINE_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    state_dir = os.environ.get("PROTAGINE_STATE_DIR", ".")

    print("🧪 Protagine E2E Pipeline Validation")
    print("=" * 40)
    print()

    # Step 0: Confirm LLM usage
    if not args.yes:
        print("This test will send one prompt through the LLM to verify the full pipeline.")
        print("It uses a small amount of LLM API credits.")
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except EOFError:
            print("  \u26a0\ufe0f No stdin — use --yes to skip confirmation")
            return
        if answer not in ("y", "yes"):
            print("Cancelled.")
            return

    # Step 1: Check sidecar is running
    print("\n[1/5] Checking sidecar...")
    try:
        resp = httpx.get(f"{url}/v1/host/health", headers=headers, timeout=5)
        data = resp.json()
        if data.get("status") != "ok":
            print(f"  ❌ Sidecar not healthy: {data.get('status')}")
            return
        print(f"  ✅ Sidecar running ({len(data.get('capabilities', []))} capabilities)")
    except Exception as e:
        print(f"  ❌ Sidecar not reachable: {e}")
        print("  Start with: protagine start")
        return

    # Step 2: Seed test data
    print("\n[2/5] Seeding test data...")
    test_contact = f"validate-{uuid.uuid4().hex[:6]}"

    r = httpx.post(f"{url}/v1/host/commitments", headers=headers,
        json={"person_id": test_contact, "description": "Validate E2E pipeline", "priority": 2}, timeout=5)
    if r.status_code not in (200, 201):
        print(f"  ❌ Could not create commitment: {r.status_code}")
        return
    cid = r.json().get("id")
    print(f"  ✅ Test commitment created")

    r = httpx.post(f"{url}/v1/host/affect/events", headers=headers,
        json={"contact_id": test_contact, "valence": 0.6, "arousal": 0.4, "trigger": "validation test"}, timeout=5)
    print(f"  ✅ Test affect recorded" if r.status_code in (200, 201) else f"  ⚠️ Affect failed: {r.status_code}")

    r = httpx.post(f"{url}/v1/host/mind/facts", headers=headers,
        json={"contact_id": test_contact, "fact": "Running pipeline validation", "category": "test", "confidence": 0.5}, timeout=5)
    print(f"  ✅ Test fact recorded" if r.status_code in (200, 201) else f"  ⚠️ Fact failed: {r.status_code}")

    # Step 3: Context assembly
    print("\n[3/5] Testing context assembly...")
    r = httpx.post(f"{url}/v1/host/context/assemble", headers=headers,
        json={"identity": {"host_id": "validate"}, "context": {"session_id": "validate", "contact_id": test_contact},
              "incoming_message": {"role": "user", "content": "What am I working on?"}}, timeout=10)
    if r.status_code != 200:
        print(f"  ❌ Context assembly failed: {r.status_code}")
        return

    sections = r.json().get("sections", [])
    section_ids = [s["id"] for s in sections]
    expected = ["protagine-commitments", "protagine-affect", "protagine-shared-facts"]
    found = [e for e in expected if e in section_ids]
    print(f"  ✅ Context assembly: {len(sections)} sections, {len(found)}/{len(expected)} cognitive sections present")

    # Step 4: Check LLM is configured (live-fire the sidecar's own router —
    # this is the pipeline Protagine actually reasons with, harness-independent)
    print("\n[4/5] Checking LLM configuration...")
    llm_ok = False
    llm_latency = None
    try:
        r = httpx.get(f"{url}/v1/host/health/llm", headers=headers, timeout=30)
        if r.status_code == 200 and (r.json() or {}).get("ok"):
            body = r.json()
            llm_ok = True
            llm_latency = body.get("latency_ms")
            print(f"  ✅ LLM router answered (tier={body.get('tier', '?')}, "
                  f"latency={llm_latency}ms)")
        else:
            detail = ""
            try:
                detail = (r.json() or {}).get("error") or ""
            except Exception:
                pass
            print(f"  ⚠️ LLM router check failed (HTTP {r.status_code}) {detail}")
            print("  Configure models via POST /v1/host/configure or re-run "
                  "'protagine init' (llm-config step)")
    except Exception as exc:
        print(f"  ⚠️ Could not reach the LLM health endpoint: {exc}")

    # Step 5: Full pipeline — context assembly reached the cognitive sections
    # (step 3) and the router answers (step 4); that IS the full sidecar
    # pipeline. A connected harness exercises it end-to-end on its next turn.
    print("\n[5/5] Pipeline verdict...")
    if llm_ok and len(found) >= 2:
        print("  ✅ Full sidecar pipeline working — context assembly + LLM router live")
    elif llm_ok:
        print("  ⚠️ LLM live but cognitive context sections are thin — run a few "
              "ordinary turns and check their retained evidence")
    else:
        print("  ⚪ LLM not verified — context pipeline validated, reasoning not")

    # Cleanup: delete test commitment
    if cid:
        httpx.delete(f"{url}/v1/host/commitments/{cid}", headers=headers, timeout=5)

    # Write validation stamp
    stamp_path = Path(state_dir) / ".protagine-e2e-validated"
    stamp_data = {
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "context_sections": len(sections),
        "cognitive_sections": len(found),
        "llm_tested": llm_ok,
    }
    stamp_path.write_text(json.dumps(stamp_data, indent=2))

    # Summary
    print()
    all_ok = len(found) >= 2  # At least commitments + one other
    if all_ok:
        print("🟢 Pipeline validation passed")
        print(f"  Context assembly: {len(sections)} sections")
        print(f"  Cognitive sections: {', '.join(found)}")
        if not llm_ok:
            print(f"  ⚠️ LLM pipeline not tested — configure LLM and re-run 'protagine validate'")
    else:
        print("🔴 Pipeline validation incomplete")
        print(f"  Missing sections: {set(expected) - set(found)}")
        print(f"  Check sidecar logs and configuration")


def _configured_instance() -> bool:
    """True once ``protagine init`` has written this instance's ``protagine.yaml``."""
    from protagine.config import CONFIG_FILE, instance_home
    return (instance_home() / CONFIG_FILE).is_file()


def _cmd_doctor(args) -> None:
    """Check the install: Hermes version, keys, adapter, pip check, sidecar, plugin."""
    _load_dotenv()
    from protagine.doctor import (
        default_api_key,
        default_protagine_url,
        exit_code,
        format_report,
        results_to_json,
        run_doctor,
    )

    url = args.url or default_protagine_url()
    api_key = args.api_key if args.api_key is not None else default_api_key()

    if getattr(args, "fix", False):
        # Safe, idempotent config repairs only — everything else gets a
        # printed remedy instead of an automatic change.
        try:
            from protagine.setup import repair_persisted_llm_config

            fixed = repair_persisted_llm_config()
            if fixed:
                print("🔧 Applied LLM config fixes: " + ", ".join(fixed) + "\n")
            else:
                print("🔧 No automatic fixes applicable\n")
        except Exception as exc:
            print(f"🔧 Automatic fixes unavailable: {exc}\n")

    results = run_doctor(protagine_url=url, api_key=api_key, timeout=args.timeout)

    if args.json:
        print(json.dumps(results_to_json(results), indent=2))
    else:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        print(format_report(results, protagine_url=url, color=color))

    sys.exit(exit_code(results))


def _load_dotenv() -> None:
    from protagine.util.instance import load_environment
    load_environment()


if __name__ == "__main__":
    main()
