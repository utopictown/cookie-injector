# Cookie Injector

A tiny web app to inject browser cookies into browserless — log into any site without sharing passwords. Sessions are persisted per-domain so you can manage multiple site sessions.

**Flow:** You log in manually → export cookies → paste into app → browserless opens with your session.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Main UI |
| POST | `/inject` | Inject cookies & open site |
| GET | `/sessions` | List all saved sessions |
| DELETE | `/sessions/{filename}` | Delete a session |
| GET | `/screenshot` | View last screenshot |
| GET | `/status` | Check browserless status |

## Setup

### 1. Deploy on your VPS (same machine as browserless)

```bash
cd ~/cookie-injector
docker-compose up -d
```

The app binds to `127.0.0.1:8001` only — it's not exposed to the internet.

### 2. Expose via Tailscale Serve

```bash
tailscale serve http://127.0.0.1:8001
```

Now it's accessible at `https://your-vps-name.tail-scale-name.ts.net/` via your tailnet.

### 3. Get cookies from your browser

1. Install [EditThisCookie](https://chrome.google.com/webstore/detail/editthiscookie/fngmhnnpilhplaeedifhccceomclgfbg) Chrome extension
2. Go to the site you want (e.g., X.com, LinkedIn), log in manually
3. Click the EditThisCookie icon → **Export** → copies JSON to clipboard
4. Paste into Cookie Injector, enter the URL you want to visit, click **Inject Cookies & Open Site**

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

- **Session duration varies by site** — some sites (LinkedIn) keep you logged in for weeks, others (X.com) may need re-exporting cookies more frequently
- **browserless must be running** on the same VPS
- **No nginx needed** — app connects directly to `wss://vm-0-163-ubuntu.tailad2bea.ts.net:9222`

## Troubleshooting

**"Connection refused" error:**
- Check browserless is running: `curl https://vm-0-163-ubuntu.tailad2bea.ts.net:9222/json/version`
- Check Cookie Injector is running: `docker logs cookie-injector`

**Login didn't work:**
- Make sure you exported ALL cookies for the domain, not just a subset
- Some sites need specific cookies (e.g., LinkedIn needs `li_at`, X.com needs `auth_token`, `ct0`, `guest_id`)
- Try logging out and back in, then re-export all cookies
