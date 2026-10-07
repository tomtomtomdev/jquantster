#!/usr/bin/env bash
# One-step install and run: installs uv and dependencies, asks for your
# J-Quants API key on first run, syncs new data, then opens the dashboard.
#
#   ./run.sh              install (if needed) → sync → dashboard
#   ./run.sh --no-sync    skip the sync, just open the dashboard
#   ./run.sh --sync-only  sync and exit (handy for cron)
#   JQUANTS_API_KEY=... JQUANTS_PLAN=light ./run.sh   set up without prompts
set -euo pipefail
cd "$(dirname "$0")"

do_sync=1
do_ui=1
for arg in "$@"; do
  case "$arg" in
    --no-sync) do_sync=0 ;;
    --sync-only) do_ui=0 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }

# 1. uv (manages Python and dependencies)
if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv (https://docs.astral.sh/uv/)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

# 2. Python + dependencies (no-op when already up to date)
step "Installing dependencies"
uv sync --quiet

# 3. Config: create .env and ask for the key on first run
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
saved_key=$(grep -E '^JQUANTS_API_KEY=' .env | cut -d= -f2- || true)
if [[ -z "$saved_key" ]]; then
  step "First-time setup"
  key=${JQUANTS_API_KEY:-}
  plan=${JQUANTS_PLAN:-}
  if [[ -z "$key" ]]; then
    if [[ ! -t 0 ]]; then
      echo "JQUANTS_API_KEY is empty in .env. Add it, or run ./run.sh in a terminal to be asked." >&2
      exit 1
    fi
    echo "Get a key at https://jpx-jquants.com → dashboard → API Keys"
    read -rsp "Paste your J-Quants API key (hidden): " key; echo
    [[ -n "$key" ]] || { echo "No key entered." >&2; exit 1; }
    read -rp "Plan [free/light/standard/premium] (default free): " plan
  fi
  plan=${plan:-free}
  # Edit .env in Python so special characters in the key are written as-is.
  JQ_KEY="$key" JQ_PLAN="$plan" uv run --quiet python - <<'EOF'
import os, re
from pathlib import Path
env = Path(".env")
text = env.read_text()
for name, value in (("JQUANTS_API_KEY", os.environ["JQ_KEY"]), ("JQUANTS_PLAN", os.environ["JQ_PLAN"])):
    line = f"{name}={value}"
    text, n = re.subn(rf"^{name}=.*$", lambda _: line, text, flags=re.M)
    if not n:
        text += f"\n{line}\n"
env.write_text(text)
EOF
  echo "Saved to .env (git-ignored)."
fi

# 4. Fetch new data (rate-limited; re-runs only fetch what's new)
if (( do_sync )); then
  step "Syncing data from J-Quants"
  if ! uv run jquantster sync; then
    (( do_ui )) || exit 1  # let schedulers see the failure
    echo "Sync finished with some errors (see above); continuing."
  fi
fi

# 5. Dashboard
if (( do_ui )); then
  step "Starting dashboard (Ctrl+C to stop)"
  exec uv run jquantster ui
fi
