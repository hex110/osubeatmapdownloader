#!/usr/bin/env bash
# Run-from-source launcher for Linux and macOS: makes a virtualenv on first run, then starts the app.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.10 or newer is required. Install it with your package manager, then run this again." >&2
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "First run: setting things up, this takes a minute..."
    "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

# The app needs a Chromium-based browser it can launch directly (Flatpak/Snap ones won't do).
if ! command -v google-chrome >/dev/null 2>&1 \
   && ! command -v google-chrome-stable >/dev/null 2>&1 \
   && ! command -v chromium >/dev/null 2>&1 \
   && ! command -v chromium-browser >/dev/null 2>&1 \
   && [ -z "${OBD_CHROME:-}" ]; then
    echo
    echo "Warning: no Google Chrome or Chromium found, so downloads won't start."
    echo "  Arch:   sudo pacman -S chromium"
    echo "  Debian: sudo apt install chromium"
    echo "  Fedora: sudo dnf install chromium"
    echo "Or set OBD_CHROME=/path/to/browser if yours lives somewhere unusual."
    echo
fi

exec .venv/bin/python app.py "$@"
