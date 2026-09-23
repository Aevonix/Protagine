#!/usr/bin/env bash
# Fresh-install check against stock Hermes (build plan M1, acceptance tests 2, 3, 5, 6 and 9).
#
# Expects:
#   HERMES_SRC   a stock hermes-agent checkout (installed here, editable, into its own venv)
#   DIST_DIR     the built protagine and protagine_hermes wheels
# Everything else is created under a temporary directory.
set -euo pipefail

: "${HERMES_SRC:?set HERMES_SRC to a stock hermes-agent checkout}"
: "${DIST_DIR:?set DIST_DIR to the directory holding the built wheels}"

work="$(mktemp -d)"
export HOME="$work/home"
export HERMES_HOME="$work/home/.hermes"
export PROTAGINE_HOME="$work/home/.protagine"
export PROTAGINE_INIT_NO_SERVICE=1
mkdir -p "$HERMES_HOME"

# A stock Hermes in its own environment. Hermes installs as an editable checkout
# (its setup.py refuses to build wheels), exactly as its own installer does.
python -m venv "$work/hermes-venv"
"$work/hermes-venv/bin/python" -m pip install --quiet --upgrade pip
"$work/hermes-venv/bin/python" -m pip install --quiet -e "$HERMES_SRC"
hermes_python="$work/hermes-venv/bin/python"
export PATH="$work/hermes-venv/bin:$PATH"

# The sidecar in its own environment, from the built wheels (pipx in spirit).
python -m venv "$work/protagine-venv"
"$work/protagine-venv/bin/python" -m pip install --quiet --upgrade pip
sidecar_wheel="$(ls "$DIST_DIR"/protagine-*.whl)"
adapter_wheel="$(ls "$DIST_DIR"/protagine_hermes-*.whl)"
"$work/protagine-venv/bin/python" -m pip install --quiet "$sidecar_wheel"
protagine="$work/protagine-venv/bin/protagine"

cat > "$HERMES_HOME/config.yaml" <<'YAML'
model:
  provider: custom
  default: ci-model
  base_url: http://127.0.0.1:9/v1
YAML

echo "== init"
"$protagine" init --non-interactive --owner-name Owner --agent-name Agent \
  --hermes-home "$HERMES_HOME" --hermes-python "$hermes_python" \
  --adapter-source "$adapter_wheel" --no-service

echo "== pip check in Hermes' environment (acceptance 2)"
"$hermes_python" -m pip check

echo "== Hermes is unmodified: RECORD hashes or a clean checkout (acceptance 5)"
"$hermes_python" -I "$(dirname "$0")/check_hermes_record.py"

echo "== stock Hermes loads the config and the profile (acceptance 7, 9)"
"$hermes_python" -I - <<'PY'
from hermes_cli.config import load_config
from hermes_cli.profiles import profile_exists
config = load_config()
assert "protagine" in config["plugins"]["enabled"], config["plugins"]
assert config["memory"]["provider"] == "protagine-memory"
assert config["plugins"]["hook_callback_timeout"] == 0
assert profile_exists("protagine-act")
import importlib.metadata as m
names = {e.name for e in m.entry_points(group="hermes_agent.plugins")}
assert "protagine" in names, names
print("stock Hermes sees the adapter and the worker profile")
PY

echo "== upgrade twice (acceptance 3)"
"$protagine" upgrade --hermes-python "$hermes_python" --adapter-source "$adapter_wheel"
before="$(find "$HERMES_HOME" "$PROTAGINE_HOME" -type f ! -path '*/backups/*' ! -name '*.db-shm' ! -name '*.db-wal' -exec sha256sum {} + | sort)"
second="$("$protagine" upgrade --hermes-python "$hermes_python")"
echo "$second"
echo "$second" | grep -q "nothing to do"
after="$(find "$HERMES_HOME" "$PROTAGINE_HOME" -type f ! -path '*/backups/*' ! -name '*.db-shm' ! -name '*.db-wal' -exec sha256sum {} + | sort)"
test "$before" = "$after"
"$hermes_python" -m pip check

echo "== doctor (the sidecar is not running here, so only that check may fail)"
"$protagine" doctor --json > "$work/doctor.json" || true
"$work/protagine-venv/bin/python" - "$work/doctor.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
failing = [r for r in report["results"] if r["status"] == "fail" and not r["name"].startswith("sidecar")]
assert not failing, failing
print("doctor:", report["summary"])
PY

echo "== non-loopback bind without a key is refused (acceptance 6)"
mv "$PROTAGINE_HOME/api.key" "$PROTAGINE_HOME/api.key.off"
if "$protagine" start --host 0.0.0.0 --port 7999 >/dev/null 2>&1; then
  echo "expected the bind to be refused"; exit 1
fi
mv "$PROTAGINE_HOME/api.key.off" "$PROTAGINE_HOME/api.key"

echo "== uninstall leaves stock Hermes (acceptance 9)"
"$protagine" init --uninstall --hermes-python "$hermes_python"
"$hermes_python" -I - <<'PY'
from hermes_cli.config import load_config
from hermes_cli.profiles import profile_exists
config = load_config()
assert "protagine" not in (config.get("plugins") or {}).get("enabled", []), config.get("plugins")
assert not profile_exists("protagine-act")
import importlib.metadata as m
try:
    m.version("protagine-hermes")
except m.PackageNotFoundError:
    print("adapter removed; stock Hermes starts with this config")
else:
    raise SystemExit("adapter still installed")
PY
"$hermes_python" -I "$(dirname "$0")/check_hermes_record.py"
echo "fresh install check passed"
