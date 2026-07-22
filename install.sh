#!/usr/bin/env bash
# install.sh — Proba installer
# Supports: Termux (Android), Linux, Windows (Git Bash)
set -e

# ── Constants ─────────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10

# ── Detect Python ─────────────────────────────────────────────────────────────
if command -v python &>/dev/null; then
  PYTHON=python
elif command -v python3 &>/dev/null; then
  PYTHON=python3
else
  echo "ERROR: Python not found. Install Python 3.10+ and try again."
  exit 1
fi

echo ""
echo "  PROBA — Installer"
echo "  NoLaptopTrades"
echo ""

# ── Termux (Android) — install only, no update/upgrade ───────────────────────
if command -v pkg &>/dev/null; then
  echo "  [install] Detected Termux — installing dependencies..."
  pkg install -y python git
fi

# ── Check Python version ──────────────────────────────────────────────────────
PYVER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  [install] Python $PYVER detected"

$PYTHON -c "
import sys
if sys.version_info < ($MIN_PYTHON_MAJOR, $MIN_PYTHON_MINOR):
    print('ERROR: Python $MIN_PYTHON_MAJOR.$MIN_PYTHON_MINOR+ required.')
    sys.exit(1)
"

# ── Detect Termux for pip flags ───────────────────────────────────────────────
IS_TERMUX=false
if command -v pkg &>/dev/null; then
  IS_TERMUX=true
fi

# Helper: run pip with the right flags for this environment
pip_install() {
  if [ "$IS_TERMUX" = true ]; then
    $PYTHON -m pip install "$@" --break-system-packages -q
  else
    # Try without flag first; fall back for system-managed Linux envs
    $PYTHON -m pip install "$@" -q 2>/dev/null || \
    $PYTHON -m pip install "$@" -q --break-system-packages
  fi
}

# ── Install Proba ─────────────────────────────────────────────────────────────
echo "  [install] Installing Proba and dependencies..."

if pip_install -e "$REPO_DIR" 2>/dev/null; then
  INSTALLED_AS_PACKAGE=true
  echo "  [install] Installed as package — 'proba' command is now available."
else
  INSTALLED_AS_PACKAGE=false
  echo "  [install] Editable install failed — falling back to requirements + alias."
  pip_install -r "$REPO_DIR/requirement.txt"
fi

# ── Windows curses (not in requirement.txt for non-Windows, handle here) ─────
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OS" == "Windows_NT" ]]; then
  echo "  [install] Windows detected — installing windows-curses..."
  pip_install windows-curses || true
fi

# ── Config setup ──────────────────────────────────────────────────────────────
CONFIG="$REPO_DIR/config.json"
if [ ! -f "$CONFIG" ]; then
  cp "$REPO_DIR/config.example.json" "$CONFIG"
  echo "  [install] config.json created from template."
  echo "            Edit it to customise your settings."
else
  echo "  [install] config.json already exists — skipping."
fi

# ── .env setup ────────────────────────────────────────────────────────────────
ENV_FILE="$REPO_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO_DIR/.env.example" "$ENV_FILE"
  echo "  [install] .env created from template."
  echo "            Add your Futuur API keys to enable authenticated features."
else
  echo "  [install] .env already exists — skipping."
fi

# ── Shell alias (only if editable install didn't create console script) ───────
if [ "$INSTALLED_AS_PACKAGE" = false ]; then
  ALIAS_CMD="alias proba='$PYTHON $REPO_DIR/proba/cli.py'"

  if [ -n "$BASH_VERSION" ]; then
    PROFILE="$HOME/.bashrc"
  elif [ -n "$ZSH_VERSION" ]; then
    PROFILE="$HOME/.zshrc"
  else
    PROFILE="$HOME/.profile"
  fi

  if ! grep -q "alias proba=" "$PROFILE" 2>/dev/null; then
    echo "" >> "$PROFILE"
    echo "# Proba — NoLaptopTrades" >> "$PROFILE"
    echo "$ALIAS_CMD" >> "$PROFILE"
    echo "  [install] 'proba' alias added to $PROFILE"
    echo "            Run: source $PROFILE  (or restart terminal)"
  else
    echo "  [install] 'proba' alias already in $PROFILE"
  fi
fi

echo ""
echo "  ✓ Installation complete."
echo ""
if [ "$INSTALLED_AS_PACKAGE" = true ]; then
  echo "  Usage:"
  echo "    proba                  # launch TUI"
  echo "    proba --opportunities  # review scored markets"
  echo "    proba --stats          # calibration progress"
else
  echo "  Next steps:"
  echo "    1. source $PROFILE"
  echo "    2. proba"
fi
echo "  (optional) Add Futuur API keys to .env"
echo ""
