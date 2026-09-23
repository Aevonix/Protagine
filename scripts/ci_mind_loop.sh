#!/usr/bin/env bash
# Build plan M2 acceptance on stock Hermes: the first closed loop end to end, the ask path,
# the 72 h expiry, the digest, the off switch without a model, `mind why` and the dismissal
# multiplier (scripts/mind_loop_probe.py), against a scripted model (scripts/fake_model.py),
# a real gateway whose platforms are the benchmark `capture` platform and a loopback webhook
# route, and `protagine init` from the built wheels.
#
# Expects:
#   HERMES_SRC   a stock hermes-agent checkout
#   DIST_DIR     the built protagine and protagine_hermes wheels
# Optional: FAKE_MODEL_DUE_SECONDS (how soon a captured promise is due; default 45).
set -euo pipefail
: "${HERMES_SRC:?set HERMES_SRC to a stock hermes-agent checkout}"
: "${DIST_DIR:?set DIST_DIR to the directory holding the built wheels}"
here="$(cd "$(dirname "$0")" && pwd)"
work="${WORK_DIR:-$(mktemp -d)}"
mkdir -p "$work"
export WORK="$work"
export HOME="$work/home"
export HERMES_HOME="$work/home/.hermes"
export PROTAGINE_HOME="$work/home/.protagine"
export PROTAGINE_INIT_NO_SERVICE=1
export HERMES_DISABLE_TELEMETRY=1
export FAKE_MODEL_DUE_SECONDS="${FAKE_MODEL_DUE_SECONDS:-45}"
export FAKE_MODEL_LOG="$work/model.log"
export CAPTURE_OUTBOX="$work/outbox.json"
export CAPTURE_HOME_CHANNEL=owner
mkdir -p "$HERMES_HOME"
start=$(date +%s)
log() { echo "[$(( $(date +%s) - start ))s] $*"; }
cleanup() {
  for name in gateway sidecar model; do
    if [ -f "$work/$name.pid" ]; then kill "$(cat "$work/$name.pid")" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT

port_free() { python -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1]))) else 1)" "$1"; }
MODEL_PORT=18865; while ! port_free $MODEL_PORT; do MODEL_PORT=$((MODEL_PORT + 1)); done
SIDECAR_PORT=7851; while ! port_free $SIDECAR_PORT; do SIDECAR_PORT=$((SIDECAR_PORT + 1)); done
WEBHOOK_PORT=18944; while ! port_free $WEBHOOK_PORT; do WEBHOOK_PORT=$((WEBHOOK_PORT + 1)); done
export MODEL_URL="http://127.0.0.1:$MODEL_PORT/v1"
export SIDECAR_URL="http://127.0.0.1:$SIDECAR_PORT"
export WEBHOOK_URL="http://127.0.0.1:$WEBHOOK_PORT"

cat > "$work/model.sh" <<SH
#!/usr/bin/env bash
case "\$1" in
  start) FAKE_MODEL_LOG="$FAKE_MODEL_LOG" FAKE_MODEL_DUE_SECONDS="$FAKE_MODEL_DUE_SECONDS" \\
           python "$here/fake_model.py" "$MODEL_PORT" >> "$work/model.out" 2>&1 &
         echo \$! > "$work/model.pid"
         for _ in \$(seq 1 30); do curl -sf "$MODEL_URL/models" > /dev/null && exit 0; sleep 0.5; done; exit 1;;
  stop) kill "\$(cat "$work/model.pid")" 2>/dev/null || true; sleep 1;;
esac
SH
log "== scripted model on 127.0.0.1:$MODEL_PORT (promises due after ${FAKE_MODEL_DUE_SECONDS}s)"
bash "$work/model.sh" start

log "== stock Hermes in its own environment ($HERMES_SRC)"
python -m venv "$work/hermes-venv"
"$work/hermes-venv/bin/python" -m pip install --quiet --upgrade pip
"$work/hermes-venv/bin/python" -m pip install --quiet -e "$HERMES_SRC"
# Hermes' own pin for its webhook platform (the [homeassistant] extra is exactly this); the guest turn arrives on it.
"$work/hermes-venv/bin/python" -m pip install --quiet "aiohttp==3.14.3"
hermes_python="$work/hermes-venv/bin/python"
export HERMES_PYTHON="$hermes_python"
export PATH="$work/hermes-venv/bin:$PATH"

log "== the sidecar from the built wheels, in its own environment"
python -m venv "$work/protagine-venv"
"$work/protagine-venv/bin/python" -m pip install --quiet --upgrade pip
"$work/protagine-venv/bin/python" -m pip install --quiet "$(ls "$DIST_DIR"/protagine-*.whl)"
protagine="$work/protagine-venv/bin/protagine"
export PROTAGINE_BIN="$protagine"

cat > "$HERMES_HOME/config.yaml" <<YAML
model:
  provider: custom
  default: scripted-model
  base_url: $MODEL_URL
plugins:
  enabled: [capture]
platforms:
  capture:
    enabled: true
  webhook:
    enabled: true
    extra:
      host: 127.0.0.1
      port: $WEBHOOK_PORT
      routes:
        guest:
          secret: INSECURE_NO_AUTH
          prompt: "{text}"
          deliver: log
kanban:
  dispatch_in_gateway: true
  dispatch_interval_seconds: 5
  default_assignee: default
tools:
  tool_search:
    enabled: off      # plugin tools offered directly, not behind tool_search (the model is scripted)
YAML
printf 'OPENAI_API_KEY=local-no-key\n' > "$HERMES_HOME/.env"
# The benchmark capture platform: every delivery lands in one JSON array (nothing is transmitted).
mkdir -p "$HERMES_HOME/plugins"
cp -r "$here/../benchmarks/paired/capture_platform" "$HERMES_HOME/plugins/capture"
echo '[]' > "$CAPTURE_OUTBOX"

log "== protagine init --non-interactive (the three-command path, with the owner's capture handle)"
"$protagine" init --non-interactive --autonomy standard --owner-name Owner --agent-name Agent --owner-handle capture=owner \
  --hermes-home "$HERMES_HOME" --hermes-python "$hermes_python" \
  --adapter-source "$(ls "$DIST_DIR"/protagine_hermes-*.whl)" --no-service --port "$SIDECAR_PORT"
# No quiet hours and a digest that is due at once: the acceptance list must not depend on the wall clock.
"$work/protagine-venv/bin/python" - <<'PY'
import os, yaml
path = os.path.join(os.environ["PROTAGINE_HOME"], "protagine.yaml")
config = yaml.safe_load(open(path))
config.setdefault("mind", {}).update({"quiet_hours": "", "digest_hour": 0})
open(path, "w").write(yaml.safe_dump(config, sort_keys=False))
PY

cat > "$work/sidecar.sh" <<SH
#!/usr/bin/env bash
# restart: stop the sidecar and start it again with the caller's environment
# (PROTAGINE_MIND_CLOCK_OFFSET_SECONDS shifts the mind's clock for the 72 h expiry check).
if [ -f "$work/sidecar.pid" ]; then kill "\$(cat "$work/sidecar.pid")" 2>/dev/null || true; fi
for _ in \$(seq 1 40); do curl -sf "$SIDECAR_URL/v1/host/health" > /dev/null || break; sleep 0.5; done
cd "$work"
"$protagine" start >> "$work/sidecar.log" 2>&1 &
echo \$! > "$work/sidecar.pid"
for _ in \$(seq 1 120); do curl -sf "$SIDECAR_URL/v1/host/health" > /dev/null && exit 0; sleep 1; done
echo "the sidecar did not come up"; tail -40 "$work/sidecar.log"; exit 1
SH
log "== sidecar"
bash "$work/sidecar.sh" restart

cat > "$work/gateway.sh" <<SH
#!/usr/bin/env bash
if [ -f "$work/gateway.pid" ]; then
  kill -INT "\$(cat "$work/gateway.pid")" 2>/dev/null || true
  for _ in \$(seq 1 40); do kill -0 "\$(cat "$work/gateway.pid")" 2>/dev/null || break; sleep 0.5; done
  kill -9 "\$(cat "$work/gateway.pid")" 2>/dev/null || true
fi
[ "\$1" = stop ] && exit 0
cd "$work"
hermes gateway run >> "$work/gateway.log" 2>&1 &
echo \$! > "$work/gateway.pid"
for _ in \$(seq 1 120); do curl -sf "$WEBHOOK_URL/health" > /dev/null && exit 0; sleep 1; done
echo "the gateway did not come up"; tail -60 "$work/gateway.log"; exit 1
SH
log "== gateway (capture platform, loopback webhook route, kanban dispatcher every 5 s)"
bash "$work/gateway.sh" start
sleep 5
kill -0 "$(cat "$work/gateway.pid")" || { echo "the gateway died:"; tail -60 "$work/gateway.log"; exit 1; }

log "== the M2 acceptance list"
rc=0
python "$here/mind_loop_probe.py" || rc=$?
if [ "$rc" != 0 ]; then
  echo "--- gateway.log (tail)"; tail -80 "$work/gateway.log"
  echo "--- sidecar.log (tail)"; tail -80 "$work/sidecar.log"
  echo "--- outbox.json"; cat "$CAPTURE_OUTBOX"
fi
log "acceptance finished with rc=$rc in $(( $(date +%s) - start )) s (work dir $work)"
exit $rc
