#!/bin/bash
# Stop what start.sh started (and any stray copy of the agents or the app).
cd "$(dirname "$0")"
rm -f data/logs/web.pid data/logs/advocates.pid
for p in "web/server.py" "agents/advocates.py"; do
  if pgrep -f "$p" >/dev/null; then
    pkill -f "$p"; sleep 2
    pgrep -f "$p" >/dev/null && pkill -9 -f "$p"   # the agents can ignore a plain stop
    echo "stopped $p"
  fi
done
