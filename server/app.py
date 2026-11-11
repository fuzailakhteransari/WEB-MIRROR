"""FastAPI signaling, host directory, and static web portal server."""

from __future__ import annotations

import hmac
import json
import struct
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import qrcode

from server.config import ServerConfig
from server.state import SignalingState


class RenameHostBody(BaseModel):
    name: str


class AuthCheckBody(BaseModel):
    token: str = ""


def _token_matches(expected: str, provided: str) -> bool:
    if not expected:
        return True
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


def create_app(config: ServerConfig | None = None) -> FastAPI:
    cfg = config or ServerConfig.from_env()
    state = SignalingState(
        support_code_ttl_seconds=cfg.support_code_ttl_seconds,
        registry_path=cfg.registry_path,
    )

    app = FastAPI(title="WebDesk Remote Signaling Server", version="1.0.0")
    app.state.config = cfg
    app.state.signaling = state

    @app.middleware("http")
    async def no_cache_static_assets(request: Any, call_next: Any) -> Response:
        response: Response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    def require_http_token(
        authorization: str | None = Header(default=None),
        token: str | None = Query(default=None),
    ) -> None:
        if not cfg.workspace_token:
            return
        bearer = ""
        if authorization and authorization.lower().startswith("bearer "):
            bearer = authorization[7:].strip()
        candidate = bearer or (token or "").strip()
        if not _token_matches(cfg.workspace_token, candidate):
            raise HTTPException(status_code=401, detail="Invalid workspace token")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "online_hosts": sum(
                1 for h in state.hosts.values() if h.websocket is not None
            ),
            "active_sessions": len(state.sessions),
        }

    @app.get("/api/auth/status")
    async def auth_status() -> dict[str, Any]:
        return {"workspace_token_required": bool(cfg.workspace_token)}

    @app.post("/api/auth/session")
    async def auth_session(body: AuthCheckBody) -> JSONResponse:
        if not _token_matches(cfg.workspace_token, body.token.strip()):
            raise HTTPException(status_code=401, detail="Invalid workspace token")
        return JSONResponse({"ok": True})

    @app.get("/api/ice-config")
    async def ice_config(_: None = Depends(require_http_token)) -> dict[str, Any]:
        return {"iceServers": cfg.ice_servers}

    @app.get("/api/tunnel-url")
    async def tunnel_url(_: None = Depends(require_http_token)) -> dict[str, Any]:
        url_file = cfg.state_dir / "tunnel_url.txt"
        if url_file.exists():
            url = url_file.read_text(encoding="utf-8").strip()
            if url.startswith("https://"):
                return {"url": url}
        return {"url": None}

    @app.get("/api/qr")
    async def qr_svg(
        url: str = Query(..., min_length=1, max_length=512),
        _: None = Depends(require_http_token),
    ) -> Response:
        qr = qrcode.QRCode(border=2, box_size=1)
        qr.add_data(url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = len(matrix)
        rects = [
            f'<rect x="{x}" y="{y}" width="1" height="1" fill="#0b101b"/>'
            for y, row in enumerate(matrix)
            for x, val in enumerate(row)
            if val
        ]
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'shape-rendering="crispEdges"><rect width="{size}" height="{size}" fill="#ffffff"/>'
            + "".join(rects)
            + "</svg>"
        )
        return Response(content=svg, media_type="image/svg+xml")

    @app.get("/api/hosts")
    async def list_hosts(_: None = Depends(require_http_token)) -> dict[str, Any]:
        hosts = [h.model_dump() for h in state.list_me2me_hosts()]
        return {"hosts": hosts}

    @app.patch("/api/hosts/{host_id}")
    async def rename_host(
        host_id: str,
        body: RenameHostBody,
        _: None = Depends(require_http_token),
    ) -> dict[str, Any]:
        updated = state.rename_host(host_id, body.name)
        if not updated:
            raise HTTPException(status_code=404, detail="Host not found")
        return {"host": updated.model_dump()}

    @app.delete("/api/hosts/{host_id}")
    async def delete_host(
        host_id: str,
        _: None = Depends(require_http_token),
    ) -> dict[str, Any]:
        removed = state.remove_host(host_id)
        if not removed:
            raise HTTPException(status_code=404, detail="Host not found")
        return {"removed": True, "host_id": host_id}

    @app.websocket("/ws/host")
    async def ws_host(ws: WebSocket, token: str = Query(default="")) -> None:
        if not _token_matches(cfg.workspace_token, token.strip()):
            await ws.close(code=4401, reason="Unauthorized")
            return
        await ws.accept()
        registered_host_id: str | None = None
        try:
            while True:
                raw_msg = await ws.receive()
                if raw_msg.get("type") == "websocket.disconnect":
                    break
                if raw_msg.get("bytes") is not None:
                    blob: bytes = raw_msg["bytes"]
                    if len(blob) < 3:
                        continue
                    (sid_len,) = struct.unpack(">H", blob[:2])
                    if len(blob) < 2 + sid_len:
                        continue
                    sid = blob[2 : 2 + sid_len].decode("utf-8", errors="ignore")
                    session = state.sessions.get(sid)
                    if session and session.host_ws is ws:
                        await session.viewer_ws.send_bytes(blob[2 + sid_len :])
                    continue

                text = raw_msg.get("text")
                if not text:
                    continue
                msg = json.loads(text)
                msg_type = msg.get("type")
                payload = msg.get("payload") or {}

                if msg_type == "register_host":
                    host_id = str(payload.get("host_id") or msg.get("host_id") or "").strip()
                    if not host_id:
                        await ws.send_json({"type": "error", "payload": {"message": "Missing host_id"}})
                        continue
                    registered_host_id = host_id
                    entry = state.register_host(
                        host_id=host_id,
                        name=str(payload.get("name") or host_id),
                        platform=str(payload.get("platform") or "windows"),
                        mode=str(payload.get("mode") or "me2me"),
                        monitors=list(payload.get("monitors") or []),
                        capabilities=list(payload.get("capabilities") or []),
                        ws=ws,
                    )
                    await ws.send_json(
                        {
                            "type": "host_registered",
                            "host_id": entry.host_id,
                            "payload": {"status": entry.status, "iceServers": cfg.ice_servers},
                        }
                    )

                elif msg_type == "heartbeat" and registered_host_id:
                    state.update_host_heartbeat(
                        registered_host_id,
                        monitors=payload.get("monitors"),
                    )
                    await ws.send_json({"type": "heartbeat_ack", "payload": {}})

                elif msg_type == "create_support_code" and registered_host_id:
                    code_entry = state.create_support_code(registered_host_id)
                    if not code_entry:
                        await ws.send_json(
                            {"type": "error", "payload": {"message": "Host not registered"}}
                        )
                    else:
                        await ws.send_json(
                            {
                                "type": "support_code_created",
                                "host_id": registered_host_id,
                                "payload": {
                                    "code": code_entry.code,
                                    "formatted": state.format_code(code_entry.code),
                                    "expires_at": code_entry.expires_at,
                                },
                            }
                        )

                elif msg_type in {
                    "sdp_answer",
                    "pake_challenge",
                    "pake_server_proof",
                    "pairing_challenge",
                    "pairing_accepted",
                    "ice_candidate",
                    "ws_fallback_ready",
                    "ws_channel",
                    "session_reject",
                    "session_end",
                }:
                    session_id = msg.get("session_id")
                    session = state.sessions.get(session_id or "")
                    if not session or session.host_ws is not ws:
                        continue
                    await session.viewer_ws.send_json(msg)
                    if msg_type in {"session_reject", "session_end"}:
                        state.end_session(session.session_id)

        except WebSocketDisconnect:
            pass
        finally:
            _, orphaned = state.disconnect_host_ws(ws)
            for sess in orphaned:
                try:
                    await sess.viewer_ws.send_json(
                        {
                            "type": "session_end",
                            "session_id": sess.session_id,
                            "payload": {"reason": "Host disconnected"},
                        }
                    )
                except Exception:
                    pass

    @app.websocket("/ws/client")
    async def ws_client(ws: WebSocket, token: str = Query(default="")) -> None:
        if not _token_matches(cfg.workspace_token, token.strip()):
            await ws.close(code=4401, reason="Unauthorized")
            return
        await ws.accept()
        try:
            while True:
                msg = await ws.receive_json()
                msg_type = msg.get("type")
                payload = msg.get("payload") or {}

                if msg_type == "connect_host":
                    host_id = str(msg.get("host_id") or payload.get("host_id") or "").strip()
                    session, err = state.create_session(host_id, "me2me", ws)
                    if err or not session:
                        await ws.send_json(
                            {
                                "type": "session_reject",
                                "host_id": host_id,
                                "payload": {"reason": err or "Unable to connect"},
                            }
                        )
                        continue
                    host = state.hosts[host_id]
                    await ws.send_json(
                        {
                            "type": "session_created",
                            "session_id": session.session_id,
                            "host_id": host_id,
                            "payload": {
                                "host_name": host.name,
                                "mode": host.mode,
                                "monitors": [m.model_dump() for m in host.monitors],
                            },
                        }
                    )
                    await session.host_ws.send_json(
                        {
                            "type": "connect_request",
                            "session_id": session.session_id,
                            "host_id": host_id,
                            "payload": {
                                "mode": "me2me",
                                "sdp_offer": payload.get("sdp_offer", ""),
                                "pairing_id": payload.get("pairing_id"),
                                "control_only": bool(payload.get("control_only", False)),
                                "viewer_label": payload.get("viewer_label", "Web Viewer"),
                            },
                        }
                    )

                elif msg_type == "connect_support":
                    raw_code = str(payload.get("code") or "")
                    host_id, err = state.consume_support_code(raw_code)
                    if err or not host_id:
                        await ws.send_json(
                            {
                                "type": "session_reject",
                                "payload": {"reason": err or "Invalid support code"},
                            }
                        )
                        continue
                    session, sess_err = state.create_session(host_id, "it2me", ws)
                    if sess_err or not session:
                        await ws.send_json(
                            {
                                "type": "session_reject",
                                "host_id": host_id,
                                "payload": {"reason": sess_err or "Host unavailable"},
                            }
                        )
                        continue
                    host = state.hosts[host_id]
                    await ws.send_json(
                        {
                            "type": "session_created",
                            "session_id": session.session_id,
                            "host_id": host_id,
                            "payload": {
                                "host_name": host.name,
                                "mode": "it2me",
                                "monitors": [m.model_dump() for m in host.monitors],
                            },
                        }
                    )
                    await session.host_ws.send_json(
                        {
                            "type": "connect_request",
                            "session_id": session.session_id,
                            "host_id": host_id,
                            "payload": {
                                "mode": "it2me",
                                "sdp_offer": payload.get("sdp_offer", ""),
                                "viewer_label": payload.get("viewer_label", "Support Helper"),
                            },
                        }
                    )

                elif msg_type in {
                    "pake_client_proof",
                    "pairing_client_proof",
                    "ice_candidate",
                    "enable_ws_fallback",
                    "ws_channel",
                    "ws_frame_ack",
                    "session_end",
                }:
                    session_id = msg.get("session_id")
                    session = state.sessions.get(session_id or "")
                    if not session or session.viewer_ws is not ws:
                        continue
                    await session.host_ws.send_json(msg)
                    if msg_type == "session_end":
                        state.end_session(session.session_id)

        except WebSocketDisconnect:
            pass
        finally:
            ended = state.disconnect_viewer_ws(ws)
            for sess in ended:
                try:
                    await sess.host_ws.send_json(
                        {
                            "type": "session_end",
                            "session_id": sess.session_id,
                            "payload": {"reason": "Viewer disconnected"},
                        }
                    )
                except Exception:
                    pass

    # Serve static web portal if present
    if cfg.web_dir.exists():
        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(cfg.web_dir / "index.html")

        app.mount("/", StaticFiles(directory=str(cfg.web_dir), html=True), name="web")

    return app


app = create_app()
