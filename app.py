"""
Cookie Injector — Inject cookies into browserless via Playwright
Run: python app.py
Access: http://127.0.0.1:8080 (then tailscale serve)
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI(title="Cookie Injector")

BROWSERLESS_WS = "wss://vm-0-163-ubuntu.tailad2bea.ts.net:9222"
DATA_DIR = Path("/data")
COOKIES_FILE = DATA_DIR / "cookies.json"
GOTO_FILE = DATA_DIR / "goto_url.txt"


# ============================================================================
# Playwright cookie injection
# ============================================================================

async def inject_and_show(cookies: list, goto_url: str) -> tuple[str, str]:
    """
    Connect to browserless, inject cookies, open URL.
    Browser stays open after return.
    Returns (title, url).
    """
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(BROWSERLESS_WS)
        ctx = await browser.new_context()
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.goto(goto_url, wait_until="networkidle", timeout=30000)
        title = await page.title()
        url = page.url
        # DON'T close — keep browser open for user
        return title, url


# ============================================================================
# FastAPI routes
# ============================================================================

class InjectRequest(BaseModel):
    cookies: list
    goto_url: str = "https://x.com/home"


class InjectResponse(BaseModel):
    message: str
    title: Optional[str] = None
    url: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>🍪 Cookie Injector</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { box-sizing: border-box; }
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; 
                max-width: 680px; margin: 0 auto; padding: 30px 20px; 
                background: #0f1117; color: #e0e0e0; 
            }
            h1 { color: #00d4ff; margin: 0 0 5px; }
            .subtitle { color: #666; margin-bottom: 30px; font-size: 14px; }
            
            label { display: block; margin: 20px 0 8px; color: #00d4ff; font-weight: 600; font-size: 14px; }
            
            textarea { 
                width: 100%; height: 200px; padding: 14px; 
                background: #1a1d27; color: #7bed9f; border: 1px solid #2a2a3a; 
                border-radius: 10px; font-family: 'Courier New', monospace; font-size: 12px;
                resize: vertical;
            }
            
            input[type="text"] { 
                width: 100%; padding: 14px; 
                background: #1a1d27; color: #fff; border: 1px solid #2a2a3a; 
                border-radius: 10px; font-size: 15px;
            }
            
            button { 
                background: #00d4ff; color: #000; padding: 14px 24px; 
                border: none; border-radius: 10px; cursor: pointer; 
                font-size: 15px; font-weight: 700; margin-top: 20px; width: 100%;
            }
            button:hover { background: #00b8e6; }
            button:disabled { background: #2a2a3a; color: #666; cursor: not-allowed; }
            
            .status { 
                margin-top: 20px; padding: 16px; 
                background: #1a1d27; border-radius: 10px; 
                border-left: 4px solid #00d4ff; line-height: 1.6;
            }
            .status.error { border-left-color: #ff4757; color: #ff8a94; }
            .status.success { border-left-color: #2ed573; color: #7bed9f; }
            
            .hint { font-size: 12px; color: #555; margin-top: 6px; }
            
            .info-box {
                background: #1a1d27; padding: 16px; border-radius: 10px;
                border: 1px solid #2a2a3a; margin-top: 30px; font-size: 13px; color: #888;
            }
            .info-box strong { color: #00d4ff; }
        </style>
    </head>
    <body>
        <h1>🍪 Cookie Injector</h1>
        <p class="subtitle">Inject cookies into browserless — no passwords shared</p>
        
        <label>Cookie JSON (from EditThisCookie):</label>
        <textarea id="cookieInput" placeholder='[&#10;  {"name": "auth_token", "value": "xxx", "domain": ".x.com", "path": "/", "secure": true},&#10;  {"name": "ct0", "value": "yyy", ...},&#10;  ...&#10;]'></textarea>
        <p class="hint">Export ALL cookies for the domain. Include auth_token, ct0, guest_id, etc.</p>
        
        <label>URL to verify (login check):</label>
        <input type="text" id="gotoUrl" value="https://x.com/home">
        
        <button id="injectBtn" onclick="doInject()">🚀 Inject & Verify Login</button>
        
        <div id="status"></div>
        
        <div class="info-box">
            <strong>How it works:</strong><br>
            1. Log into the site in YOUR browser, export cookies with EditThisCookie<br>
            2. Paste the JSON, click Inject<br>
            3. Cookie Injector opens browserless with your cookies<br>
            4. 🔴 Browserless stays OPEN — close it manually when done<br>
            5. Reconnect anytime — same cookies work for days
        </div>
        
        <script>
            let loading = false;
            
            async function doInject() {
                if (loading) return;
                const btn = document.getElementById('injectBtn');
                const status = document.getElementById('status');
                const cookies = document.getElementById('cookieInput').value.trim();
                const gotoUrl = document.getElementById('gotoUrl').value.trim();
                
                if (!cookies) {
                    status.className = 'status error';
                    status.textContent = '❌ Paste cookies JSON first';
                    return;
                }
                
                try {
                    loading = true;
                    btn.disabled = true;
                    btn.textContent = '⏳ Connecting...';
                    status.className = 'status';
                    status.textContent = '⏳ Connecting to browserless...';
                    
                    const parsed = JSON.parse(cookies);
                    
                    const res = await fetch('/inject', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ cookies: parsed, goto_url: gotoUrl })
                    });
                    
                    const data = await res.json();
                    
                    if (res.ok) {
                        status.className = 'status success';
                        status.innerHTML = \`✅ \${data.message}<br><br>
                            <strong>Page title:</strong> \${data.title || 'N/A'}<br>
                            <strong>URL:</strong> \${data.url || 'N/A'}<br><br>
                            <em>🔴 Browser is OPEN — close it manually at browserless when done.</em>\`;
                    } else {
                        status.className = 'status error';
                        status.textContent = '❌ ' + data.detail;
                    }
                } catch(e) {
                    status.className = 'status error';
                    status.textContent = '❌ ' + e.message;
                } finally {
                    loading = false;
                    btn.disabled = false;
                    btn.textContent = '🚀 Inject & Verify Login';
                }
            }
        </script>
    </body>
    </html>
    """


@app.post("/inject", response_model=InjectResponse)
async def inject_cookies(req: InjectRequest):
    if not req.cookies:
        raise HTTPException(status_code=400, detail="No cookies provided")
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(COOKIES_FILE, "w") as f:
        json.dump(req.cookies, f, indent=2)
    with open(GOTO_FILE, "w") as f:
        f.write(req.goto_url)
    
    try:
        title, url = await inject_and_show(req.cookies, req.goto_url)
        return InjectResponse(
            message="Cookies injected! Browser opened with your session.",
            title=title,
            url=url
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status")
async def status():
    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            resp = await client.get(f"https://vm-0-163-ubuntu.tailad2bea.ts.net:9222/json/version")
            data = resp.json()
            return {"status": "ok", "browser": data.get("Browser")}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
