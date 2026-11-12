package com.webdesk.phonehost

import java.math.BigInteger
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * RFC 5054 2048-bit SRP-6a PAKE + DTLS/WS context binding + HMAC pairing.
 * Interoperates byte-for-byte with shared/srp.py and web/js/srp.js.
 */
object MobileSrp {

    private const val SRP_N_HEX =
        "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1" +
            "29024E088A67CC74020BBEA63B139B22514A08798E3404DD" +
            "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245" +
            "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED" +
            "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D" +
            "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F" +
            "83655D23DCA3AD961C62F356208552BB9ED529077096966D" +
            "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B" +
            "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9" +
            "DE2BCBF6955817183995497CEA956AE515D2261898FA0510" +
            "15728E5A8AACAA68FFFFFFFFFFFFFFFF"

    val SRP_N = BigInteger(SRP_N_HEX, 16)
    val SRP_G: BigInteger = BigInteger.valueOf(2)
    private const val PAD_LEN = 256

    private val rng = SecureRandom()

    val SRP_K: BigInteger by lazy {
        BigInteger(1, sha256(padN(SRP_N), padN(SRP_G))).mod(SRP_N)
    }

    private val hNXorHG: ByteArray by lazy {
        val hN = sha256(padN(SRP_N))
        val hG = sha256(padN(SRP_G))
        ByteArray(32) { i -> (hN[i].toInt() xor hG[i].toInt()).toByte() }
    }

    fun sha256(vararg parts: ByteArray): ByteArray {
        val digest = MessageDigest.getInstance("SHA-256")
        for (part in parts) {
            digest.update(part)
        }
        return digest.digest()
    }

    fun padN(value: BigInteger): ByteArray {
        val modVal = value.mod(SRP_N)
        val raw = modVal.toByteArray()
        val out = ByteArray(PAD_LEN)
        if (raw.size == PAD_LEN) {
            return raw
        }
        if (raw.size > PAD_LEN) {
            // Strip leading sign byte(s)
            System.arraycopy(raw, raw.size - PAD_LEN, out, 0, PAD_LEN)
        } else {
            System.arraycopy(raw, 0, out, PAD_LEN - raw.size, raw.size)
        }
        return out
    }

    fun hexToBytes(hex: String): ByteArray {
        val clean = hex.trim()
        require(clean.length % 2 == 0) { "Odd hex length" }
        return ByteArray(clean.length / 2) { i ->
            clean.substring(i * 2, i * 2 + 2).toInt(16).toByte()
        }
    }

    fun bytesToHex(bytes: ByteArray): String {
        val sb = StringBuilder(bytes.size * 2)
        for (b in bytes) {
            sb.append(String.format("%02x", b.toInt() and 0xFF))
        }
        return sb.toString()
    }

    fun randomBytes(count: Int): ByteArray {
        val out = ByteArray(count)
        rng.nextBytes(out)
        return out
    }

    fun extractDtlsFingerprint(sdp: String?): String {
        if (sdp.isNullOrBlank()) return ""
        val regex = Regex("^a=fingerprint:([^\\r\\n]+)", setOf(RegexOption.MULTILINE, RegexOption.IGNORE_CASE))
        return regex.findAll(sdp)
            .map { it.groupValues[1].trim().lowercase() }
            .sorted()
            .joinToString(",")
    }

    fun buildDtlsContext(hostFp: String, viewerFp: String): String {
        return "host:${hostFp.trim().lowercase()}|viewer:${viewerFp.trim().lowercase()}"
    }

    fun computeX(salt: ByteArray, identity: String, pin: String): BigInteger {
        val inner = sha256("$identity:$pin".toByteArray(StandardCharsets.UTF_8))
        return BigInteger(1, sha256(salt, inner))
    }

    fun createVerifier(identity: String, pin: String, customSalt: ByteArray? = null): Pair<String, String> {
        val salt = customSalt ?: randomBytes(16)
        val x = computeX(salt, identity, pin)
        val v = SRP_G.modPow(x, SRP_N)
        return bytesToHex(salt) to bytesToHex(padN(v))
    }

    fun computeM1(
        identity: String,
        salt: ByteArray,
        aBig: BigInteger,
        bBig: BigInteger,
        kBytes: ByteArray,
        dtlsContext: String
    ): ByteArray {
        val hIdentity = sha256(identity.toByteArray(StandardCharsets.UTF_8))
        return sha256(
            hNXorHG,
            hIdentity,
            salt,
            padN(aBig),
            padN(bBig),
            kBytes,
            dtlsContext.toByteArray(StandardCharsets.UTF_8)
        )
    }

    fun computeM2(aBig: BigInteger, m1: ByteArray, kBytes: ByteArray, dtlsContext: String): ByteArray {
        return sha256(
            padN(aBig),
            m1,
            kBytes,
            dtlsContext.toByteArray(StandardCharsets.UTF_8)
        )
    }

    fun hashPairingSecret(secretHex: String): String {
        return bytesToHex(sha256(hexToBytes(secretHex)))
    }

    fun verifyPairingHmac(
        storedAuthKeyHex: String,
        providedProofHex: String,
        nonceHex: String,
        dtlsContext: String
    ): Boolean {
        return try {
            val key = hexToBytes(storedAuthKeyHex)
            val provided = hexToBytes(providedProofHex)
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(key, "HmacSHA256"))
            val expected = mac.doFinal("$nonceHex|$dtlsContext".toByteArray(StandardCharsets.UTF_8))
            MessageDigest.isEqual(expected, provided)
        } catch (_: Exception) {
            false
        }
    }

    class SrpServerSession private constructor(
        val identity: String,
        val salt: ByteArray,
        private val verifier: BigInteger,
        private val b: BigInteger,
        private val bPub: BigInteger,
        private val dtlsContext: String
    ) {
        val saltHex: String get() = bytesToHex(salt)
        val bHex: String get() = bytesToHex(padN(bPub))

        fun verifyClientProof(aHex: String, m1Hex: String): String {
            val aPub = BigInteger(aHex.trim(), 16)
            if (aPub.mod(SRP_N) == BigInteger.ZERO) {
                throw SecurityException("Invalid client ephemeral A mod N == 0")
            }
            val u = BigInteger(1, sha256(padN(aPub), padN(bPub)))
            if (u == BigInteger.ZERO) {
                throw SecurityException("Invalid scrambling parameter u == 0")
            }
            val base = aPub.multiply(verifier.modPow(u, SRP_N)).mod(SRP_N)
            val s = base.modPow(b, SRP_N)
            val kBytes = sha256(padN(s))

            val expectedM1 = computeM1(identity, salt, aPub, bPub, kBytes, dtlsContext)
            val clientM1 = try {
                hexToBytes(m1Hex)
            } catch (_: Exception) {
                throw SecurityException("Malformed client M1 hex")
            }
            if (!MessageDigest.isEqual(expectedM1, clientM1)) {
                throw SecurityException("Invalid PIN or verification context")
            }
            val m2 = computeM2(aPub, expectedM1, kBytes, dtlsContext)
            return bytesToHex(m2)
        }

        companion object {
            fun start(
                identity: String,
                saltHex: String,
                verifierHex: String,
                dtlsContext: String = ""
            ): SrpServerSession {
                val salt = hexToBytes(saltHex)
                val verifier = BigInteger(verifierHex.trim(), 16)
                val b = BigInteger(1, randomBytes(32))
                val bPub = SRP_K.multiply(verifier).add(SRP_G.modPow(b, SRP_N)).mod(SRP_N)
                if (bPub == BigInteger.ZERO) {
                    throw SecurityException("Generated invalid zero server public ephemeral B")
                }
                return SrpServerSession(identity, salt, verifier, b, bPub, dtlsContext)
            }
        }
    }
}
