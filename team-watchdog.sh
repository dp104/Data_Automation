#!/bin/bash
# Run every 2 minutes by launchd: keep Tailscale connected and the team link (serve) configured.
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! pgrep -xq Tailscale; then open -ga Tailscale; sleep 5; fi
if $TS status 2>&1 | grep -q "Tailscale is stopped"; then
  echo "$(date '+%F %T') tailscale stopped -> up"; $TS up
fi
if ! $TS serve status 2>&1 | grep -q "127.0.0.1:8765"; then
  echo "$(date '+%F %T') serve missing -> re-adding"; $TS serve --bg 8765
fi
curl -s -m 5 -o /dev/null http://127.0.0.1:8765/ || { echo "$(date '+%F %T') web app down -> restart"; launchctl kickstart -k gui/$(id -u)/com.flyurdream.uniscrape; }
