"""
Cookie Injector — CDP Proxy + Web UI

Two servers in one process:
  • Port 9224 — CDP HTTP endpoint (cookie-injector behavior)
  • Port 8001 — Web UI for uploading session JSON files

Playwright MCP (HTTP CDP) → This Proxy (persistent WS) → Browserless
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

import websockets
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
import uvicorn
from pydantic import BaseModel

# ─── Config ───────────────────────────────────────────────────────────────────
BROWSERLESS_WS = "ws://browserless:3000"
DATA_DIR = Path("/data")
SESSIONS_DIR = DATA_DIR / "sessions"
PROXY_PORT = 9224
WEB_PORT = 8001
# ─────────────────────────────────────────────────────────────────────────────

# ─── CDP Proxy App (port 9224) ────────────────────────────────────────────────
app_cdp = FastAPI(title="Cookie Injector CDP Proxy")

# ─── Web UI App (port 8001) ──────────────────────────────────────────────────
app_web = FastAPI(title="Cookie Injector Web UI")


# ─── Exceptions ────────────────────────────────────────────────────────────────

class CDPError(Exception):
    def __init__(self, err: dict):
        self.code = err.get("code", -1)
        self.message = err.get("message", "Unknown error")
        super().__init__(f"CDP Error {self.code}: {self.message}")


class ProxyError(Exception):
    """Raised when the proxy can't connect to Browserless."""
    pass


# ─── CDP WebSocket Client ─────────────────────────────────────────────────────

class BrowserlessConnection:
    """
    Maintains a persistent WebSocket connection to Browserless.
    All CDP commands from Playwright MCP are forwarded through this.
    Cookie injection happens on page creation/navigation.
    """

    def __init__(self):
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._recv_task: Optional[asyncio.Task] = None
        self._page_session_id: Optional[str] = None
        self._target_id: Optional[str] = None
        self._lock = asyncio.Lock()
        self._cookies_injected = False

    async def connect(self):
        if self._ws is not None and not self._ws.closed:
            return
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(BROWSERLESS_WS, ping_interval=None),
                timeout=10
            )
            self._recv_task = asyncio.create_task(self._receive_loop())
            print(f"[Proxy] Connected to Browserless")
        except asyncio.TimeoutError:
            raise ProxyError("Browserless connection timeout")
        except Exception as e:
            raise ProxyError(f"Browserless connection failed: {e}")

    async def _receive_loop(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id and msg_id in self._pending:
                    self._pending.pop(msg_id).set_result(msg)
                elif "method" in msg and "sessionId" not in msg:
                    if msg["method"] == "Target.attachedToTarget":
                        for k, fut in list(self._pending.items()):
                            if str(k).startswith("_evt_"):
                                fut.set_result(msg)
                                break
        except websockets.exceptions.ConnectionClosed:
            pass

    async def _send(self, method: str, params: dict = None, session_id: str = None) -> dict:
        await self.connect()
        async with self._lock:
            self._msg_id += 1
            msg = {"id": self._msg_id, "method": method}
            if params:
                msg["params"] = params
            if session_id:
                msg["sessionId"] = session_id
            fut = asyncio.Future()
            self._pending[self._msg_id] = fut
            await self._ws.send(json.dumps(msg))
            result = await fut
            self._pending.pop(self._msg_id, None)
            if "error" in result:
                raise CDPError(result["error"])
            return result.get("result", {})

    async def _wait_for_event(self, method: str, timeout: float = 10) -> dict:
        key = f"_evt_{method}"
        fut = asyncio.Future()
        self._pending[key] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(key, None)

    def load_cookies_for_url(self, url: str) -> Optional[list]:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc
        except Exception:
            return None

        if not SESSIONS_DIR.exists():
            return None

        for session_file in SESSIONS_DIR.glob("*.json"):
            try:
                with open(session_file) as f:
                    cookies = json.load(f)
                for c in cookies:
                    cookie_domain = c.get("domain", "").lstrip(".")
                    if cookie_domain and cookie_domain in domain:
                        SAMESITE_MAP = {
                            "unspecified": "Lax", "no_restriction": "None",
                            "strict": "Strict", "lax": "Lax", "none": "None",
                        }
                        for cookie in cookies:
                            cookie["sameSite"] = SAMESITE_MAP.get(
                                cookie.get("sameSite", ""), "Lax"
                            )
                        return cookies
            except Exception:
                continue
        return None

    async def ensure_page_with_cookies(self, url: str) -> str:
        cookies = self.load_cookies_for_url(url)
        if cookies:
            result = await self._send("Target.createTarget", {"url": "about:blank"})
            self._target_id = result["targetId"]

            attach_task = asyncio.create_task(self._wait_for_event("Target.attachedToTarget"))
            await self._send("Target.attachToTarget", {
                "targetId": self._target_id, "flatten": True
            })
            self._page_session_id = (await attach_task)["params"]["sessionId"]

        if cookies:
            print(f"[DEBUG] Setting {len(cookies)} cookies via Network.setCookies")
            set_result = await self._send(
                "Network.setCookies",
                {"cookies": cookies},
                session_id=self._page_session_id,
            )
            print(f"[DEBUG] Network.setCookies result: {set_result}")
            self._cookies_injected = True
            print(f"[DEBUG] Navigating to {url}")
        else:
            result = await self._send("Target.createTarget", {"url": "about:blank"})
            self._target_id = result["targetId"]
            attach_task = asyncio.create_task(self._wait_for_event("Target.attachedToTarget"))
            await self._send("Target.attachToTarget", {
                "targetId": self._target_id, "flatten": True
            })
            self._page_session_id = (await attach_task)["params"]["sessionId"]

        return url

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()


# ─── Global connection ─────────────────────────────────────────────────────────
_browserless: Optional[BrowserlessConnection] = None


async def get_browserless() -> BrowserlessConnection:
    global _browserless
    if _browserless is None:
        _browserless = BrowserlessConnection()
    return _browserless


# ─── CDP HTTP Endpoints (port 9224) ──────────────────────────────────────────

@app_cdp.post("/json/send")
async def cdp_send(request: Request):
    body = await request.json()
    method = body.get("method", "")
    params = body.get("params", {})
    session_id = request.headers.get("x-sesame-session-id", "")

    b = await get_browserless()

    if method == "Page.navigate" and not b._cookies_injected:
        url = params.get("url", "")
        if url:
            await b.ensure_page_with_cookies(url)
            try:
                result = await b._send(
                    "Page.navigate",
                    {"url": url},
                    session_id=b._page_session_id
                )
                return JSONResponse(result)
            except CDPError as e:
                return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=500)
            except ProxyError as e:
                return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=503)
            except Exception as e:
                return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=500)

    sid = session_id or b._page_session_id

    try:
        result = await b._send(method, params if params else None, session_id=sid)
        return JSONResponse(result)
    except ProxyError as e:
        return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=503)
    except CDPError as e:
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=500)


@app_cdp.post("/json/new")
async def cdp_new(request: Request):
    body = await request.json()
    url = body.get("url", "about:blank")

    b = await get_browserless()

    if url and url != "about:blank":
        await b.ensure_page_with_cookies(url)
        try:
            result = await b._send(
                "Page.navigate",
                {"url": url},
                session_id=b._page_session_id
            )
            return JSONResponse({"status": result.get("status", ""), "targetId": b._target_id, "id": 1})
        except ProxyError as e:
            return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=503)
        except CDPError as e:
            return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=500)
    else:
        result = await b._send("Target.createTarget", {"url": "about:blank"})
        b._target_id = result["targetId"]
        attach_task = asyncio.create_task(b._wait_for_event("Target.attachedToTarget"))
        await b._send("Target.attachToTarget", {"targetId": b._target_id, "flatten": True})
        b._page_session_id = (await attach_task)["params"]["sessionId"]
        return JSONResponse({"targetId": b._target_id, "id": 1})


@app_cdp.get("/json")
async def cdp_list():
    b = await get_browserless()
    try:
        result = await b._send("Target.getTargets")
        targets = result.get("targetInfos", [])
        formatted = [
            {
                "id": t["targetId"],
                "type": t["type"],
                "title": t.get("title", ""),
                "url": t.get("url", ""),
                "attached": t.get("attached", False),
            }
            for t in targets
        ]
        return JSONResponse(formatted)
    except ProxyError as e:
        return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=503)
    except CDPError as e:
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=500)


@app_cdp.get("/json/version")
async def cdp_version():
    b = await get_browserless()
    try:
        result = await b._send("Browser.getVersion")
        return JSONResponse(result)
    except ProxyError as e:
        return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=503)
    except CDPError as e:
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": {"code": -1, "message": str(e)}}, status_code=500)


@app_cdp.get("/json/protocol")
async def cdp_protocol():
    b = await get_browserless()
    try:
        result = await b._send("Schema.getDomains")
        return JSONResponse(result)
    except Exception:
        return JSONResponse({"domains": []})


@app_cdp.get("/status")
async def cdp_status():
    try:
        b = await get_browserless()
        return {"status": "ok", "connected_to": BROWSERLESS_WS, "has_session": b._page_session_id is not None}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── Session Management ────────────────────────────────────────────────────────

class InjectRequest(BaseModel):
    cookies: list
    goto_url: str = ""


class InjectResponse(BaseModel):
    message: str
    title: Optional[str] = None
    url: Optional[str] = None
    is_logged_in: bool = False


class SessionInfo(BaseModel):
    domain: str
    file: str
    cookie_count: int
    saved_at: Optional[str] = None


def get_session_path(domain: str) -> Path:
    safe = domain.lstrip(".").replace(".", "_")
    return SESSIONS_DIR / f"{safe}.json"


async def inject_and_show(cookies: list, goto_url: str) -> tuple[str, str, bool]:
    from playwright.async_api import async_playwright

    SAMESITE_MAP = {
        "unspecified": "Lax", "no_restriction": "None",
        "strict": "Strict", "lax": "Lax", "none": "None",
    }
    for c in cookies:
        raw = c.get("sameSite", "")
        c["sameSite"] = SAMESITE_MAP.get(raw, "Lax")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(BROWSERLESS_WS)
        ctx = await browser.new_context()
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        if not goto_url:
            domains = [c.get("domain", "") for c in cookies if c.get("domain")]
            goto_url = f"https://{domains[0].lstrip('.')}" if domains else "https://example.com"
        await page.goto(goto_url, wait_until="domcontentloaded", timeout=30000)
        title = await page.title()
        url = page.url
        is_logged_in = "/login" not in url
        screenshot_path = DATA_DIR / "last_screenshot.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return title, url, is_logged_in


@app_cdp.post("/inject", response_model=InjectResponse)
async def inject_cookies(req: InjectRequest):
    if not req.cookies:
        raise HTTPException(status_code=400, detail="No cookies provided")
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    for cookie in req.cookies:
        domain = cookie.get("domain", "")
        if domain:
            session_path = get_session_path(domain)
            with open(session_path, "w") as f:
                json.dump(req.cookies, f, indent=2)
            break

    try:
        title, url, is_logged_in = await inject_and_show(req.cookies, req.goto_url or "")
        return InjectResponse(message="Cookies injected!", title=title, url=url, is_logged_in=is_logged_in)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app_cdp.get("/sessions", response_model=list[SessionInfo])
async def list_sessions():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            with open(f) as fp:
                cookies = json.load(fp)
            domain = cookies[0].get("domain", "unknown") if cookies else "unknown"
            sessions.append(SessionInfo(domain=domain, file=f.name, cookie_count=len(cookies), saved_at=str(f.stat().st_mtime)))
        except Exception:
            pass
    return sorted(sessions, key=lambda s: s.domain)


@app_cdp.delete("/sessions/{filename}")
async def delete_session(filename: str):
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = SESSIONS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    path.unlink()
    return {"message": f"Deleted {filename}"}


@app_cdp.get("/screenshot")
async def screenshot():
    screenshot_path = DATA_DIR / "last_screenshot.png"
    if not screenshot_path.exists():
        raise HTTPException(status_code=404, detail="No screenshot yet")
    return FileResponse(screenshot_path, media_type="image/png")


# ─── Web UI Routes (port 8001) ───────────────────────────────────────────────

WEB_UI_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Cookie Injector — Session Upload</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
    *{box-sizing:border-box}
    body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:720px;margin:0 auto;padding:40px 20px;background:#0f1117;color:#e0e0e0}
    h1{color:#00d4ff;margin:0 0 8px}
    .subtitle{color:#666;margin-bottom:40px;font-size:15px}
    label{display:block;margin:24px 0 10px;color:#00d4ff;font-weight:600;font-size:14px}
    textarea{width:100%;height:220px;padding:14px;background:#1a1d27;color:#7bed9f;border:1px solid #2a2a3a;border-radius:10px;font-family:monospace;font-size:12px;resize:vertical}
    input[type="text"]{width:100%;padding:14px;background:#1a1d27;color:#fff;border:1px solid #2a2a3a;border-radius:10px;font-size:15px}
    .btn{background:#00d4ff;color:#000;padding:16px 28px;border:none;border-radius:10px;cursor:pointer;font-size:16px;font-weight:700;width:100%;margin-top:20px}
    .btn:hover{background:#00b8e6}
    .btn:disabled{background:#2a2a3a;color:#666;cursor:not-allowed}
    .status{margin-top:20px;padding:16px;background:#1a1d27;border-radius:10px;border-left:4px solid #00d4ff;line-height:1.6;display:none}
    .status.error{border-left-color:#ff4757;color:#ff8a94;display:block}
    .status.success{border-left-color:#2ed573;color:#7bed9f;display:block}
    .info-box{background:#1a1d27;padding:20px;border-radius:10px;border:1px solid #2a2a3a;margin-top:40px;font-size:13px;color:#888;line-height:1.7}
    .info-box strong{color:#00d4ff}
    .file-drop{background:#1a1d27;border:2px dashed #2a2a3a;border-radius:10px;padding:40px;text-align:center;color:#555;cursor:pointer;transition:border-color .2s,color .2s}
    .file-drop:hover,.file-drop.dragover{border-color:#00d4ff;color:#00d4ff}
    .file-drop input{display:none}
    .file-name{color:#7bed9f;font-size:13px;margin-top:10px;word-break:break-all}
</style>
</head>
<body>
    <h1>🍪 Cookie Injector</h1>
    <p class="subtitle">Upload a session JSON file to import cookies into browserless</p>

    <label>Session JSON file:</label>
    <div class="file-drop" id="dropZone" onclick="document.getElementById('fileInput').click()">
        <input type="file" id="fileInput" accept=".json">
        <div style="font-size:40px;margin-bottom:10px">📁</div>
        <div>Click or drag & drop your session .json file here</div>
        <div class="file-name" id="fileName"></div>
    </div>

    <label>Or paste cookies JSON directly:</label>
    <textarea id="cookieInput" placeholder='[
  {"name": "session_id", "value": "xxx", "domain": ".example.com", "path": "/", "secure": true},
  ...
]'></textarea>

    <label>URL to open (optional):</label>
    <input type="text" id="gotoUrl" placeholder="https://any-site.com" value="">

    <button class="btn" id="injectBtn" onclick="doInject()">🚀 Import Session</button>

    <div class="status" id="status"></div>

    <div class="info-box">
        <strong>How to get session JSON:</strong><br>
        1. Install <strong>EditThisCookie</strong> browser extension<br>
        2. Log into your site (x.com, LinkedIn, etc.)<br>
        3. Click the extension → Export → copy the JSON<br>
        4. Paste above or upload the exported .json file<br><br>
        Sessions are saved automatically and used for all future browser sessions.
    </div>

    <script>
        const dropZone = document.getElementById('dropZone');
        const fileInput = document.getElementById('fileInput');
        const fileNameEl = document.getElementById('fileName');

        dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
        dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
        dropZone.addEventListener('drop', e => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
            const file = e.dataTransfer.files[0];
            if (file) handleFile(file);
        });

        fileInput.addEventListener('change', e => {
            if (e.target.files[0]) handleFile(e.target.files[0]);
        });

        function handleFile(file) {
            fileNameEl.textContent = '📄 ' + file.name;
            const reader = new FileReader();
            reader.onload = e => document.getElementById('cookieInput').value = e.target.result;
            reader.readAsText(file);
        }

        async function doInject() {
            const btn = document.getElementById('injectBtn');
            const status = document.getElementById('status');
            const cookieVal = document.getElementById('cookieInput').value.trim();
            const gotoUrl = document.getElementById('gotoUrl').value.trim();

            if (!cookieVal) {
                status.className = 'status error';
                status.textContent = '❌ Paste cookies JSON or upload a session file first';
                return;
            }

            let parsed;
            try {
                parsed = JSON.parse(cookieVal);
            } catch(e) {
                status.className = 'status error';
                status.textContent = '❌ Invalid JSON: ' + e.message;
                return;
            }

            btn.disabled = true;
            btn.textContent = '⏳ Importing...';
            status.className = 'status';
            status.style.display = 'block';
            status.textContent = '⏳ Saving session...';

            try {
                const res = await fetch('http://localhost:9224/inject', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ cookies: parsed, goto_url: gotoUrl })
                });
                const data = await res.json();

                if (res.ok) {
                    status.className = 'status success';
                    status.innerHTML = `✅ <strong>Session imported!</strong><br><br>
                        Page: ${data.title || 'N/A'}<br>
                        URL: ${data.url || 'N/A'}<br>
                        Logged in: ${data.is_logged_in ? '✅ Yes' : '❌ No (check cookies)'}`;
                } else {
                    status.className = 'status error';
                    status.textContent = '❌ ' + (data.detail || data.message || 'Unknown error');
                }
            } catch(e) {
                status.className = 'status error';
                status.textContent = '❌ Connection error: ' + e.message + ' (is cookie-injector running?)';
            } finally {
                btn.disabled = false;
                btn.textContent = '🚀 Import Session';
            }
        }
    </script>
</body>
</html>
"""


@app_web.get("/", response_class=HTMLResponse)
async def web_index():
    return WEB_UI_HTML


@app_web.get("/sessions", response_model=list[SessionInfo])
async def web_list_sessions():
    """Proxy to CDP app's sessions endpoint."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("http://localhost:9224/sessions")
            return resp.json()
    except Exception as e:
        return [{"domain": "error", "file": str(e), "cookie_count": 0}]


@app_web.delete("/sessions/{filename}")
async def web_delete_session(filename: str):
    import httpx
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.delete(f"http://localhost:9224/sessions/{filename}")
        return resp.json()


@app_web.get("/screenshot")
async def web_screenshot():
    screenshot_path = DATA_DIR / "last_screenshot.png"
    if not screenshot_path.exists():
        raise HTTPException(status_code=404, detail="No screenshot yet")
    return FileResponse(screenshot_path, media_type="image/png")


# ─── Start Both Servers ───────────────────────────────────────────────────────

def main():
    import concurrent.futures
    print(f"Cookie Injector starting...")
    print(f"  CDP Proxy:  http://127.0.0.1:{PROXY_PORT}")
    print(f"  Web UI:     http://127.0.0.1:{WEB_PORT}")
    print(f"  → Browserless WS: {BROWSERLESS_WS}")
    print(f"  → Sessions dir: {SESSIONS_DIR}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        print("[STARTUP] Launching CDP proxy thread...")
        executor.submit(lambda: uvicorn.run(app_cdp, host="127.0.0.1", port=PROXY_PORT, log_level="info"))
        print("[STARTUP] Launching web UI thread...")
        executor.submit(lambda: uvicorn.run(app_web, host="127.0.0.1", port=WEB_PORT, log_level="info"))
        # Keep main thread alive
        import time
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
