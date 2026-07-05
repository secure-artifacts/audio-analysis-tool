#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1

ensure_requirements() {
  python_cmd="$1"
  missing=0
  while IFS= read -r line; do
    requirement=$(printf "%s" "$line" | sed 's/[[:space:]]#.*//' | xargs)
    [ -z "$requirement" ] && continue
    package=$(printf "%s" "$requirement" | sed 's/[<>=!~; ].*//')
    "$python_cmd" -m pip show "$package" >/dev/null 2>&1 || missing=1
  done < requirements.txt

  if [ "$missing" -eq 1 ]; then
    echo "Dependencies missing, installing..."
    "$python_cmd" -m pip install -r requirements.txt || exit 1
  else
    echo "Dependencies already installed, skipping install."
  fi
}

if command -v python3 >/dev/null 2>&1; then
  ensure_requirements python3
  exec python3 server.py
fi

if command -v python >/dev/null 2>&1; then
  ensure_requirements python
  exec python server.py
fi

echo "Python 3.11/3.12 not found." >&2
exit 1
