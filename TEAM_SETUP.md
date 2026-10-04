# Team access via Tailscale (Google Workspace sign-in)

The scraper keeps running on this Mac. Tailscale gives it a private **https** link that only
people signed in with a **@flyurdream.com** Google account can open. Every scan, export and
download is recorded in `logs/audit.log` with the person's email.

## A. One-time setup on this Mac (host)

1. Install Tailscale (asks for the Mac password):
   ```bash
   brew install --cask tailscale-app
   ```
   Open **Tailscale** from Applications, allow the VPN/system extension when macOS asks.
2. In the Tailscale menu-bar icon choose **Log in** → **Sign in with Google** → use your
   **@flyurdream.com** account. This creates the Flyurdream tailnet.
3. In the admin console (https://login.tailscale.com/admin/dns): turn on **MagicDNS** and
   **HTTPS Certificates**.
4. Security note: this mode trusts the `Tailscale-User-Login` header with no signature of its own - safe
   only because `tailscale serve` is the sole way anything reaches this port. `team.env` already sets
   `UNISCRAPE_TRUST_TAILSCALE_HEADER=1` to acknowledge this; never set it if this port might ever be
   reachable another way (a stray port-forward, another local process, binding beyond 127.0.0.1).
5. Then (Claude can run these):
   ```bash
   cd ~/projects/University_Scraper
   sed -i '' 's/^UNISCRAPE_AUTH=.*/UNISCRAPE_AUTH=tailscale/' team.env
   ./team-install.sh                      # starts the web app now and at every login
   /Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 8765
   /Applications/Tailscale.app/Contents/MacOS/Tailscale serve status   # shows the team link
   ```
   The link looks like `https://<mac-name>.<tailnet>.ts.net`.

Keep the Mac on, plugged in and logged in. The app keeps it from sleeping while it runs.

## B. Each team member (once)

1. Install Tailscale: https://tailscale.com/download (Mac, Windows, phone).
2. Sign in with Google using their **@flyurdream.com** account (same tailnet automatically).
   If "device approval" is on, the admin approves them at https://login.tailscale.com/admin/machines
3. Open the team link. The page shows "Signed in as name@flyurdream.com".

Only @flyurdream.com accounts are accepted, by Tailscale and again by the tool itself.
Never invite partner agencies or personal accounts to this tailnet.

## Day to day

- Results are the same as running locally (same engine, same saved scans).
- Different scans (different universities, sites or projects) can run at the same time; the same one
  can never be scanned twice at once - a repeat request just reuses the scan already in progress.
- Files are saved on this Mac in `outputs/` and downloaded through the page.
- Stop: `launchctl unload ~/Library/LaunchAgents/com.flyurdream.uniscrape.plist`
- Start again: `./team-install.sh`
- Remove the link: `/Applications/Tailscale.app/Contents/MacOS/Tailscale serve reset`
