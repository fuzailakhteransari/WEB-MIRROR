"""Tests for FastAPI signaling, host directory, workspace auth, and IT2Me single-use codes."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import create_app
from server.config import ServerConfig


def test_workspace_token_auth_and_directory(tmp_path: Path) -> None:
    cfg = ServerConfig(
        workspace_token="super-secret-token",
        registry_path=tmp_path / "registry.json",
    )
    app = create_app(cfg)
    client = TestClient(app)

    assert client.get("/api/auth/status").json()["workspace_token_required"] is True
    assert client.get("/api/hosts").status_code == 401
    assert client.post("/api/auth/session", json={"token": "wrong"}).status_code == 401
    assert client.post("/api/auth/session", json={"token": "super-secret-token"}).status_code == 200

    headers = {"Authorization": "Bearer super-secret-token"}
    assert client.get("/api/hosts", headers=headers).json()["hosts"] == []

    # Connect Me2Me host via WS
    with client.websocket_connect("/ws/host?token=super-secret-token") as host_ws:
        host_ws.send_json(
            {
                "type": "register_host",
                "payload": {
                    "host_id": "h1",
                    "name": "Office-PC",
                    "platform": "windows",
                    "mode": "me2me",
                    "monitors": [{"index": 1, "name": "Disp 1", "left": 0, "top": 0, "width": 1920, "height": 1080, "is_primary": True}],
                },
            }
        )
        reg = host_ws.receive_json()
        assert reg["type"] == "host_registered"

        hosts = client.get("/api/hosts", headers=headers).json()["hosts"]
        assert len(hosts) == 1
        assert hosts[0]["name"] == "Office-PC"
        assert hosts[0]["status"] == "online"

        # Rename works
        r = client.patch("/api/hosts/h1", json={"name": "Lab-PC"}, headers=headers)
        assert r.status_code == 200
        assert r.json()["host"]["name"] == "Lab-PC"

    # After host disconnects, status becomes offline and DELETE removes it
    hosts_after = client.get("/api/hosts", headers=headers).json()["hosts"]
    assert hosts_after[0]["status"] == "offline"
    assert client.delete("/api/hosts/h1", headers=headers).status_code == 200
    assert client.get("/api/hosts", headers=headers).json()["hosts"] == []


def test_it2me_support_code_single_use_and_mode_isolation() -> None:
    cfg = ServerConfig(support_code_ttl_seconds=60)
    app = create_app(cfg)
    client = TestClient(app)

    with client.websocket_connect("/ws/host") as host_ws:
        host_ws.send_json(
            {
                "type": "register_host",
                "payload": {
                    "host_id": "it2me-abc",
                    "name": "Support-PC",
                    "mode": "it2me",
                    "monitors": [],
                },
            }
        )
        assert host_ws.receive_json()["type"] == "host_registered"

        # IT2Me host must NOT appear in Me2Me directory
        assert client.get("/api/hosts").json()["hosts"] == []

        host_ws.send_json({"type": "create_support_code", "payload": {}})
        code_msg = host_ws.receive_json()
        assert code_msg["type"] == "support_code_created"
        formatted = code_msg["payload"]["formatted"]
        assert len(code_msg["payload"]["code"]) == 12

        # Viewer cannot bypass support code via connect_host
        with client.websocket_connect("/ws/client") as bad_viewer:
            bad_viewer.send_json(
                {
                    "type": "connect_host",
                    "host_id": "it2me-abc",
                    "payload": {"sdp_offer": "v=0"},
                }
            )
            rej = bad_viewer.receive_json()
            assert rej["type"] == "session_reject"

        # First viewer redeems support code successfully
        with client.websocket_connect("/ws/client") as v1:
            v1.send_json(
                {
                    "type": "connect_support",
                    "payload": {"code": formatted, "sdp_offer": "v=0"},
                }
            )
            created = v1.receive_json()
            assert created["type"] == "session_created"
            req = host_ws.receive_json()
            assert req["type"] == "connect_request"
            assert req["payload"]["mode"] == "it2me"

            # Second viewer cannot reuse same code
            with client.websocket_connect("/ws/client") as v2:
                v2.send_json(
                    {
                        "type": "connect_support",
                        "payload": {"code": formatted, "sdp_offer": "v=0"},
                    }
                )
                rej2 = v2.receive_json()
                assert rej2["type"] == "session_reject"


def test_it2me_code_expiration() -> None:
    cfg = ServerConfig(support_code_ttl_seconds=1)
    app = create_app(cfg)
    client = TestClient(app)

    with client.websocket_connect("/ws/host") as host_ws:
        host_ws.send_json({"type": "register_host", "payload": {"host_id": "it2me-exp", "name": "Exp", "mode": "it2me"}})
        host_ws.receive_json()
        host_ws.send_json({"type": "create_support_code", "payload": {}})
        code = host_ws.receive_json()["payload"]["code"]

        time.sleep(1.15)
        with client.websocket_connect("/ws/client") as viewer_ws:
            viewer_ws.send_json({"type": "connect_support", "payload": {"code": code, "sdp_offer": "v=0"}})
            rej = viewer_ws.receive_json()
            assert rej["type"] == "session_reject"
            assert "expired" in rej["payload"]["reason"].lower()


def test_ice_servers_file_and_tunnel_url_endpoint(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ice_servers.json").write_text(
        '[{"urls": ["turn:example.org:3478"], "username": "u", "credential": "p"}]',
        encoding="utf-8",
    )
    (tmp_path / "tunnel_url.txt").write_text(
        "https://demo-tunnel.trycloudflare.com\n", encoding="utf-8"
    )
    monkeypatch.setenv("WEBDESK_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("WEBDESK_ICE_SERVERS_JSON", raising=False)

    cfg = ServerConfig.from_env()
    client = TestClient(create_app(cfg))
    ice = client.get("/api/ice-config").json()["iceServers"]
    assert ice[0]["urls"] == ["turn:example.org:3478"]
    assert client.get("/api/tunnel-url").json()["url"] == "https://demo-tunnel.trycloudflare.com"

    qr_resp = client.get("/api/qr", params={"url": "https://demo-tunnel.trycloudflare.com"})
    assert qr_resp.status_code == 200
    assert qr_resp.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in qr_resp.text and "<rect" in qr_resp.text


def test_me2me_authenticated_reconnect_replaces_stale_busy_session() -> None:
    app = create_app(ServerConfig())
    client = TestClient(app)

    with client.websocket_connect("/ws/host") as host_ws:
        host_ws.send_json(
            {
                "type": "register_host",
                "payload": {"host_id": "m1", "name": "Home-PC", "mode": "me2me", "monitors": []},
            }
        )
        assert host_ws.receive_json()["type"] == "host_registered"

        with client.websocket_connect("/ws/client") as v1:
            v1.send_json({"type": "connect_host", "host_id": "m1", "payload": {"sdp_offer": "v=0"}})
            s1 = v1.receive_json()
            assert s1["type"] == "session_created"
            req1 = host_ws.receive_json()
            assert req1["type"] == "connect_request"
            sid1 = s1["session_id"]

            # Host is marked busy while v1 is active
            hosts_busy = client.get("/api/hosts").json()["hosts"]
            assert hosts_busy[0]["status"] == "busy"

            # Owner reconnects from another tab/phone while old socket is still half-open
            with client.websocket_connect("/ws/client") as v2:
                v2.send_json({"type": "connect_host", "host_id": "m1", "payload": {"sdp_offer": "v=0"}})
                s2 = v2.receive_json()
                assert s2["type"] == "session_created"
                req2 = host_ws.receive_json()
                assert req2["type"] == "connect_request"
                sid2 = s2["session_id"]

                # Before PIN/pairing proof completes, v1 stays protected
                hosts_still_busy = client.get("/api/hosts").json()["hosts"]
                assert hosts_still_busy[0]["status"] == "busy"

                # Once host confirms PIN/pairing proof for v2, stale v1 is cleanly replaced
                host_ws.send_json(
                    {"type": "pake_server_proof", "session_id": sid2, "payload": {"M2": "ok"}}
                )
                proof = v2.receive_json()
                assert proof["type"] == "pake_server_proof"
                ended1 = v1.receive_json()
                assert ended1["type"] == "session_end"
                assert ended1["session_id"] == sid1

        # After v2 exits cleanly, host immediately returns online
        hosts_online = client.get("/api/hosts").json()["hosts"]
        assert hosts_online[0]["status"] == "online"
