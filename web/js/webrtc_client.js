import {
  SRPClient,
  buildDtlsContext,
  computePairingProof,
  extractDtlsFingerprint,
} from "./srp.js?v=3";

const CHUNK_BYTES = 32 * 1024;

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

export class WebRTCViewerClient {
  constructor({
    wsUrl,
    iceServers,
    videoEl,
    canvasEl,
    iceTimeoutMs = 6000,
    onStatus,
    onReady,
    onError,
    onPairingGranted,
    onPairingInvalid,
    onClipboardSync,
    onFileProgress,
    onStats,
    onSystemStatus,
    onQuickActionResult,
    onSnapshot,
    onClosed,
  }) {
    this.wsUrl = wsUrl;
    this.iceServers = iceServers || [];
    this.videoEl = videoEl;
    this.canvasEl = canvasEl;
    this.iceTimeoutMs = iceTimeoutMs;
    this.onStatus = onStatus || (() => {});
    this.onReady = onReady || (() => {});
    this.onError = onError || (() => {});
    this.onPairingGranted = onPairingGranted || (() => {});
    this.onPairingInvalid = onPairingInvalid || (() => {});
    this.onClipboardSync = onClipboardSync || (() => {});
    this.onFileProgress = onFileProgress || (() => {});
    this.onStats = onStats || (() => {});
    this.onSystemStatus = onSystemStatus || (() => {});
    this.onQuickActionResult = onQuickActionResult || (() => {});
    this.onSnapshot = onSnapshot || (() => {});
    this.onClosed = onClosed || (() => {});

    this.ws = null;
    this.pc = null;
    this.sessionId = null;
    this.viewerFp = "";
    this.srpClient = null;
    this.channels = {};
    this._pendingIce = [];
    this._statsTimer = null;
    this._prevBytes = 0;
    this._prevTs = 0;
    this._closed = false;
    this._connectOptions = null;
    this._pendingFileResolvers = new Map();

    // Auto WSS Relay fallback state
    this._authSucceeded = false;
    this._sessionReady = false;
    this._videoLive = false;
    this._webrtcReady = false;
    this.wsFallbackActive = false;
    this._stalePc = null;
    this._iceWatchdog = null;
    this._markVideoLive = null;
    this._wsSeq = 0;
    this._wsFrameCount = 0;
    this._wsBytesCount = 0;
    this._wsStatsLastTs = 0;
    this._drawingFrame = false;
  }

  async connect({ mode, hostId, supportCode, pin, pairing, rememberDevice, controlOnly = false }) {
    this._connectOptions = {
      mode,
      hostId,
      supportCode,
      pin,
      pairing,
      rememberDevice,
      controlOnly: Boolean(controlOnly),
    };
    this.onStatus(controlOnly ? "Connecting Quick Actions..." : "Connecting to signaling server...");

    if (controlOnly) {
      this.pc = null;
      this.viewerFp = "ws";
      this.wsFallbackActive = true;
      await this._openSignalingAndInitiate("");
      return;
    }

    if (this.videoEl && !this._markVideoLive) {
      this._markVideoLive = () => {
        if (this.videoEl && this.videoEl.videoWidth > 0) {
          this._videoLive = true;
          this._maybeFinalizeWebRtc();
        }
      };
      this.videoEl.addEventListener("loadeddata", this._markVideoLive);
      this.videoEl.addEventListener("playing", this._markVideoLive);
      this.videoEl.addEventListener("resize", this._markVideoLive);
    }

    this.pc = new RTCPeerConnection({ iceServers: this.iceServers });
    const transceiver = this.pc.addTransceiver("video", { direction: "recvonly" });
    this._preferH264Codec(transceiver);

    for (const label of ["control", "input", "clipboard", "files"]) {
      const dc = this.pc.createDataChannel(label, { ordered: true });
      this._wireDataChannel(label, dc);
      this.channels[label] = dc;
    }

    this.pc.ontrack = (event) => {
      const stream = event.streams && event.streams[0]
        ? event.streams[0]
        : new MediaStream([event.track]);
      this.videoEl.srcObject = stream;
      this.videoEl.play().catch(() => {});
      if (this._markVideoLive) this._markVideoLive();
    };

    this.pc.onicecandidate = (event) => {
      if (!event.candidate) return;
      const candPayload = {
        candidate: event.candidate.candidate,
        sdpMid: event.candidate.sdpMid,
        sdpMLineIndex: event.candidate.sdpMLineIndex,
      };
      if (this.sessionId && this.ws && this.ws.readyState === WebSocket.OPEN) {
        this._sendWs({
          type: "ice_candidate",
          session_id: this.sessionId,
          payload: candPayload,
        });
      } else {
        this._pendingIce.push(candPayload);
      }
    };

    this.pc.onconnectionstatechange = () => {
      if (!this.pc || this.wsFallbackActive) return;
      if (this.pc.connectionState === "connected") {
        this._startStatsLoop();
        if (!this._sessionReady) {
          this.onStatus("Encrypted tunnel open — waiting for remote display...");
        }
      } else if (this.pc.connectionState === "failed") {
        if (this._authSucceeded && this.ws && this.ws.readyState === WebSocket.OPEN) {
          this._activateWsFallback("Direct P2P failed — switching to WSS Relay...");
        } else {
          this.onError("WebRTC P2P/relay connection failed.");
          this.close();
        }
      }
    };

    this.pc.oniceconnectionstatechange = () => {
      if (!this.pc || this.wsFallbackActive) return;
      if (
        this.pc.iceConnectionState === "failed" &&
        this._authSucceeded &&
        !this._webrtcReady &&
        this.ws &&
        this.ws.readyState === WebSocket.OPEN
      ) {
        this._activateWsFallback("Mobile/firewall NAT blocked P2P — switching to WSS Relay...");
      }
    };

    const offer = await this.pc.createOffer();
    await this.pc.setLocalDescription(offer);
    this.viewerFp = extractDtlsFingerprint(offer.sdp);

    await this._openSignalingAndInitiate(offer.sdp);
  }

  _openSignalingAndInitiate(sdpOffer) {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.wsUrl);
      ws.binaryType = "arraybuffer";
      this.ws = ws;

      ws.onopen = () => {
        const opts = this._connectOptions;
        if (opts.mode === "it2me") {
          ws.send(
            JSON.stringify({
              type: "connect_support",
              payload: {
                code: opts.supportCode,
                sdp_offer: sdpOffer,
                viewer_label: navigator.userAgent.slice(0, 60),
              },
            })
          );
        } else {
          ws.send(
            JSON.stringify({
              type: "connect_host",
              host_id: opts.hostId,
              payload: {
                sdp_offer: sdpOffer,
                pairing_id: opts.pairing ? opts.pairing.pairing_id : null,
                control_only: Boolean(opts.controlOnly),
                viewer_label: navigator.userAgent.slice(0, 60),
              },
            })
          );
        }
        resolve();
      };

      ws.onerror = () => {
        const err = new Error("WebSocket signaling error");
        this.onError(err.message);
        reject(err);
      };

      ws.onmessage = async (event) => {
        try {
          if (event.data instanceof ArrayBuffer) {
            await this._handleFallbackFrame(event.data);
            return;
          }
          const msg = JSON.parse(event.data);
          await this._handleSignaling(msg);
        } catch (err) {
          this.onError(err.message || "Signaling error");
          this.close();
        }
      };

      ws.onclose = () => {
        if (!this._closed && (this.wsFallbackActive || !this.pc || this.pc.connectionState !== "connected")) {
          this.onClosed("Signaling closed");
        }
      };
    });
  }

  async _handleSignaling(msg) {
    const type = msg.type;
    const payload = msg.payload || {};

    if (type === "session_created") {
      this.sessionId = msg.session_id;
      this.onStatus(`Negotiating with ${payload.host_name || "host"}...`);
      for (const c of this._pendingIce) {
        this._sendWs({ type: "ice_candidate", session_id: this.sessionId, payload: c });
      }
      this._pendingIce = [];
    } else if (type === "sdp_answer") {
      if (typeof window !== "undefined" && window.__WEBDESK_FORCE_WS_FALLBACK) {
        return;
      }
      await this.pc.setRemoteDescription(
        new RTCSessionDescription({ type: "answer", sdp: payload.sdp_answer })
      );
    } else if (type === "pairing_challenge") {
      this.onStatus("Authenticating trusted device...");
      const pairing = this._connectOptions.pairing;
      if (!pairing || !pairing.pairing_secret) {
        this._sendWs({
          type: "pairing_client_proof",
          session_id: this.sessionId,
          payload: { fallback_to_pin: true },
        });
        return;
      }
      const ctx = buildDtlsContext(payload.host_fp, this.viewerFp);
      const proof = await computePairingProof(pairing.pairing_secret, payload.nonce, ctx);
      this._sendWs({
        type: "pairing_client_proof",
        session_id: this.sessionId,
        payload: { proof },
      });
    } else if (type === "pairing_accepted") {
      this._authSucceeded = true;
      if (this._connectOptions && this._connectOptions.controlOnly) {
        this.onStatus("Trusted pairing verified — opening Quick Actions...");
      } else {
        this.onStatus("Trusted pairing verified! Opening video...");
        this._startIceWatchdog();
      }
    } else if (type === "pake_challenge") {
      this.onStatus("Verifying PIN via SRP-6a...");
      const pin = this._connectOptions.pin || "";
      const ctx = buildDtlsContext(payload.host_fp, this.viewerFp);
      this.srpClient = new SRPClient(payload.identity, pin, ctx);
      const m1 = await this.srpClient.processChallenge(payload.salt, payload.B);
      this._sendWs({
        type: "pake_client_proof",
        session_id: this.sessionId,
        payload: {
          A: this.srpClient.AHex,
          M1: m1,
          remember_device: Boolean(this._connectOptions.rememberDevice),
          viewer_label: "Browser",
        },
      });
    } else if (type === "pake_server_proof") {
      const ok = this.srpClient && (await this.srpClient.verifyServerProof(payload.M2));
      if (!ok) {
        this.onError("Security alert: Host proof / DTLS fingerprint verification failed!");
        this.close();
        return;
      }
      this._authSucceeded = true;
      if (this._connectOptions && this._connectOptions.controlOnly) {
        this.onStatus("PIN verified — opening Quick Actions...");
      } else {
        this.onStatus("PIN verified! Starting remote display...");
        this._startIceWatchdog();
      }
    } else if (type === "ws_fallback_ready") {
      if (this._stalePc) {
        try {
          this._stalePc.close();
        } catch {}
        this._stalePc = null;
      }
      this.wsFallbackActive = true;
      this._clearIceWatchdog();
      this._startFallbackStatsLoop();
      this.onReady({ ...payload, wsFallback: true });
    } else if (type === "ws_channel") {
      if (payload.channel && payload.data) {
        this._handleChannelPayload(payload.channel, payload.data);
      }
    } else if (type === "ice_candidate") {
      if (payload.candidate && this.pc) {
        await this.pc.addIceCandidate(new RTCIceCandidate(payload));
      }
    } else if (type === "session_reject") {
      if (payload.code === "pairing_invalid") {
        this.onPairingInvalid(this._connectOptions.hostId);
      }
      this.onError(payload.reason || "Connection rejected");
      this.close();
    } else if (type === "session_end") {
      this.close(payload.reason || "Session ended");
    }
  }

  _maybeFinalizeWebRtc() {
    if (this._sessionReady && this._videoLive && !this._webrtcReady) {
      this._webrtcReady = true;
      this._clearIceWatchdog();
    }
  }

  _wireDataChannel(label, dc) {
    dc.onopen = () => {
      if (label === "control" && !this._sessionReady) {
        this.onStatus("Control channel ready — starting screen...");
      }
    };
    dc.onmessage = (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      this._handleChannelPayload(label, data);
    };
  }

  _handleChannelPayload(label, data) {
    if (label === "control") {
      if (data.type === "session_ready") {
        this._sessionReady = true;
        this._maybeFinalizeWebRtc();
        this.onReady(data);
      } else if (data.type === "pairing_grant") {
        this.onPairingGranted({
          hostId: this._connectOptions.hostId,
          pairing_id: data.pairing_id,
          pairing_secret: data.pairing_secret,
        });
      } else if (data.type === "system_status") {
        this.onSystemStatus(data.status || {});
      } else if (data.type === "quick_action_result") {
        this.onQuickActionResult(data);
      } else if (data.type === "quick_snapshot") {
        this.onSnapshot(data);
      }
    } else if (label === "clipboard") {
      if (data.type === "clipboard_sync") {
        this.onClipboardSync(data.text || "");
      }
    } else if (label === "files") {
      const resolver = this._pendingFileResolvers.get(data.file_id);
      if (data.type === "file_saved" || data.type === "file_error") {
        if (resolver) {
          this._pendingFileResolvers.delete(data.file_id);
          resolver(data);
        }
      }
    }
  }

  _startIceWatchdog() {
    this._clearIceWatchdog();
    if (typeof window !== "undefined" && window.__WEBDESK_FORCE_WS_FALLBACK) {
      this._activateWsFallback("Direct P2P blocked — switching to WSS Relay...");
      return;
    }
    const timeoutMs =
      typeof window !== "undefined" && typeof window.__WEBDESK_ICE_TIMEOUT_MS === "number"
        ? window.__WEBDESK_ICE_TIMEOUT_MS
        : this.iceTimeoutMs;
    this._iceWatchdog = setTimeout(() => {
      if (!this._closed && !this._webrtcReady && !this.wsFallbackActive && this._authSucceeded) {
        this._activateWsFallback("Direct P2P timed out — switching to WSS Relay...");
      }
    }, timeoutMs);
  }

  _clearIceWatchdog() {
    if (this._iceWatchdog) {
      clearTimeout(this._iceWatchdog);
      this._iceWatchdog = null;
    }
  }

  _activateWsFallback(statusMsg) {
    if (this._closed || this.wsFallbackActive || !this._authSucceeded) return;
    this.wsFallbackActive = true;
    this._clearIceWatchdog();
    if (this._statsTimer) {
      clearInterval(this._statsTimer);
      this._statsTimer = null;
    }
    this.onStatus(statusMsg || "Switching to WSS Relay...");
    if (this.pc) {
      this.pc.onconnectionstatechange = null;
      this.pc.oniceconnectionstatechange = null;
      this.pc.onicecandidate = null;
      this.pc.ontrack = null;
      // Close the old PeerConnection after ws_fallback_ready arrives so a
      // DTLS close_notify packet does not tear down the host session first.
      this._stalePc = this.pc;
      this.pc = null;
    }
    this._sendWs({
      type: "enable_ws_fallback",
      session_id: this.sessionId,
      payload: {},
    });
  }

  async _handleFallbackFrame(arrayBuffer) {
    if (!this.canvasEl || this._closed) return;
    this._wsSeq += 1;
    const seq = this._wsSeq;
    this._wsFrameCount += 1;
    this._wsBytesCount += arrayBuffer.byteLength;

    if (!this._drawingFrame) {
      this._drawingFrame = true;
      try {
        const blob = new Blob([arrayBuffer], { type: "image/jpeg" });
        const bitmap = await createImageBitmap(blob);
        if (this.canvasEl.width !== bitmap.width || this.canvasEl.height !== bitmap.height) {
          this.canvasEl.width = bitmap.width;
          this.canvasEl.height = bitmap.height;
        }
        const ctx = this.canvasEl.getContext("2d");
        if (ctx) ctx.drawImage(bitmap, 0, 0);
        bitmap.close();
      } catch {
        // Ignore transient decode failure on session close
      } finally {
        this._drawingFrame = false;
      }
    }

    this._sendWs({
      type: "ws_frame_ack",
      session_id: this.sessionId,
      payload: { seq },
    });
  }

  _startFallbackStatsLoop() {
    if (this._statsTimer) clearInterval(this._statsTimer);
    this._wsStatsLastTs = performance.now();
    this._wsFrameCount = 0;
    this._wsBytesCount = 0;
    this._statsTimer = setInterval(() => {
      if (this._closed || !this.wsFallbackActive) return;
      const now = performance.now();
      const dt = Math.max(0.1, (now - this._wsStatsLastTs) / 1000);
      const fps = Math.round(this._wsFrameCount / dt);
      const bitrateKbps = Math.round((this._wsBytesCount * 8) / (dt * 1000));
      this._wsFrameCount = 0;
      this._wsBytesCount = 0;
      this._wsStatsLastTs = now;
      this.onStats({ fps, rttMs: null, candidateType: "ws_relay", bitrateKbps });
    }, 1000);
  }

  _preferH264Codec(transceiver) {
    try {
      if (!transceiver.setCodecPreferences || !RTCRtpReceiver.getCapabilities) return;
      const caps = RTCRtpReceiver.getCapabilities("video");
      if (!caps || !caps.codecs) return;
      const h264 = [];
      const others = [];
      for (const c of caps.codecs) {
        if ((c.mimeType || "").toLowerCase() === "video/h264") {
          h264.push(c);
        } else {
          others.push(c);
        }
      }
      if (h264.length > 0) {
        transceiver.setCodecPreferences([...h264, ...others]);
      }
    } catch {
      // Fallback safely to browser default codec order
    }
  }

  sendControl(data) {
    this._sendChannel("control", data);
  }

  sendQuickAction(action, params = {}) {
    const actionId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
    this._sendChannel("control", {
      type: "quick_action",
      action,
      params,
      action_id: actionId,
    });
    return actionId;
  }

  requestSystemStatus() {
    this._sendChannel("control", { type: "system_status" });
  }

  requestSnapshot() {
    this._sendChannel("control", { type: "quick_snapshot" });
  }

  sendInput(data) {
    this._sendChannel("input", data);
  }

  sendText(text) {
    this._sendChannel("input", { type: "paste_text", text });
  }

  sendClipboard(text) {
    this._sendChannel("clipboard", { type: "clipboard_set", text });
  }

  pullClipboard() {
    this._sendChannel("clipboard", { type: "clipboard_pull" });
  }

  async uploadFile(file) {
    const ch = this.channels.files;
    if (!this.wsFallbackActive && (!ch || ch.readyState !== "open")) {
      throw new Error("File transfer channel is not open");
    }
    const fileId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
    const buf = await file.arrayBuffer();
    const digestBuf = await crypto.subtle.digest("SHA-256", buf);
    const shaHex = Array.from(new Uint8Array(digestBuf))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");

    const donePromise = new Promise((resolve) => {
      this._pendingFileResolvers.set(fileId, resolve);
    });

    this._sendChannel("files", {
      type: "file_offer",
      file_id: fileId,
      name: file.name,
      size: buf.byteLength,
    });

    let offset = 0;
    while (offset < buf.byteLength) {
      const transport = this.wsFallbackActive ? this.ws : ch;
      while (transport && transport.bufferedAmount > 512 * 1024) {
        await new Promise((r) => setTimeout(r, 15));
      }
      const slice = buf.slice(offset, offset + CHUNK_BYTES);
      this._sendChannel("files", {
        type: "file_chunk",
        file_id: fileId,
        data: arrayBufferToBase64(slice),
      });
      offset += slice.byteLength;
      const pct = Math.min(100, Math.round((offset / Math.max(1, buf.byteLength)) * 100));
      this.onFileProgress(`Uploading ${file.name}: ${pct}%`);
    }

    this._sendChannel("files", {
      type: "file_complete",
      file_id: fileId,
      sha256: shaHex,
    });

    const result = await donePromise;
    if (result.type === "file_error") {
      throw new Error(result.reason || "File transfer failed");
    }
    return result;
  }

  _sendChannel(label, data) {
    if (this.wsFallbackActive) {
      this._sendWs({
        type: "ws_channel",
        session_id: this.sessionId,
        payload: { channel: label, data },
      });
      return;
    }
    const ch = this.channels[label];
    if (ch && ch.readyState === "open") {
      ch.send(JSON.stringify(data));
    }
  }

  _sendWs(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  _startStatsLoop() {
    if (this._statsTimer) return;
    this._statsTimer = setInterval(async () => {
      if (!this.pc || this._closed) return;
      try {
        const stats = await this.pc.getStats();
        let fps = 0;
        let framesDecoded = 0;
        let rttMs = null;
        let candidateType = "";
        let bytesReceived = 0;
        const localCandTypes = new Map();
        let selectedPairId = null;

        stats.forEach((report) => {
          if (report.type === "local-candidate") {
            localCandTypes.set(report.id, report.candidateType || "");
          } else if (report.type === "transport" && report.selectedCandidatePairId) {
            selectedPairId = report.selectedCandidatePairId;
          }
        });

        stats.forEach((report) => {
          if (report.type === "inbound-rtp" && report.kind === "video") {
            fps = Math.round(report.framesPerSecond || 0);
            framesDecoded = report.framesDecoded || 0;
            bytesReceived = report.bytesReceived || 0;
          } else if (
            report.type === "candidate-pair" &&
            ((selectedPairId && report.id === selectedPairId) ||
              (report.nominated && report.state === "succeeded"))
          ) {
            if (typeof report.currentRoundTripTime === "number") {
              rttMs = Math.round(report.currentRoundTripTime * 1000);
            }
            if (report.localCandidateId) {
              candidateType = localCandTypes.get(report.localCandidateId) || candidateType;
            }
          }
        });

        const now = performance.now();
        let bitrateKbps = 0;
        if (this._prevTs > 0 && bytesReceived >= this._prevBytes) {
          const dt = (now - this._prevTs) / 1000;
          if (dt > 0) {
            bitrateKbps = Math.round(((bytesReceived - this._prevBytes) * 8) / (dt * 1000));
          }
        }
        this._prevBytes = bytesReceived;
        this._prevTs = now;

        if (framesDecoded > 0 || (this.videoEl && this.videoEl.videoWidth > 0)) {
          this._videoLive = true;
          this._maybeFinalizeWebRtc();
        }
        this.onStats({ fps, framesDecoded, rttMs, candidateType, bitrateKbps });
      } catch {
        // Ignore transient stats errors on teardown
      }
    }, 1000);
  }

  close(reason = "Disconnected") {
    if (this._closed) return;
    this._closed = true;
    this._clearIceWatchdog();
    if (this._statsTimer) {
      clearInterval(this._statsTimer);
      this._statsTimer = null;
    }
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      if (this.sessionId) {
        this._sendWs({ type: "session_end", session_id: this.sessionId, payload: { reason } });
      }
      this.ws.close();
    }
    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }
    if (this._stalePc) {
      try {
        this._stalePc.close();
      } catch {}
      this._stalePc = null;
    }
    if (this.videoEl) {
      if (this._markVideoLive) {
        this.videoEl.removeEventListener("loadeddata", this._markVideoLive);
        this.videoEl.removeEventListener("playing", this._markVideoLive);
        this.videoEl.removeEventListener("resize", this._markVideoLive);
        this._markVideoLive = null;
      }
      this.videoEl.srcObject = null;
    }
    this.onClosed(reason);
  }
}
