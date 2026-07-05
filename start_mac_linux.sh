#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  exec python3 server.py
fi

if command -v python >/dev/null 2>&1; then
  exec python server.py
fi

echo "Python 3.11/3.12 not found." >&2
exit 1
