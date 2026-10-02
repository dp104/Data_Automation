#!/bin/bash
# Started by launchd. Keeps the Mac awake while the team web app runs.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a; source "$DIR/team.env"; set +a
cd "$DIR" && exec /usr/bin/caffeinate -is "$DIR/.venv/bin/python" "$DIR/webapp.py"
