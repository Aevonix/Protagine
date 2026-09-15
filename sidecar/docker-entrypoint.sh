#!/bin/sh
set -eu
# Preserve an explicitly selected state directory.
if [ -z "${PROTAGINE_STATE_DIR+x}" ]; then
    export PROTAGINE_STATE_DIR=/var/lib/protagine
fi
exec "$@"
