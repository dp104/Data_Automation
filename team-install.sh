#!/bin/bash
# Installs a login agent so the team web app starts automatically and restarts if it stops.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PL=~/Library/LaunchAgents/com.flyurdream.uniscrape.plist
mkdir -p ~/Library/LaunchAgents "$DIR/logs"
cat > "$PL" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.flyurdream.uniscrape</string>
  <key>ProgramArguments</key><array><string>$DIR/team-start.sh</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/web.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/web.log</string>
</dict></plist>
PLIST
pkill -f "University_Scraper/webapp.py" 2>/dev/null
launchctl unload "$PL" 2>/dev/null; launchctl load "$PL" && echo "Team web app installed and running (logs: $DIR/logs/web.log)"
