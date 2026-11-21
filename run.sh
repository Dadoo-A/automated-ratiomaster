#!/usr/bin/env bash
# ratiomaster launcher for Linux/macOS. Forwards every argument.
#   ./run.sh my.torrent --up 500 --down 2000
#   ./run.sh --watch
#   ./run.sh --mode qbit --qbit-url http://localhost:8080
if ! command -v python3 >/dev/null 2>&1; then
    echo "[!] python3 (3.10+) not found. Install it from your package manager or https://www.python.org/downloads/" >&2
    exit 1
fi
exec python3 "$(dirname "$0")/ratiomaster.py" "$@"
