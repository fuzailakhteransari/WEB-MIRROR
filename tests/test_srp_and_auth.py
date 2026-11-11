"""Unit and cross-language (Python <-> Browser JS) tests for SRP-6a, DTLS binding, and pairing."""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

import pytest

from host.config_store import (
    PinRateLimiter,
    create_pairing_entry,
    get_pairing_auth_key,
    initialize_host_config,
)
from shared.srp import (
    SRPAuthError,
    SRPClientSession,
    SRPServerSession,
    build_dtls_context,
    compute_pairing_hmac,
    create_verifier,
    extract_dtls_fingerprint,
    hash_pairing_secret,
    verify_pairing_hmac,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_srp_roundtrip_and_wrong_pin() -> None:
    identity = "host-uuid-1234"
    pin = "654321"
    salt_hex, verifier_hex = create_verifier(identity, pin)
    ctx = build_dtls_context("sha-256 AA:BB:CC", "sha-256 11:22:33")

    srv = SRPServerSession.start(identity, salt_hex, verifier_hex, dtls_context=ctx)
    cli = SRPClientSession.start(identity, pin, dtls_context=ctx)

    m1 = cli.process_challenge(srv.salt_hex, srv.B_hex)
    m2 = srv.verify_client_proof(cli.A_hex, m1)
    assert cli.verify_server_proof(m2) is True
    assert srv.session_key == cli.session_key

    # Wrong PIN must fail verification
    bad_srv = SRPServerSession.start(identity, salt_hex, verifier_hex, dtls_context=ctx)
    bad_cli = SRPClientSession.start(identity, "000000", dtls_context=ctx)
    bad_m1 = bad_cli.process_challenge(bad_srv.salt_hex, bad_srv.B_hex)
    with pytest.raises(SRPAuthError):
        bad_srv.verify_client_proof(bad_cli.A_hex, bad_m1)


def test_dtls_fingerprint_tamper_detection() -> None:
    sdp = "v=0\r\na=fingerprint:sha-256 AA:BB:CC:DD\r\na=setup:actpass\r\n"
    assert extract_dtls_fingerprint(sdp) == "sha-256 aa:bb:cc:dd"

    identity = "host-dtls-test"
    pin = "123456"
    salt_hex, verifier_hex = create_verifier(identity, pin)

    # Suppose MITM swapped viewer's DTLS fingerprint on the signaling server
    srv_ctx = build_dtls_context("sha-256 01:02", "sha-256 EE:FF")
    cli_ctx = build_dtls_context("sha-256 01:02", "sha-256 99:88")

    srv = SRPServerSession.start(identity, salt_hex, verifier_hex, dtls_context=srv_ctx)
    cli = SRPClientSession.start(identity, pin, dtls_context=cli_ctx)
    m1 = cli.process_challenge(srv.salt_hex, srv.B_hex)

    with pytest.raises(SRPAuthError):
        srv.verify_client_proof(cli.A_hex, m1)


def test_js_srp_interoperates_with_python_server() -> None:
    """Verify web/js/srp.js in Node.js matches shared/srp.py byte-for-byte."""
    identity = "js-interop-host"
    pin = "987654"
    salt_hex, verifier_hex = create_verifier(identity, pin)
    host_fp = "sha-256 AA:11:BB:22"
    viewer_fp = "sha-256 CC:33:DD:44"
    ctx = build_dtls_context(host_fp, viewer_fp)

    srv = SRPServerSession.start(identity, salt_hex, verifier_hex, dtls_context=ctx)

    pairing_secret = secrets.token_bytes(32).hex()
    auth_key = hash_pairing_secret(pairing_secret)
    nonce = secrets.token_hex(16)

    srp_js_url = (PROJECT_ROOT / "web" / "js" / "srp.js").as_uri()
    js_script = f"""
    import {{ SRPClient, buildDtlsContext, computePairingProof }} from "{srp_js_url}";
    const ctx = buildDtlsContext({json.dumps(host_fp)}, {json.dumps(viewer_fp)});
    const cli = new SRPClient({json.dumps(identity)}, {json.dumps(pin)}, ctx);
    const m1 = await cli.processChallenge({json.dumps(srv.salt_hex)}, {json.dumps(srv.B_hex)});
    const pairingProof = await computePairingProof({json.dumps(pairing_secret)}, {json.dumps(nonce)}, ctx);
    console.log(JSON.stringify({{ A: cli.AHex, M1: m1, pairingProof }}));
    """
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", js_script],
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(proc.stdout.strip())

    # Server verifies JS client's M1 and generates M2
    m2 = srv.verify_client_proof(out["A"], out["M1"])
    assert verify_pairing_hmac(auth_key, out["pairingProof"], nonce, ctx) is True
    assert verify_pairing_hmac(auth_key, out["pairingProof"], nonce, "tampered") is False

    # Also verify Python helper matches JS pairing proof
    py_proof = compute_pairing_hmac(auth_key, nonce, ctx)
    assert py_proof == out["pairingProof"]
    assert len(m2) == 64


def test_pairing_registry_and_rate_limiter(tmp_path: Path) -> None:
    cfg_path = tmp_path / "host_config.json"
    cfg = initialize_host_config("TestPC", "123456", path=cfg_path)
    pid, raw_secret = create_pairing_entry(cfg, label="Chrome", path=cfg_path)

    auth_key = get_pairing_auth_key(cfg, pid)
    assert auth_key is not None
    assert auth_key != raw_secret

    limiter = PinRateLimiter(max_failures=3, base_lock_seconds=10.0)
    assert limiter.is_locked(now=100.0) == (False, 0.0)
    limiter.record_failure(now=100.0)
    limiter.record_failure(now=101.0)
    assert limiter.is_locked(now=101.0) == (False, 0.0)
    limiter.record_failure(now=102.0)
    locked, rem = limiter.is_locked(now=105.0)
    assert locked is True
    assert rem == pytest.approx(7.0)
    limiter.record_success()
    assert limiter.is_locked(now=105.0) == (False, 0.0)
