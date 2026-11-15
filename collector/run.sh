#!/bin/sh
# Entrypoint for the collector image (Docker Compose and the Home Assistant add-on).
#
# As an add-on, Supervisor writes the user's options to /data/options.json; they are
# mapped to the env vars collector.py reads. Values already set in the environment win
# for anything the add-on leaves empty. Then /data is handed to the unprivileged `app`
# user and the collector runs as that user.
set -eu

OPTIONS=/data/options.json
if [ -f "$OPTIONS" ]; then
    # Python, not jq (the image has no jq). Secrets go straight into the environment;
    # nothing is printed.
    eval "$(python3 - "$OPTIONS" <<'PY'
import json, shlex, sys

opts = json.load(open(sys.argv[1]))
mapping = {
    "username": "DUQUESNE_USERNAME",
    "password": "DUQUESNE_PASSWORD",
    "run_at": "COLLECTOR_RUN_AT",
    "api_token": "COLLECTOR_API_TOKEN",
    "download_bills": "COLLECTOR_BILLS",
    "hourly_lookback_days": "HOURLY_LOOKBACK_DAYS",
    "log_level": "LOG_LEVEL",
}
for key, env in mapping.items():
    value = opts.get(key)
    if value is None or value == "":
        continue
    if isinstance(value, bool):
        value = "1" if value else "0"
    print(f"export {env}={shlex.quote(str(value))}")
PY
)"
fi

export DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R app "$DATA_DIR"
    exec setpriv --reuid=app --regid=app --init-groups env HOME=/home/app \
        python /app/collector/collector.py "$@"
fi
exec python /app/collector/collector.py "$@"
