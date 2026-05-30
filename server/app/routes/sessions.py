from fastapi import APIRouter, HTTPException
from app.models import LoginRequest
from app.store import STORE
from app.config import settings
from app.proxy_helper import get_proxy_url
from playwright.async_api import async_playwright
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
import uuid
from pathlib import Path

router = APIRouter(prefix="/api/sessions")

# Active browser instances: name -> session data
_active_sessions: dict = {}

# Must match the persistent-context viewport below; input coords arrive
# normalized in [0,1] and are scaled to these dimensions.
VIEWPORT_W = 1280
VIEWPORT_H = 800

import re as _re


def _sanitize_name(raw: str) -> str:
    """Keep a filesystem/profile-safe session name (alnum, dash, underscore)."""
    name = _re.sub(r"[^A-Za-z0-9_\-]", "-", raw.strip())
    return name[:64] or f"ctx-{uuid.uuid4().hex[:8]}"


@router.get("/")
async def list_sessions():
    return await STORE.list_sessions()


@router.post("/scan")
async def scan_sessions():
    """Scan browser cookies, auto-detect logged-in sites, and save as sessions."""
    from app.cookie_scanner import scan_browser_cookies
    detected = scan_browser_cookies()
    saved = []
    for site in detected:
        name = site["name"]
        await STORE.save_session(name, url=site["url"], cookies=site["cookies"])
        saved.append({
            "name": name,
            "url": site["url"],
            "label": site["label"],
            "cookie_count": site["cookie_count"],
            "auth_cookies_found": site.get("auth_cookies_found", []),
        })
    return {"success": True, "detected": saved}


@router.post("/create")
async def create_browser(body: dict = {}):
    """Launch a headless browser session for in-app remote-control login.

    Body: `{name?, url?}`. The page is streamed via `/ws/sessions/{name}/stream`
    and driven through `/api/sessions/{name}/input`; cookies are persisted by
    `/confirm-login`.
    """
    raw_name = (body or {}).get("name") or ""
    name = _sanitize_name(raw_name) if raw_name else f"ctx-{uuid.uuid4().hex[:8]}"
    url = (body or {}).get("url") or ""

    # Reuse an already-active session of the same name (profile dir is locked).
    if name in _active_sessions:
        return {"success": True, "session_name": name, "reused": True}

    try:
        pw = await async_playwright().start()
        profile_dir = Path(settings.DATA_DIR).resolve() / "browser_profiles" / name
        profile_dir.mkdir(parents=True, exist_ok=True)

        launch_opts: dict = {
            "channel": "chrome",
            "headless": True,
            "viewport": {"width": VIEWPORT_W, "height": VIEWPORT_H},
            "args": [
                "--disable-blink-features=AutomationControlled",
            ],
        }
        proxy_url = await get_proxy_url()
        if proxy_url:
            # Parse http://user:pass@host:port into Playwright proxy format
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            proxy_conf: dict = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_conf["username"] = parsed.username
            if parsed.password:
                proxy_conf["password"] = parsed.password
            launch_opts["proxy"] = proxy_conf
            import logging
            logging.getLogger("sessions").info(f"Using proxy: {parsed.hostname}:{parsed.port}")

        context = await pw.chromium.launch_persistent_context(
            str(profile_dir), **launch_opts
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Anti-detection: apply full playwright-stealth (same as crawl4ai)
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)

        _active_sessions[name] = {
            "playwright": pw,
            "context": context,
            "page": page,
            "ws_clients": set(),
            "streaming": False,
            "stream_task": None,
        }

        if url:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                import logging
                logging.getLogger("sessions").warning(f"page.goto({url}) failed: {e}")

        return {"success": True, "session_name": name, "url": page.url}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/{name}/input")
async def session_input(name: str, body: dict):
    """Forward a single user input event to the live page (remote control).

    Coordinates `x`/`y` are normalized fractions in [0,1] of the streamed frame.
    Supported `type`: click, move, scroll, type, key, navigate.
    """
    session = _active_sessions.get(name)
    if not session:
        raise HTTPException(404, "Session not found")
    page = session["page"]
    etype = (body or {}).get("type")

    try:
        if etype in ("click", "move"):
            x = float(body.get("x", 0)) * VIEWPORT_W
            y = float(body.get("y", 0)) * VIEWPORT_H
            if etype == "click":
                await page.mouse.click(x, y)
            else:
                await page.mouse.move(x, y)
        elif etype == "scroll":
            await page.mouse.wheel(0, float(body.get("dy", 0)))
        elif etype == "type":
            await page.keyboard.type(body.get("text", ""))
        elif etype == "key":
            key = body.get("key", "")
            if key:
                await page.keyboard.press(key)
        elif etype == "navigate":
            nav_url = body.get("url", "about:blank")
            try:
                await page.goto(nav_url, wait_until="load", timeout=30000)
            except Exception as e:
                import logging
                logging.getLogger("sessions").warning(f"navigate({nav_url}) failed: {e}")
        else:
            raise HTTPException(400, f"Unknown input type: {etype}")
        return {"success": True, "url": page.url}
    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/{name}/navigate")
async def navigate_session(name: str, body: dict = {}):
    """Navigate the session's browser to a URL."""
    session = _active_sessions.get(name)
    if not session:
        raise HTTPException(404, "Session not found")
    url = body.get("url", "about:blank")
    try:
        await session["page"].goto(url, wait_until="domcontentloaded")
        return {"success": True, "url": session["page"].url}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/login")
async def login(req: LoginRequest):
    """Open a real desktop Chrome window for the user to log in manually.

    Uses proxy + playwright-stealth for anti-detection. After the user finishes
    logging in, the frontend calls /confirm-login to save cookies.
    """
    if req.session_name in _active_sessions:
        return {"success": True, "message": "Session already active"}

    try:
        pw = await async_playwright().start()
        profile_dir = Path(settings.DATA_DIR).resolve() / "browser_profiles" / req.session_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Build proxy config
        proxy_url = await get_proxy_url()
        launch_opts: dict = {
            "channel": "chrome",
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        }
        if proxy_url:
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            proxy_conf = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_conf["username"] = parsed.username
            if parsed.password:
                proxy_conf["password"] = parsed.password
            launch_opts["proxy"] = proxy_conf

        # Headed Chrome so the user can log in on their desktop
        context = await pw.chromium.launch_persistent_context(
            str(profile_dir), **launch_opts
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # Full anti-detection via playwright-stealth (same as crawl4ai)
        from playwright_stealth import Stealth
        await Stealth().apply_stealth_async(page)

        try:
            await page.goto(req.url, wait_until="load", timeout=30000)
        except Exception as e:
            import logging
            logging.getLogger("sessions").warning(f"login page.goto({req.url}): {e}")

        _active_sessions[req.session_name] = {
            "playwright": pw,
            "context": context,
            "page": page,
            "ws_clients": set(),
            "streaming": False,
            "stream_task": None,
        }
        return {"success": True, "message": "Browser opened for login"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.post("/import-cookies")
async def import_cookies(body: dict):
    """Import cookies from text (name=value; name2=value2 format).

    Body: `{name: str, url: str, cookie_text: str}`.
    Parses the cookie text, resolves domain from URL, and saves as a session.
    """
    name = (body or {}).get("name", "").strip()
    url = (body or {}).get("url", "").strip()
    cookie_text = (body or {}).get("cookie_text", "").strip()

    if not name or not url or not cookie_text:
        return {"success": False, "error": "name, url, cookie_text are required"}

    name = _sanitize_name(name)

    from urllib.parse import urlparse
    parsed = urlparse(url)
    domain = parsed.hostname or ""

    # Parse cookie text: "name1=value1; name2=value2" or one per line
    cookies = []
    # Split by newlines first, then by semicolons
    parts = []
    for line in cookie_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for part in line.split(";"):
            part = part.strip()
            if part:
                parts.append(part)

    for part in parts:
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        if not k:
            continue
        cookies.append({
            "name": k,
            "value": v,
            "domain": domain,
            "path": "/",
        })

    if not cookies:
        return {"success": False, "error": "No valid cookies found in input"}

    await STORE.save_session(name, url=url, cookies=cookies)
    return {"success": True, "cookie_count": len(cookies), "session_name": name}


@router.post("/{name}/confirm-login")
async def confirm_login(name: str):
    session = _active_sessions.get(name)

    if not session or len(session["context"].pages) == 0:
        try:
            pw = await async_playwright().start()
            profile_dir = Path(settings.DATA_DIR).resolve() / "browser_profiles" / name
            context = await pw.chromium.launch_persistent_context(
                str(profile_dir),
                headless=True,
                viewport={"width": 1280, "height": 800},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            session = {"playwright": pw, "context": context, "page": page, "ws_clients": set()}
            _active_sessions[name] = session
        except Exception as e:
            raise HTTPException(500, f"Failed to re-open browser: {e}")

    session["streaming"] = False
    for ws in session.get("ws_clients", set()):
        try:
            await ws.send_json({"type": "confirmed"})
        except Exception:
            pass

    cookies = []
    url = ""
    try:
        cookies = await session["context"].cookies()
        url = session["page"].url
    except Exception:
        pass

    await STORE.save_session(name, url=url, cookies=cookies)

    try:
        await session["context"].close()
    except Exception:
        pass
    try:
        await session["playwright"].stop()
    except Exception:
        pass
    _active_sessions.pop(name, None)

    return {"success": True, "cookie_count": len(cookies), "url": url}


@router.delete("/{name}")
async def delete_session(name: str):
    session = _active_sessions.get(name)
    if session:
        session["streaming"] = False
        try:
            await session["context"].close()
        except Exception:
            pass
        try:
            await session["playwright"].stop()
        except Exception:
            pass
        _active_sessions.pop(name, None)

    ok = await STORE.delete_session(name)
    if not ok:
        raise HTTPException(404, "Not found")
    return {"success": True}


async def _stream_screenshots(name: str):
    session = _active_sessions.get(name)
    if not session:
        return

    while session["streaming"] and session.get("ws_clients"):
        try:
            buf = await session["page"].screenshot(type="jpeg", quality=55)
            import base64
            b64 = base64.b64encode(buf).decode()
            msg = {"type": "frame", "data": b64}

            dead = set()
            for ws in session["ws_clients"]:
                try:
                    await ws.send_json(msg)
                except Exception:
                    dead.add(ws)
            session["ws_clients"] -= dead
        except Exception:
            pass
        await asyncio.sleep(0.2)

    session["streaming"] = False


async def session_stream_ws(websocket: WebSocket, session_name: str):
    await websocket.accept()
    session = _active_sessions.get(session_name)
    if not session:
        await websocket.send_json({"type": "error", "message": "No active session"})
        await websocket.close()
        return

    session["ws_clients"].add(websocket)
    if not session.get("streaming"):
        session["streaming"] = True
        session["stream_task"] = asyncio.ensure_future(_stream_screenshots(session_name))

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        session["ws_clients"].discard(websocket)
