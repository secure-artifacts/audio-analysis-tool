#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1

ensure_homebrew_path() {
  if command -v brew >/dev/null 2>&1; then
    return
  fi
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

persist_homebrew_path() {
  if [ -x /opt/homebrew/bin/brew ]; then
    line='eval "$(/opt/homebrew/bin/brew shellenv)"'
  elif [ -x /usr/local/bin/brew ]; then
    line='eval "$(/usr/local/bin/brew shellenv)"'
  else
    return
  fi

  profile="${ZDOTDIR:-$HOME}/.zprofile"
  touch "$profile" || return
  grep -F "$line" "$profile" >/dev/null 2>&1 || printf "\n%s\n" "$line" >> "$profile"
}

install_homebrew() {
  if [ "$(uname -s)" != "Darwin" ]; then
    return 1
  fi
  command -v curl >/dev/null 2>&1 || return 1

  echo "Homebrew missing, installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || exit 1
  ensure_homebrew_path
  persist_homebrew_path
  command -v brew >/dev/null 2>&1
}

ensure_ffmpeg() {
  if command -v ffmpeg >/dev/null 2>&1; then
    echo "ffmpeg already installed."
    return
  fi

  ensure_homebrew_path
  if ! command -v brew >/dev/null 2>&1; then
    install_homebrew || {
      echo "ffmpeg not found, and Homebrew could not be installed automatically." >&2
      exit 1
    }
  fi

  if command -v brew >/dev/null 2>&1; then
    echo "ffmpeg missing, installing with Homebrew..."
    brew install ffmpeg || exit 1
    return
  fi
}

ensure_requirements() {
  python_cmd="$1"
  missing=0
  while IFS= read -r line; do
    requirement=$(printf "%s" "$line" | sed 's/[[:space:]]#.*//' | xargs)
    [ -z "$requirement" ] && continue
    package=$(printf "%s" "$requirement" | sed 's/[<>=!~; ].*//')
    "$python_cmd" -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$package') else 1)" || missing=1
  done < requirements.txt

  if [ "$missing" -eq 1 ]; then
    echo "Dependencies missing, installing..."
    "$python_cmd" -m pip install -r requirements.txt || exit 1
  else
    echo "Dependencies already installed, skipping install."
  fi
}

ensure_ffmpeg

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
