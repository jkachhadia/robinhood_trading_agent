#!/bin/bash
# Generate and (un)install launchd agents for the scheduled trading runs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
mkdir -p "$DEST" "$ROOT/data/runs"

labels=(scan manage execute scan-intraday review)

if [ "${1:-}" = "remove" ]; then
  for l in "${labels[@]}"; do
    f="$DEST/com.tradeagent.$l.plist"
    [ -f "$f" ] && launchctl unload "$f" 2>/dev/null || true
    rm -f "$f"
  done
  echo "removed"
  exit 0
fi

plist_head() {
cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tradeagent.$1</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>$ROOT/scripts/run.sh</string>$2
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict>
    <key>TZ</key><string>America/New_York</string>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StandardOutPath</key><string>$ROOT/data/runs/launchd-$1.out</string>
  <key>StandardErrorPath</key><string>$ROOT/data/runs/launchd-$1.err</string>
EOF
}

cal() { # weekday hour minute
  echo "    <dict><key>Weekday</key><integer>$1</integer><key>Hour</key><integer>$2</integer><key>Minute</key><integer>$3</integer></dict>"
}

write_plist() { # label args(xml) calendar-entries(xml)
  {
    plist_head "$1" "$2"
    echo "  <key>StartCalendarInterval</key><array>"
    echo "$3"
    echo "  </array>"
    echo "</dict></plist>"
  } > "$DEST/com.tradeagent.$1.plist"
}

weekday_entries() { # hour minute
  for d in 1 2 3 4 5; do cal "$d" "$1" "$2"; done
}

write_plist scan "<string>scan</string>" "$(weekday_entries 8 30)"
write_plist scan-intraday "<string>scan</string><string>intraday</string>" "$(weekday_entries 14 30)"
write_plist execute "<string>execute</string>" "$(weekday_entries 9 50; weekday_entries 13 5)"
write_plist review "<string>review-day</string>" "$(weekday_entries 16 15)"

manage_entries=""
for d in 1 2 3 4 5; do
  for hm in "9 45" "10 15" "10 45" "11 15" "11 45" "12 15" "12 45" "13 15" "13 45" "14 15" "14 45" "15 15" "15 45"; do
    set -- $hm
    manage_entries+="$(cal "$d" "$1" "$2")"$'\n'
  done
done
write_plist manage "<string>manage</string>" "$manage_entries"

for l in "${labels[@]}"; do
  f="$DEST/com.tradeagent.$l.plist"
  plutil -lint "$f" >/dev/null
  launchctl unload "$f" 2>/dev/null || true
  launchctl load "$f"
  echo "loaded $f"
done
