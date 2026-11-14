#!/bin/bash
# Stop what start.sh started.
cd "$(dirname "$0")"
for f in data/logs/web.pid data/logs/advocates.pid; do
  [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null && echo "stopped $(basename "$f" .pid)"
  rm -f "$f"
done
