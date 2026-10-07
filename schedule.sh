#!/usr/bin/env bash
# Daily automatic sync: launchd on macOS (catches up after sleep), cron on Linux.
#
#   ./schedule.sh install [HH:MM]   weekdays at HH:MM local time
#                                   (default: 18:45 Tokyo time, after J-Quants publishes)
#   ./schedule.sh status            is it installed, when it last ran
#   ./schedule.sh run               sync now through the scheduler
#   ./schedule.sh logs              follow the sync log
#   ./schedule.sh uninstall
set -euo pipefail
cd "$(dirname "$0")"
DIR=$(pwd)
LABEL=dev.tomtomtomdev.jquantster.sync
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$DIR/data/sync.log"
CRON_TAG="# jquantster-sync"

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }

default_time() {
  # 18:45 JST in this machine's timezone (prices ~16:30, breakdown ~18:00 JST).
  /usr/bin/env python3 - <<'PY'
from datetime import datetime
from zoneinfo import ZoneInfo
t = datetime.now(ZoneInfo("Asia/Tokyo")).replace(hour=18, minute=45, second=0)
print(t.astimezone().strftime("%H:%M"))
PY
}

job_command() {
  # Rotate the log at ~1 MB, sync, and raise a notification if it fails.
  local notify=""
  [[ "$(uname)" == Darwin ]] && notify=" || osascript -e 'display notification \"Sync failed. Run ./schedule.sh logs\" with title \"jquantster\"'"
  echo "cd '$DIR' && mkdir -p data && { [ \$(wc -c < '$LOG' 2>/dev/null || echo 0) -lt 1000000 ] || mv '$LOG' '$LOG.1'; } && { echo \"=== \$(date '+%Y-%m-%d %H:%M:%S')\"; ./run.sh --sync-only; } >> '$LOG' 2>&1$notify"
}

install() {
  local at=${1:-$(default_time)}
  [[ "$at" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]] || { echo "Time must be HH:MM, got '$at'" >&2; exit 2; }
  local hour=$((10#${BASH_REMATCH[1]})) minute=$((10#${BASH_REMATCH[2]}))
  grep -qE '^JQUANTS_API_KEY=.+' .env 2>/dev/null || { echo "No API key in .env yet. Run ./run.sh once first." >&2; exit 1; }
  mkdir -p data
  local path_dirs
  path_dirs="$(dirname "$(command -v uv)"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

  if [[ "$(uname)" == Darwin ]]; then
    mkdir -p "$(dirname "$PLIST")"
    {
      cat <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string><string>$(job_command | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g')</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$path_dirs</string></dict>
  <key>StartCalendarInterval</key>
  <array>
XML
      for day in 1 2 3 4 5; do
        echo "    <dict><key>Weekday</key><integer>$day</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>"
      done
      cat <<XML
  </array>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
XML
    } > "$PLIST"
    plutil -lint -s "$PLIST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed launchd job $LABEL: weekdays at $(printf '%02d:%02d' "$hour" "$minute") local time."
    echo "If the Mac is asleep then, it runs on wake. Log: $LOG"
  else
    local line="$minute $hour * * 1-5 PATH=$path_dirs bash -c \"$(job_command | sed 's/"/\\"/g')\" $CRON_TAG"
    { crontab -l 2>/dev/null | grep -v "$CRON_TAG" || true; echo "$line"; } | crontab -
    echo "Installed cron job: weekdays at $(printf '%02d:%02d' "$hour" "$minute"). Log: $LOG"
  fi
}

uninstall() {
  if [[ "$(uname)" == Darwin ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
  else
    { crontab -l 2>/dev/null | grep -v "$CRON_TAG" || true; } | crontab -
  fi
  echo "Removed the daily sync."
}

status() {
  if [[ "$(uname)" == Darwin ]]; then
    if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
      echo "Installed: $PLIST"
      launchctl print "gui/$(id -u)/$LABEL" | grep -E '^\s+(state|runs|last exit code)' || true
      echo "Schedule (local time):"
      /usr/libexec/PlistBuddy -c Print:StartCalendarInterval "$PLIST" \
        | awk '/Weekday/{d=$3} /Hour/{h=$3} /Minute/{m=$3} /^    }/{printf "  weekday %s at %02d:%02d\n", d, h, m}'
    else
      echo "Not installed. Run: ./schedule.sh install"
    fi
  else
    crontab -l 2>/dev/null | grep "$CRON_TAG" || echo "Not installed. Run: ./schedule.sh install"
  fi
  if [[ -f "$LOG" ]]; then
    echo "Last run:"
    awk '/^=== /{start=NR} {lines[NR]=$0} END{for(i=start;i<=NR;i++) print "  " lines[i]}' "$LOG" | tail -12
  fi
}

run_now() {
  if [[ "$(uname)" == Darwin ]] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl kickstart "gui/$(id -u)/$LABEL"
    echo "Started. Follow it with: ./schedule.sh logs"
  else
    bash -c "$(job_command)"
    tail -20 "$LOG"
  fi
}

case "${1:-}" in
  install) install "${2:-}" ;;
  uninstall) uninstall ;;
  status) status ;;
  run) run_now ;;
  logs) touch "$LOG"; tail -n 40 -f "$LOG" ;;
  *) usage; exit 2 ;;
esac
