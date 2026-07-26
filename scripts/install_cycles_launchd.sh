#!/bin/zsh
# Install (or reinstall) the twice-daily hsai cycle launchd agents.
# Usage: ./scripts/install_cycles_launchd.sh [AM_HOUR] [PM_HOUR]   (defaults 9 and 15)
# Remove: launchctl unload ~/Library/LaunchAgents/com.hsai.cycle.*.plist && rm ~/Library/LaunchAgents/com.hsai.cycle.*.plist
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AM_HOUR="${1:-9}"
PM_HOUR="${2:-15}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$REPO_DIR/.hsai/logs"
mkdir -p "$AGENTS_DIR" "$LOG_DIR"

write_plist() {
  local label="$1" hour="$2" plist="$AGENTS_DIR/$1.plist"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd "$REPO_DIR" && source .venv/bin/activate && unset ANTHROPIC_API_KEY && hsai cycle >> "$LOG_DIR/cycle-\$(date +%%Y%%m%%d-%%H%%M).log" 2>&1</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$LOG_DIR/$label.out</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$label.err</string>
</dict>
</plist>
PLIST
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  echo "loaded $label (daily at $hour:00)"
}

write_plist "com.hsai.cycle.am" "$AM_HOUR"
write_plist "com.hsai.cycle.pm" "$PM_HOUR"
echo "Done. Review briefs will be waiting before your AM/PM reviews."
