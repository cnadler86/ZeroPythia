"""FastAPI dashboard server.

HTTP + WebSocket endpoints:
  GET  /               – HTML dashboard GUI
  GET  /api/state      – current DashboardState as JSON
  GET  /api/regulators – list all registered regulators
  POST /api/mode       – set operating mode
  POST /api/regulators/select        – select active regulator
  POST /api/regulators/{name}/settings – update regulator settings
  WS   /ws             – live state stream (DashboardState JSON, ~1 s)

The server receives a ``ControlRuntime`` instance from the entry point and
registers/removes WebSocket callbacks on connect/disconnect.

Language support
----------------
``create_app`` accepts a ``lang`` parameter (default ``"en"``).  Translations
are looked up in ``i18n.TRANSLATIONS``; unknown codes fall back to English.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import tomllib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from ZeroPythia.runtime.control_runtime import ControlRuntime
from ZeroPythia.runtime.models import DashboardState

from .i18n import TRANSLATIONS, build_js_t
from .models import (
    AutoConnectCommand,
    SelectRegulatorCommand,
    SetModeCommand,
)

logger = logging.getLogger(__name__)


# ── Project metadata ──────────────────────────────────────────────────────────


def _read_project_info() -> tuple[str, str]:
    """Return (version, github_url) from pyproject.toml, or safe defaults."""
    try:
        p = Path(__file__).parent.parent.parent / "pyproject.toml"
        with open(p, "rb") as f:
            data = tomllib.load(f)
        version = data.get("project", {}).get("version", "dev")
        urls = data.get("project", {}).get("urls", {})
        github = urls.get("Repository", urls.get("Homepage", ""))
        return version, github
    except Exception:
        return "dev", ""


_PROJECT_VERSION, _GITHUB_URL = _read_project_info()


# ── HTML template (external file, no build step) ──────────────────────────────
# Placeholders use <<<key>>> syntax to avoid conflicts with CSS/JS syntax.

_TEMPLATE_PATH = Path(__file__).with_name("templates").joinpath("dashboard.html")


def _load_html_template() -> str:
    """Load the dashboard HTML template from disk."""
    try:
        return _TEMPLATE_PATH.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to load dashboard template: %s", _TEMPLATE_PATH)
        return "<!DOCTYPE html><html><body><h1>Dashboard template missing</h1></body></html>"


_HTML_TEMPLATE = _load_html_template()


def _build_html(t: dict[str, str], version: str, github_url: str) -> str:
    """Render the HTML template with the given translation dict and metadata."""
    if github_url:
        safe_url = _html.escape(github_url, quote=True)
        github_link = f'<a href="{safe_url}" target="_blank" rel="noopener">{t["t_github"]}</a>'
    else:
        github_link = ""

    result = _HTML_TEMPLATE
    # Static metadata
    result = result.replace("<<<VERSION>>>", _html.escape(version))
    result = result.replace("<<<GITHUB_LINK>>>", github_link)
    result = result.replace("<<<T_JSON>>>", build_js_t(t))
    result = result.replace("<<<lang>>>", t.get("lang", "en"))
    # Translation keys
    for key, val in t.items():
        result = result.replace(f"<<<{key}>>>", val)
    return result


# ── FastAPI app factory ───────────────────────────────────────────────────────


def create_app(runtime: ControlRuntime, *, lang: str = "en") -> FastAPI:
    """Create and return the FastAPI application bound to a ControlRuntime.

    Parameters
    ----------
    runtime:
        The ``ControlRuntime`` instance that drives sampling and control.
    lang:
        Dashboard UI language code (``"en"`` or ``"de"``).
        Falls back to English for unknown codes.
    """
    t = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    _html_page = _build_html(t, _PROJECT_VERSION, _GITHUB_URL)
    app = FastAPI(title="ZeroPythia", docs_url=None, redoc_url=None)

    # ── HTML ──────────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root() -> HTMLResponse:
        return HTMLResponse(_html_page)

    # ── PWA assets ────────────────────────────────────────────────────────────

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {
                "name": "Zero-Feed Controller",
                "short_name": "ZeroFeed",
                "description": "Three-phase zero-feed-in battery controller dashboard",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#0f1117",
                "theme_color": "#0f1117",
                "icons": [
                    {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
                ],
            },
            headers={"Content-Type": "application/manifest+json"},
        )

    @app.get("/icon.svg", include_in_schema=False)
    async def app_icon():
        from fastapi.responses import Response

        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            '<rect width="512" height="512" rx="80" fill="#0f1117"/>'
            '<rect x="80" y="155" width="300" height="190" rx="24"'
            ' fill="none" stroke="#4f8ef7" stroke-width="20"/>'
            '<rect x="380" y="200" width="52" height="100" rx="16" fill="#4f8ef7"/>'
            '<rect x="100" y="175" width="220" height="150" rx="14"'
            ' fill="#22c55e" opacity="0.85"/>'
            '<path d="M236 185 L200 268 H244 L220 345 L292 258 H248 Z" fill="#0f1117"/>'
            "</svg>"
        )
        return Response(content=svg, media_type="image/svg+xml")

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        from fastapi.responses import Response

        sw = """
// Zero-Feed Controller – minimal service worker for PWA installability.
// Strategy: network-first for navigation, fallback to cache on offline.
const CACHE = 'zfc-v1';

self.addEventListener('install', evt => {
  evt.waitUntil(caches.open(CACHE).then(c => c.add('/')));
  self.skipWaiting();
});

self.addEventListener('activate', evt => {
  evt.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', evt => {
  if (evt.request.mode === 'navigate') {
    evt.respondWith(
      fetch(evt.request).catch(() => caches.match('/'))
    );
  }
  // API calls and WebSocket connections pass through without caching.
});
"""
        return Response(
            content=sw,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"},
        )

    # ── REST API ──────────────────────────────────────────────────────────────

    @app.get("/api/state")
    async def get_state() -> DashboardState:
        return runtime.get_state()

    @app.get("/api/regulators")
    async def list_regulators():
        return runtime.list_regulators()

    @app.post("/api/mode")
    async def set_mode(cmd: SetModeCommand) -> dict[str, str]:
        await runtime.set_mode(
            cmd.mode,
            charge_power_w=cmd.charge_power_w,
            max_discharge_w=cmd.max_discharge_w,
        )
        return {"status": "ok"}

    @app.post("/api/regulators/select")
    async def select_regulator(cmd: SelectRegulatorCommand) -> dict[str, str]:
        try:
            await runtime.set_active_regulator(cmd.name)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"status": "ok", "active": cmd.name}

    @app.post("/api/regulators/{name}/settings")
    async def update_settings(name: str, settings: dict[str, Any]) -> dict[str, str]:
        try:
            await runtime.update_regulator_settings(name, settings)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"status": "ok"}

    @app.post("/api/auto/connect")
    async def auto_connect(cmd: AutoConnectCommand) -> dict[str, str]:
        try:
            await runtime.enable_auto_mode(
                mqtt_broker=cmd.mqtt_broker,
                device_id=cmd.device_id,
                topic_prefix=cmd.topic_prefix,
                status_interval_s=cmd.status_interval_s,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e)) from e
        return {"status": "ok", "device_id": cmd.device_id}

    @app.post("/api/auto/disconnect")
    async def auto_disconnect() -> dict[str, str]:
        await runtime.disable_auto_mode()
        return {"status": "ok"}

    # ── WebSocket ─────────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        queue: asyncio.Queue[DashboardState] = asyncio.Queue(maxsize=5)

        async def push(state: DashboardState) -> None:
            try:
                queue.put_nowait(state)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(state)
                except asyncio.QueueEmpty:
                    pass

        runtime.add_state_callback(push)
        try:
            # Send current state immediately on connect
            await ws.send_text(runtime.get_state().model_dump_json())

            while True:
                state = await asyncio.wait_for(queue.get(), timeout=5.0)
                await ws.send_text(state.model_dump_json())
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except Exception:
            logger.debug("WebSocket error", exc_info=True)
        finally:
            runtime.remove_state_callback(push)

    return app
