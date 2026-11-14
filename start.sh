#!/bin/bash
# Start the travelbee web app and the live Advocates in the background (they keep running after you close
# the terminal; the Mac must stay on). Logs in data/logs/. Stop everything with ./stop.sh
cd "$(dirname "$0")"
mkdir -p data/logs
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
./stop.sh >/dev/null 2>&1
export HOST="${HOST:-0.0.0.0}"   # reachable by teammates on the same Wi-Fi
nohup "$PY" -u web/server.py > data/logs/web.log 2>&1 & echo $! > data/logs/web.pid
nohup "$PY" -u agents/advocates.py > data/logs/advocates.log 2>&1 & echo $! > data/logs/advocates.pid
sleep 2
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
echo "travelbee app:  http://127.0.0.1:${PORT:-8123}  (you)"
[ -n "$IP" ] && echo "                http://$IP:${PORT:-8123}  (teammates on the same Wi-Fi)"
echo "                logs: data/logs/web.log"
echo "Advocates:      one Band agent per registered traveler   (logs: data/logs/advocates.log)"
echo "Stop:           ./stop.sh"
