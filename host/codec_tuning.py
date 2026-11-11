"""High-bitrate, crisp-text WebRTC codec tuning for aiortc H.264 and VP8 encoders."""

from __future__ import annotations

import logging
from typing import Any

from aiortc import RTCPeerConnection
from aiortc.codecs import h264, vpx

logger = logging.getLogger(__name__)
_TUNED = False


def apply_high_quality_codec_tuning() -> None:
    """Raise aiortc's conservative 500 kbps defaults to crisp remote-desktop bitrates."""
    global _TUNED
    if _TUNED:
        return
    _TUNED = True

    # 1. H.264: 6 Mbps default, up to 15 Mbps, ultrafast zerolatency
    h264.MIN_BITRATE = 800_000
    h264.DEFAULT_BITRATE = 6_000_000
    h264.MAX_BITRATE = 15_000_000

    orig_h264_encode = h264.H264Encoder._encode_frame

    def tuned_h264_encode(self, frame, force_keyframe: bool):
        was_none = self.codec is None
        gen = orig_h264_encode(self, frame, force_keyframe)
        # orig_h264_encode is a generator; create codec before first yield by advancing once
        try:
            first = next(gen)
        except StopIteration:
            return iter(())
        if was_none and self.codec is not None:
            try:
                opts = dict(self.codec.options or {})
                opts["preset"] = "ultrafast"
                opts["tune"] = "zerolatency"
                self.codec.options = opts
            except Exception:
                pass

        def _chain():
            yield first
            yield from gen

        return _chain()

    h264.H264Encoder._encode_frame = tuned_h264_encode  # type: ignore[method-assign]

    # 2. VP8 fallback: 4.5 Mbps default, up to 12 Mbps, sharper quantizer & no blur filter
    vpx.MIN_BITRATE = 600_000
    vpx.DEFAULT_BITRATE = 4_500_000
    vpx.MAX_BITRATE = 12_000_000

    orig_vpx_encode = vpx.Vp8Encoder.encode

    def tuned_vpx_encode(self, frame, force_keyframe: bool = False):
        was_none = self.codec is None
        result = orig_vpx_encode(self, frame, force_keyframe)
        if was_none and self.codec is not None:
            try:
                self.codec.qmax = 26
                opts = dict(self.codec.options or {})
                opts["noise-sensitivity"] = "0"
                opts["cpu-used"] = "-8"
                self.codec.options = opts
            except Exception:
                pass
        return result

    vpx.Vp8Encoder.encode = tuned_vpx_encode  # type: ignore[method-assign]


def set_video_sender_bitrate(pc: RTCPeerConnection, bitrate_bps: int) -> None:
    """Update active aiortc video encoder target_bitrate immediately for this PeerConnection."""
    for sender in pc.getSenders():
        if sender.track and sender.track.kind == "video":
            enc: Any = getattr(sender, "_RTCRtpSender__encoder", None)
            if enc is not None and hasattr(enc, "target_bitrate"):
                enc.target_bitrate = bitrate_bps


def boost_answer_sdp_bandwidth(sdp: str, max_kbps: int = 12000) -> str:
    """Add b=AS bandwidth hint in video section so Chrome ramps bitrate quickly."""
    lines = sdp.splitlines()
    out: list[str] = []
    in_video = False
    added_bw = False
    for line in lines:
        if line.startswith("m="):
            in_video = line.startswith("m=video ")
            added_bw = False
        if in_video and line.startswith("b=AS:"):
            out.append(f"b=AS:{max_kbps}")
            added_bw = True
            continue
        out.append(line)
        if in_video and not added_bw and line.startswith("c=IN "):
            out.append(f"b=AS:{max_kbps}")
            added_bw = True
    return "\r\n".join(out) + ("\r\n" if sdp.endswith("\n") else "")
