"""RFC 5054 2048-bit SRP-6a PAKE with DTLS fingerprint binding and HMAC pairing."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

# RFC 5054 Appendix A.3 2048-bit safe prime (MODP Group 14)
SRP_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
    "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF"
)
SRP_N = int(SRP_N_HEX, 16)
SRP_G = 2
PAD_LEN = 256  # 2048 bits / 8

_FINGERPRINT_RE = re.compile(r"^a=fingerprint:([^\r\n]+)", re.MULTILINE | re.IGNORECASE)


def pad_n(value: int) -> bytes:
    """Encode an integer mod N into fixed 256-byte big-endian representation."""
    return (value % SRP_N).to_bytes(PAD_LEN, "big")


def sha256(*parts: bytes) -> bytes:
    """Compute SHA-256 digest over concatenated byte parts."""
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def _srp_k() -> int:
    return int.from_bytes(sha256(pad_n(SRP_N), pad_n(SRP_G)), "big") % SRP_N


SRP_K = _srp_k()
_H_N_XOR_H_G = bytes(a ^ b for a, b in zip(sha256(pad_n(SRP_N)), sha256(pad_n(SRP_G))))


def extract_dtls_fingerprint(sdp: str) -> str:
    """Extract sorted canonical DTLS fingerprint string from an SDP payload."""
    matches = sorted(m.strip().lower() for m in _FINGERPRINT_RE.findall(sdp or ""))
    return ",".join(matches)


def build_dtls_context(host_fp: str, viewer_fp: str) -> str:
    """Bind host and viewer DTLS fingerprints into PAKE proof context."""
    return f"host:{host_fp.strip().lower()}|viewer:{viewer_fp.strip().lower()}"


def compute_x(salt: bytes, identity: str, pin: str) -> int:
    """Compute SRP private scalar x = H(salt | H(identity ':' pin))."""
    inner = sha256(f"{identity}:{pin}".encode("utf-8"))
    return int.from_bytes(sha256(salt, inner), "big")


def create_verifier(identity: str, pin: str, salt: bytes | None = None) -> tuple[str, str]:
    """Generate (salt_hex, verifier_hex) for local host storage without keeping plaintext PIN."""
    salt_bytes = salt if salt is not None else secrets.token_bytes(16)
    x = compute_x(salt_bytes, identity, pin)
    v = pow(SRP_G, x, SRP_N)
    return salt_bytes.hex(), pad_n(v).hex()


def compute_m1(
    identity: str,
    salt: bytes,
    A: int,
    B: int,
    K: bytes,
    dtls_context: str = "",
) -> bytes:
    """Compute client proof M1 binding identity, salt, ephemerals, key, and DTLS context."""
    h_identity = sha256(identity.encode("utf-8"))
    return sha256(
        _H_N_XOR_H_G,
        h_identity,
        salt,
        pad_n(A),
        pad_n(B),
        K,
        dtls_context.encode("utf-8"),
    )


def compute_m2(A: int, M1: bytes, K: bytes, dtls_context: str = "") -> bytes:
    """Compute server proof M2 confirming server knows session key K and DTLS context."""
    return sha256(pad_n(A), M1, K, dtls_context.encode("utf-8"))


class SRPAuthError(ValueError):
    """Raised when SRP protocol validation fails."""


@dataclass
class SRPServerSession:
    """Host-side SRP-6a handshake state."""

    identity: str
    salt: bytes
    verifier: int
    b: int
    B: int
    dtls_context: str = ""
    session_key: bytes | None = None

    @classmethod
    def start(
        cls,
        identity: str,
        salt_hex: str,
        verifier_hex: str,
        dtls_context: str = "",
    ) -> "SRPServerSession":
        salt = bytes.fromhex(salt_hex)
        verifier = int(verifier_hex, 16)
        b = int.from_bytes(secrets.token_bytes(32), "big")
        B = (SRP_K * verifier + pow(SRP_G, b, SRP_N)) % SRP_N
        if B == 0:
            raise SRPAuthError("Generated invalid zero server public ephemeral B")
        return cls(
            identity=identity,
            salt=salt,
            verifier=verifier,
            b=b,
            B=B,
            dtls_context=dtls_context,
        )

    @property
    def salt_hex(self) -> str:
        return self.salt.hex()

    @property
    def B_hex(self) -> str:
        return pad_n(self.B).hex()

    def verify_client_proof(self, A_hex: str, M1_hex: str) -> str:
        """Verify client proof (A, M1) and return server proof M2 hex."""
        A = int(A_hex, 16)
        if A % SRP_N == 0:
            raise SRPAuthError("Invalid client ephemeral A mod N == 0")

        u = int.from_bytes(sha256(pad_n(A), pad_n(self.B)), "big")
        if u == 0:
            raise SRPAuthError("Invalid scrambling parameter u == 0")

        base = (A * pow(self.verifier, u, SRP_N)) % SRP_N
        S = pow(base, self.b, SRP_N)
        K = sha256(pad_n(S))

        expected_m1 = compute_m1(
            self.identity, self.salt, A, self.B, K, self.dtls_context
        )
        try:
            client_m1 = bytes.fromhex(M1_hex)
        except ValueError as exc:
            raise SRPAuthError("Malformed client M1 hex") from exc

        if not hmac.compare_digest(expected_m1, client_m1):
            raise SRPAuthError("SRP client proof mismatch (wrong PIN or tampered context)")

        self.session_key = K
        M2 = compute_m2(A, expected_m1, K, self.dtls_context)
        return M2.hex()


@dataclass
class SRPClientSession:
    """Client-side SRP-6a handshake helper (used in CLI tests and verification)."""

    identity: str
    pin: str
    a: int
    A: int
    dtls_context: str = ""
    session_key: bytes | None = None
    _m1: bytes | None = None

    @classmethod
    def start(cls, identity: str, pin: str, dtls_context: str = "") -> "SRPClientSession":
        a = int.from_bytes(secrets.token_bytes(32), "big")
        A = pow(SRP_G, a, SRP_N)
        if A % SRP_N == 0:
            raise SRPAuthError("Generated invalid client ephemeral A")
        return cls(identity=identity, pin=pin, a=a, A=A, dtls_context=dtls_context)

    @property
    def A_hex(self) -> str:
        return pad_n(self.A).hex()

    def process_challenge(self, salt_hex: str, B_hex: str) -> str:
        """Process server (salt, B) and return client proof M1 hex."""
        salt = bytes.fromhex(salt_hex)
        B = int(B_hex, 16)
        if B % SRP_N == 0:
            raise SRPAuthError("Invalid server ephemeral B mod N == 0")

        u = int.from_bytes(sha256(pad_n(self.A), pad_n(B)), "big")
        if u == 0:
            raise SRPAuthError("Invalid scrambling parameter u == 0")

        x = compute_x(salt, self.identity, self.pin)
        g_x = pow(SRP_G, x, SRP_N)
        base = (B - SRP_K * g_x) % SRP_N
        exp = self.a + u * x
        S = pow(base, exp, SRP_N)
        K = sha256(pad_n(S))

        M1 = compute_m1(self.identity, salt, self.A, B, K, self.dtls_context)
        self.session_key = K
        self._m1 = M1
        return M1.hex()

    def verify_server_proof(self, M2_hex: str) -> bool:
        """Verify server proof M2 matches derived session key and DTLS context."""
        if self.session_key is None or self._m1 is None:
            return False
        expected_m2 = compute_m2(self.A, self._m1, self.session_key, self.dtls_context)
        try:
            provided = bytes.fromhex(M2_hex)
        except ValueError:
            return False
        return hmac.compare_digest(expected_m2, provided)


def hash_pairing_secret(secret_hex: str) -> str:
    """Hash a pairing token before persisting it in the host pairing registry."""
    return hashlib.sha256(bytes.fromhex(secret_hex)).hexdigest()


def compute_pairing_hmac(
    pairing_secret_hex: str,
    nonce_hex: str,
    dtls_context: str,
) -> str:
    """Compute HMAC-SHA256 proof binding pairing token, challenge nonce, and DTLS fingerprints."""
    key = bytes.fromhex(pairing_secret_hex)
    msg = f"{nonce_hex}|{dtls_context}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_pairing_hmac(
    stored_secret_hash_hex: str,
    provided_secret_proof_hmac: str,
    nonce_hex: str,
    dtls_context: str,
) -> bool:
    """Verify pairing proof when host stores raw or hashed pairing token.

    Note: Because HMAC requires the shared secret key on both ends, the host stores the
    encrypted or access-controlled pairing secret locally (`pairing_secret_hash` cannot
    be used directly as an HMAC key unless the host sends a blinded key). Therefore we
    derive `pairing_auth_key = SHA256(pairing_secret)` on the client and store
    `pairing_auth_key` on the host, while keeping the original `pairing_secret` on client
    OR verifying `SHA256(pairing_secret)` via `HMAC(stored_key, nonce|ctx)`.
    """
    try:
        key = bytes.fromhex(stored_secret_hash_hex)
    except ValueError:
        return False
    msg = f"{nonce_hex}|{dtls_context}".encode("utf-8")
    expected = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided_secret_proof_hmac.lower())
