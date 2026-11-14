#!/bin/bash
# Start the travelbee web app and the live Advocates in the background (they keep running after you close
# the terminal; the Mac must stay on). Logs in data/logs/. Stop everything with ./stop.sh
cd "$(dirname "$0")"
mkdir -p data/logs
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
./stop.sh >/dev/null 2>&1
nohup "$PY" -u web/server.py > data/logs/web.log 2>&1 & echo $! > data/logs/web.pid
nohup "$PY" -u agents/advocates.py > data/logs/advocates.log 2>&1 & echo $! > data/logs/advocates.pid
sleep 2
echo "travelbee app:  http://127.0.0.1:${PORT:-8123}   (logs: data/logs/web.log)"
echo "Advocates:      one Band agent per registered traveler   (logs: data/logs/advocates.log)"
echo "Stop:           ./stop.sh"
