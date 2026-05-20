#!/usr/bin/env bash
# SIFTR — first-time setup
# Safe to re-run: already-complete steps are detected and skipped.

set -euo pipefail

# ── Output helpers ────────────────────────────────────────────────────────────

if [[ -t 1 ]]; then
  c_green='\033[32m'; c_yellow='\033[33m'; c_red='\033[31m'
  c_bold='\033[1m';   c_reset='\033[0m'
else
  c_green=''; c_yellow=''; c_red=''; c_bold=''; c_reset=''
fi

ok()   { echo -e "  ${c_green}✓${c_reset} $*"; }
warn() { echo -e "  ${c_yellow}⚠${c_reset}  $*"; }
err()  { echo -e "  ${c_red}✗${c_reset} $*"; }
step() { echo -e "\n${c_bold}$*${c_reset}"; }

echo -e "${c_bold}"
echo "  ┌────────────────────────────┐"
echo "  │   SIFTR — first-time setup │"
echo "  └────────────────────────────┘"
echo -e "${c_reset}"

# ── 1. Python version ─────────────────────────────────────────────────────────

step "Step 1 — Python version check"

# Pick up whichever python3/python is on PATH
if command -v python3 &>/dev/null; then
  SYS_PY=python3
elif command -v python &>/dev/null; then
  SYS_PY=python
else
  err "Python not found. Install Python 3.10+ from https://python.org and re-run."
  exit 1
fi

PY_VER=$($SYS_PY -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$($SYS_PY -c "import sys; print(sys.version_info.major)")
PY_MINOR=$($SYS_PY -c "import sys; print(sys.version_info.minor)")

if [[ "$PY_MAJOR" -lt 3 || ( "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ) ]]; then
  warn "Python $PY_VER detected. Python 3.10+ is recommended."
  warn "If you hit issues, install a newer version from https://python.org"
else
  ok "Python $PY_VER"
fi

# ── 2. Virtual environment ────────────────────────────────────────────────────

step "Step 2 — Virtual environment"

if [[ -d ".venv" && -x ".venv/bin/python" ]]; then
  ok "Virtual environment already exists — skipping"
else
  $SYS_PY -m venv .venv
  ok "Created .venv"
fi

PY=".venv/bin/python"
PIP=".venv/bin/pip"

# ── 3. Python dependencies ────────────────────────────────────────────────────

step "Step 3 — Python dependencies"

$PIP install --quiet --upgrade pip
$PIP install --quiet -r requirements.txt
ok "All requirements installed"

# ── 4. FFmpeg ─────────────────────────────────────────────────────────────────

step "Step 4 — FFmpeg"

if command -v ffmpeg &>/dev/null; then
  FFMPEG_VER=$(ffmpeg -version 2>&1 | head -1 | awk '{print $3}' || echo "installed")
  ok "FFmpeg $FFMPEG_VER"
else
  err "FFmpeg not found."
  echo ""
  echo "  Install FFmpeg, then re-run ./install.sh:"
  echo ""
  echo "    brew install ffmpeg        # macOS (Homebrew — recommended)"
  echo "    sudo apt install ffmpeg    # Ubuntu / Debian"
  echo ""
  echo "  Homebrew itself: https://brew.sh"
  echo ""
  exit 1
fi

# ── 5. Fresh-install: reset videos.yaml + initialise database ────────────────

step "Step 5 — Database"

if [[ ! -f ".siftr_installed" ]]; then
  # First run — write a clean videos.yaml so no dev test data is present
  cat > videos.yaml << 'YAML_EOF'
markets: []
YAML_EOF
  ok "videos.yaml reset to empty starter template"
fi

$PY setup.py

# ── 6. Verification ───────────────────────────────────────────────────────────

step "Step 6 — Verifying installation"

ALL_OK=true

# Python version inside the venv
VENV_PY_VER=$($PY -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "unknown")
if $PY -c "import sys; assert sys.version_info >= (3, 10)" 2>/dev/null; then
  ok "Python $VENV_PY_VER"
else
  warn "Python $VENV_PY_VER (3.10+ recommended)"
fi

# All required packages importable
if $PY -c "import flask, yt_dlp, PIL, imagehash, yaml, cv2" 2>/dev/null; then
  ok "Python packages"
else
  err "Some packages failed to import — try: $PIP install -r requirements.txt"
  ALL_OK=false
fi

# FFmpeg on PATH
if command -v ffmpeg &>/dev/null; then
  ok "FFmpeg on PATH"
else
  err "FFmpeg not found on PATH"
  ALL_OK=false
fi

# Database with expected tables
if [[ -f "database.sqlite" ]]; then
  TABLES=$($PY -c "
import sqlite3
conn = sqlite3.connect('database.sqlite')
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
print(','.join(tables))
" 2>/dev/null || echo "")
  if [[ "$TABLES" == *"videos"* && "$TABLES" == *"frames"* ]]; then
    ok "Database (tables: videos, frames)"
  else
    err "Database exists but is missing expected tables"
    ALL_OK=false
  fi
else
  err "database.sqlite not found"
  ALL_OK=false
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""

if $ALL_OK; then
  touch .siftr_installed
  echo -e "${c_green}${c_bold}✓ SIFTR setup complete.${c_reset}"
  echo ""
  echo "  To start the review UI:  python app.py"
  echo "  To run a harvest:        python harvest.py"
  echo "  The app will be at:      http://localhost:5001"
  echo ""
  echo -e "  ${c_yellow}Remember to activate the virtual environment first:${c_reset}"
  echo "    source .venv/bin/activate"
  echo ""
else
  echo -e "${c_red}Setup completed with errors.${c_reset}"
  echo "  Fix the issues marked ✗ above, then re-run: ./install.sh"
  echo ""
  exit 1
fi
