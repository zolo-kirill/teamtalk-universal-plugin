#!/bin/bash
# Wrapper: sets SDK env and secrets, runs the TeamTalk music bot.
set -e
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="$DIR/sdk/tt5sdk_v5.22a_ubuntu22_x86_64"

export LD_LIBRARY_PATH="$SDK/Library/TeamTalk_DLL${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$SDK/Library/TeamTalkPy${PYTHONPATH:+:$PYTHONPATH}"

# Prefer the venv created by install.sh, fall back to system python.
if [ -x "$DIR/.venv/bin/python" ]; then
    export PATH="$DIR/.venv/bin:$PATH"
    PY="$DIR/.venv/bin/python"
else
    PY="python3"
fi

# Load stored secrets (TEAMTALK_PASSWORD, TG_TOKEN, ...) if present
SECRETS="$DIR/../.secrets/.env"
if [ -f "$SECRETS" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$SECRETS"
    set +a
fi
export TEAMTALK_PASSWORD="${TEAMTALK_PASSWORD:-$TEAMTALK_BOT_PASSWORD}"

cd "$DIR"
exec "$PY" bot.py
