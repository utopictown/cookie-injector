"""
CDP WebSocket client for browserless.
browserless uses standard Chrome DevTools Protocol over WebSocket.
"""

import asyncio
import json
import websockets
from typing import Optional, Callable


class CDPClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._pending: dict[int, asyncio.Future] = {}
        self._listening_task: Optional[asyncio.Task] = None
        self._session_id: Optional[str] = None
    
    async def connect(self):
        self.ws = await websockets.connect(self.ws_url, ping_interval=None)
        self._listening_task = asyncio.create_task(self._listen())
    
    async def _listen(self):
        """Listen for all incoming messages, route to pending futures or emit events."""
        async for raw in self.ws:
            msg = json.loads(raw)
            msg_id = msg.get("id")
            if msg_id and msg_id in self.pending:
                self.pending[msg_id].set_result(msg)
            elif "method" in msg and "sessionId" not in msg:
                # Browser-level event
                pass  # ignore for now
    
    @property
    def pending(self) -> dict:
        return self._pending
    
    async def send(self, method: str, params: Optional[dict] = None, session_id: Optional[str] = None) -> dict:
        """Send CDP command, return result dict."""
        msg = {"id": id(self), "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        
        fut = asyncio.Future()
        self._pending[id(fut)] = fut
        
        await self.ws.send(json.dumps(msg))
        result = await fut
        del self._pending[id(fut)]
        
        if "error" in result:
            raise CDPError(result["error"])
        return result.get("result", {})
    
    async def close(self):
        if self._listening_task:
            self._listening_task.cancel()
        if self.ws:
            await self.ws.close()


class BrowserlessCDP(CDPClient):
    """CDP client specifically for browserless WebSocket."""
    
    def __init__(self, ws_url: str = "wss://vm-0-163-ubuntu.tailad2bea.ts.net:9222"):
        super().__init__(ws_url)
        self._browser_session_id: Optional[str] = None
    
    async def connect(self):
        await super().connect()
        # First message is usually Browser.getVersion result
        # Just drain it
        await asyncio.wait_for(asyncio.get_event_loop().create_task(self._drain_browser_events()), timeout=2)
    
    async def _drain_browser_events(self):
        """Drain browser-level events that come before we're attached."""
        try:
            async for raw in asyncio.wait_for(self.ws.recv(), timeout=3):
                msg = json.loads(raw)
                if msg.get("id") == id(self):
                    return msg
        except asyncio.TimeoutError:
            pass


async def inject_cookies_and_navigate(cookies: list, goto_url: str, ws_url: str = "wss://vm-0-163-ubuntu.tailad2bea.ts.net:9222") -> tuple[str, str]:
    """
    Connect to browserless via CDP, inject cookies, navigate to URL.
    Browser stays open after return — caller closes when done.
    Returns (title, url).
    """
    client = BrowserlessCDP(ws_url)
    await client.connect()
    
    try:
        # Get browser version (drain welcome)
        try:
            await client.send("Browser.getVersion")
        except asyncio.TimeoutError:
            pass
        
        # Create a new blank page target
        result = await client.send("Target.createTarget", {"url": "about:blank"})
        target_id = result["targetId"]
        
        # Attach to the target — returns an event, not a response
        await client.send("Target.attachToTarget", {"targetId": target_id})
        
        # The attach event comes as a message — need to capture sessionId
        # Since we're using a simple send/recv model, let's try a different approach:
        # Actually, Target.attachToTarget fires Target.attachedToTarget event
        # We need to listen for it
        attach_task = asyncio.create_task(client._listen_for_event("Target.attachedToTarget"))
        
        await client.send("Target.attachToTarget", {"targetId": target_id})
        attach_event = await asyncio.wait_for(attach_task, timeout=5)
        
        session_id = attach_event["params"]["sessionId"]
        
        # Helper to send on the attached session
        async def sess(method, params=None):
            return await client.send(method, params, session_id=session_id)
        
        # Set cookies
        cdp_cookies = []
        for c in cookies:
            cdp_cookies.append({
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "secure": c.get("secure", True),
            })
        
        await sess("Page.setCookie", {"cookies": cdp_cookies})
        
        # Navigate
        await sess("Page.navigate", {"url": goto_url})
        
        # Wait for load
        try:
            await sess("Page.waitForLoadState", {"state": "networkidle"})
        except:
            pass
        
        # Get title
        r = await sess("Runtime.evaluate", {"expression": "document.title"})
        title = r.get("result", {}).get("value", "")
        
        # Get URL
        r = await sess("Runtime.evaluate", {"expression": "window.location.href"})
        url = r.get("result", {}).get("value", "")
        
        # Detach but KEEP browser alive
        await client.send("Target.detachFromTarget", {"sessionId": session_id})
        
        return title, url
        
    finally:
        await client.close()
