# Cookie Injector

A tiny web app to inject browser cookies into browserless — log into any site without sharing passwords.

**Flow:** You log in manually → export cookies → paste into app → browserless opens with your session.

## Setup

### 1. Deploy on your VPS (same machine as browserless)

```bash
cd ~/cookie-injector
docker-compose up -d
```

The app binds to `127.0.0.1:8080` only — it's not exposed to the internet.

### 2. Expose via Tailscale Serve

```bash
tailscale serve http://127.0.0.1:8080
```

Now it's accessible at `https://your-vps-name.tail-scale-name.ts.net/` via your tailnet.

### 3. Get cookies from your browser

1. Install [EditThisCookie](https://chrome.google.com/webstore/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg) Chrome extension
2. Go to the site you want (e.g., X.com, LinkedIn), log in manually
3. Click the EditThisCookie icon → **Export** → copies JSON to clipboard
4. Paste into Cookie Injector, click **Inject & Verify Login**

### 4. Done!

- Browserless opens the URL with your cookies — you're logged in
- The browser **stays open** in browserless until you manually close it
- Next time, same cookies work for weeks (LinkedIn) or days (X.com)

## Files

```
cookie-injector/
├── app.py              # FastAPI web app
├── requirements.txt    # Python deps
├── Dockerfile          # Docker image
├── docker-compose.yml  # Compose file
├── data/              # Saved cookies (gitignored)
└── README.md
```

## Important notes

- **X.com sessions are ephemeral** — may need to re-export cookies every few days
- **LinkedIn sessions persist for weeks** — cookies work long-term
- **browserless must be running** on the same VPS
- **No nginx needed** — app connects directly to `wss://vm-0-163-ubuntu.tailad2bea.ts.net:9222`

## Troubleshooting

**"Connection refused" error:**
- Check browserless is running: `curl https://vm-0-163-ubuntu.tailad2bea.ts.net:9222/json/version`
- Check Cookie Injector is running: `docker logs cookie-injector`

**Login didn't work:**
- Make sure you exported ALL cookies, not just auth_token
- Some sites need additional cookies (x.com needs `auth_token` AND `ct0` AND `guest_id`)
- Try logging out and back in, then re-export
