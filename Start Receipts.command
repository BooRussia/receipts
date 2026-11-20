#!/bin/bash
# Double-click to start Receipts on a Mac. The first run sets everything up (a few minutes).
cd "$(dirname "$0")" || exit 1
LOG="$HOME/Library/Application Support/Receipts/launcher.log"
mkdir -p "$(dirname "$LOG")"

fail() {
    echo
    echo "$1"
    read -n 1 -r -s -p "Press any key to close."
    exit 1
}

find_python() {
    for py in python3.13 python3.12 python3.11 python3.10 \
              /Library/Frameworks/Python.framework/Versions/3.1{3,2,1,0}/bin/python3 \
              /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
        if command -v "$py" >/dev/null 2>&1 &&
           "$py" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
            echo "$py"
            return 0
        fi
    done
    return 1
}

if [ ! -x .venv/bin/python ]; then
    PY=$(find_python) || fail "Receipts needs Python 3.10 or newer. Install Python 3.12 from python.org, then double-click this again."
    echo "Setting up Receipts. First run only, this takes a few minutes..."
    "$PY" -m venv .venv || fail "Setup failed while creating the Python environment."
fi

# Install packages on first run, and again whenever an update changes requirements.txt.
if ! cmp -s requirements.txt .venv/requirements.installed; then
    echo "Installing packages..."
    .venv/bin/python -m pip install --upgrade pip >>"$LOG" 2>&1
    .venv/bin/python -m pip install -r requirements.txt || fail "Setup failed while installing packages."
    cp requirements.txt .venv/requirements.installed
fi

nohup .venv/bin/python -m receipts "$@" >>"$LOG" 2>&1 &
disown
echo "Receipts is running: look for the gold receipt in the menu bar. You can close this window."
