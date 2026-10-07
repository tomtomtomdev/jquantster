#!/usr/bin/env bash
# One-step install and run: installs uv and dependencies, asks for your
# J-Quants API key (and an optional EDINET key) on first run, syncs new data,
# then opens the dashboard.
#
#   ./run.sh              install (if needed) → sync → dashboard
#   ./run.sh --no-sync    skip the sync, just open the dashboard
#   ./run.sh --sync-only  sync and exit (handy for cron; never prompts without a terminal)
#   JQUANTS_API_KEY=... JQUANTS_PLAN=light EDINET_API_KEY=... ./run.sh   set up without prompts
set -euo pipefail
cd "$(dirname "$0")"

do_sync=1
do_ui=1
for arg in "$@"; do
  case "$arg" in
    --no-sync) do_sync=0 ;;
    --sync-only) do_ui=0 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# 3. Config: create .env and ask for the keys on first run
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
env_value() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
# Values to write to .env, passed to Python as JQUANTSTER_SET_<NAME> so special characters are safe.
updates=()
if [[ -z "$(env_value JQUANTS_API_KEY)" ]]; then
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
  updates+=("JQUANTSTER_SET_JQUANTS_API_KEY=$key" "JQUANTSTER_SET_JQUANTS_PLAN=${plan:-free}")
fi
# Optional EDINET key: taken from the environment, or asked once in a terminal.
# Pressing Enter writes EDINET_SKIP=1 so later runs don't ask again.
if [[ -z "$(env_value EDINET_API_KEY)" && "$(env_value EDINET_SKIP)" != 1 ]]; then
  edinet_key=${EDINET_API_KEY:-}
  if [[ -z "$edinet_key" && -t 0 ]]; then
    echo "Optional: EDINET (FSA filings, large shareholders, balance sheets) needs a free key"
    echo "from https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1"
    read -rsp "Paste your EDINET API key (hidden, Enter to skip): " edinet_key; echo
    [[ -n "$edinet_key" ]] || { updates+=("JQUANTSTER_SET_EDINET_SKIP=1"); echo "Skipped. Add EDINET_API_KEY to .env any time."; }
  fi
  [[ -z "$edinet_key" ]] || updates+=("JQUANTSTER_SET_EDINET_API_KEY=$edinet_key")
fi
if (( ${#updates[@]} )); then
  # Edit .env in Python so special characters in the keys are written as-is. A commented
  # template line ("# NAME=") is replaced in place; otherwise the line is appended.
  env "${updates[@]}" uv run --quiet python - <<'EOF'
import os, re
from pathlib import Path
env = Path(".env")
text = env.read_text()
for var, value in os.environ.items():
    if not var.startswith("JQUANTSTER_SET_"):
        continue
    name = var[len("JQUANTSTER_SET_"):]
    line = f"{name}={value}"
    for pattern in (rf"^{name}=.*$", rf"^# ?{name}=.*$"):
        text, n = re.subn(pattern, lambda _: line, text, count=1, flags=re.M)
        if n:
            break
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
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
