// Translates browser pointer/wheel/keyboard and mobile Trackpad/Touch events into DataChannel messages.

export function getVideoContentRect(videoEl) {
  const rect = videoEl.getBoundingClientRect();
  const vw = videoEl.videoWidth || videoEl.width || rect.width;
  const vh = videoEl.videoHeight || videoEl.height || rect.height;
  if (!rect.width || !rect.height || !vw || !vh) {
    return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
  }

  if (videoEl.classList.contains("fit-original")) {
    return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
  }

  const scale = Math.min(rect.width / vw, rect.height / vh);
  const renderedW = vw * scale;
  const renderedH = vh * scale;
  const offsetX = (rect.width - renderedW) / 2;
  const offsetY = (rect.height - renderedH) / 2;
  return {
    left: rect.left + offsetX,
    top: rect.top + offsetY,
    width: renderedW,
    height: renderedH,
  };
}

export function clientToNormalized(videoEl, clientX, clientY) {
  const box = getVideoContentRect(videoEl);
  if (box.width <= 0 || box.height <= 0) return { x: 0, y: 0 };
  const nx = Math.max(0, Math.min(1, (clientX - box.left) / box.width));
  const ny = Math.max(0, Math.min(1, (clientY - box.top) / box.height));
  return { x: nx, y: ny };
}

export function isTouchCapable() {
  return Boolean(
    (typeof navigator !== "undefined" && navigator.maxTouchPoints > 0) ||
      (typeof window !== "undefined" && "ontouchstart" in window)
  );
}

export class InputCaptureController {
  constructor({ stageEl, videoEl, sendInput, onCursorMove, onZoomChange }) {
    this.stageEl = stageEl;
    this.videoEl = videoEl;
    this.sendInput = sendInput;
    this.onCursorMove = onCursorMove || (() => {});
    this.onZoomChange = onZoomChange || (() => {});
    this.enabled = true;
    this.inputMode = isTouchCapable() ? "trackpad" : "desktop";
    this.cursor = { x: 0.5, y: 0.5 };
    this._pendingMove = null;
    this._rafScheduled = false;
    this._dragging = false;
    this._bound = [];

    this.zoom = 1;
    this.panX = 0;
    this.panY = 0;
    this.minZoom = 1;
    this.maxZoom = 5;

    // Touch gesture state
    this._touchStart = { x: 0, y: 0 };
    this._touchLast = { x: 0, y: 0 };
    this._touchStartTime = 0;
    this._moved1 = false;
    this._twoFinger = false;
    this._moved2 = false;
    this._pinchMode = null;
    this._pinchStartDist = 0;
    this._pinchLastDist = 0;
    this._lastMid = { x: 0, y: 0 };
    this._lastTapAt = 0;
    this._doubleTapDragging = false;
  }

  _applyZoomTransform() {
    if (!this.videoEl) return;
    if (this.zoom <= 1.005) {
      this.zoom = 1;
      this.panX = 0;
      this.panY = 0;
      this.videoEl.style.transformOrigin = "";
      this.videoEl.style.transform = "";
      this.videoEl.style.willChange = "";
    } else {
      this.videoEl.style.transformOrigin = "0 0";
      this.videoEl.style.transform = "translate3d(" + this.panX.toFixed(2) + "px, " + this.panY.toFixed(2) + "px, 0) scale(" + this.zoom.toFixed(4) + ")";
      this.videoEl.style.willChange = "transform";
    }
    this.onZoomChange(this.zoom);
    this.onCursorMove(this.cursor.x, this.cursor.y, this.inputMode);
  }

  setZoom(targetZoom, anchorX = null, anchorY = null, moveDx = 0, moveDy = 0) {
    if (!this.videoEl || !this.stageEl) return;
    const oldZoom = this.zoom || 1;
    let newZoom = Math.max(this.minZoom, Math.min(this.maxZoom, targetZoom));
    if (newZoom <= 1.02 && targetZoom <= oldZoom) newZoom = 1;
    if (newZoom <= 1.005) {
      this.zoom = 1;
      this.panX = 0;
      this.panY = 0;
      this._applyZoomTransform();
      return;
    }
    const stageRect = this.stageEl.getBoundingClientRect();
    const rect = this.videoEl.getBoundingClientRect();
    const ax = anchorX !== null ? anchorX : stageRect.left + stageRect.width / 2;
    const ay = anchorY !== null ? anchorY : stageRect.top + stageRect.height / 2;
    const baseW = rect.width > 0 ? rect.width / oldZoom : stageRect.width;
    const baseH = rect.height > 0 ? rect.height / oldZoom : stageRect.height;
    const baseLeft = rect.left - this.panX;
    const baseTop = rect.top - this.panY;
    const nextPanX = this.panX + (ax - rect.left) * (1 - newZoom / oldZoom) + moveDx;
    const nextPanY = this.panY + (ay - rect.top) * (1 - newZoom / oldZoom) + moveDy;
    const scaledW = baseW * newZoom;
    const scaledH = baseH * newZoom;
    const minPanX = scaledW <= stageRect.width ? 0 : stageRect.right - baseLeft - scaledW;
    const maxPanX = scaledW <= stageRect.width ? 0 : stageRect.left - baseLeft;
    const minPanY = scaledH <= stageRect.height ? 0 : stageRect.bottom - baseTop - scaledH;
    const maxPanY = scaledH <= stageRect.height ? 0 : stageRect.top - baseTop;
    this.zoom = newZoom;
    this.panX = Math.max(minPanX, Math.min(maxPanX, nextPanX));
    this.panY = Math.max(minPanY, Math.min(maxPanY, nextPanY));
    this._applyZoomTransform();
  }

  zoomBy(factor, anchorX = null, anchorY = null) {
    this.setZoom(this.zoom * factor, anchorX, anchorY);
  }

  resetZoom() {
    this.zoom = 1;
    this.panX = 0;
    this.panY = 0;
    this._applyZoomTransform();
  }

  setMode(mode) {
    if (["desktop", "trackpad", "touch"].includes(mode)) {
      this.inputMode = mode;
      this.onCursorMove(this.cursor.x, this.cursor.y, this.inputMode);
    }
  }

  _updateCursor(nx, ny) {
    this.cursor.x = Math.max(0, Math.min(1, nx));
    this.cursor.y = Math.max(0, Math.min(1, ny));
    this.onCursorMove(this.cursor.x, this.cursor.y, this.inputMode);
  }

  _queueMove(clientX, clientY) {
    const pt = clientToNormalized(this.videoEl, clientX, clientY);
    this._updateCursor(pt.x, pt.y);
    this._pendingMove = { x: this.cursor.x, y: this.cursor.y };
    if (!this._rafScheduled) {
      this._rafScheduled = true;
      requestAnimationFrame(() => {
        this._rafScheduled = false;
        if (this._pendingMove && this.enabled) {
          this.sendInput({
            type: "mouse_move",
            x: this._pendingMove.x,
            y: this._pendingMove.y,
          });
        }
      });
    }
  }

  attach() {
    const onVideoMouseMove = (e) => {
      if (!this.enabled || this._dragging) return;
      this._queueMove(e.clientX, e.clientY);
    };

    const onWindowMouseMove = (e) => {
      if (!this.enabled || !this._dragging) return;
      this._queueMove(e.clientX, e.clientY);
    };

    const onMouseDown = (e) => {
      if (!this.enabled) return;
      this._dragging = true;
      this.stageEl.focus({ preventScroll: true });
      const pt = clientToNormalized(this.videoEl, e.clientX, e.clientY);
      this._updateCursor(pt.x, pt.y);
      this.sendInput({
        type: "mouse_button",
        button: e.button,
        down: true,
        x: pt.x,
        y: pt.y,
      });
    };

    const onWindowMouseUp = (e) => {
      if (!this.enabled || !this._dragging) return;
      this._dragging = false;
      const pt = clientToNormalized(this.videoEl, e.clientX, e.clientY);
      this._updateCursor(pt.x, pt.y);
      this.sendInput({
        type: "mouse_button",
        button: e.button,
        down: false,
        x: pt.x,
        y: pt.y,
      });
    };

    const onTouchStart = (e) => {
      if (!this.enabled) return;
      if (e.touches.length >= 2) {
        if (!this._twoFinger && (this.inputMode === "touch" || this._doubleTapDragging)) {
          this.sendInput({ type: "mouse_button", button: 0, down: false, x: this.cursor.x, y: this.cursor.y });
          this._doubleTapDragging = false;
        }
        this._twoFinger = true;
        this._moved2 = false;
        this._pinchMode = this.zoom > 1.02 ? "zoom_pan" : null;
        const t0 = e.touches[0];
        const t1 = e.touches[1];
        this._lastMid = {
          x: (t0.clientX + t1.clientX) / 2,
          y: (t0.clientY + t1.clientY) / 2,
        };
        this._pinchStartDist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
        this._pinchLastDist = this._pinchStartDist;
        return;
      }

      if (e.touches.length === 1) {
        const t = e.touches[0];
        this._twoFinger = false;
        this._moved1 = false;
        this._touchStart = { x: t.clientX, y: t.clientY };
        this._touchLast = { x: t.clientX, y: t.clientY };
        this._touchStartTime = performance.now();

        if (this.inputMode === "touch") {
          const pt = clientToNormalized(this.videoEl, t.clientX, t.clientY);
          this._updateCursor(pt.x, pt.y);
          this.sendInput({
            type: "mouse_button",
            button: 0,
            down: true,
            x: this.cursor.x,
            y: this.cursor.y,
          });
        } else if (this._touchStartTime - this._lastTapAt < 260) {
          this._doubleTapDragging = true;
          this.sendInput({
            type: "mouse_button",
            button: 0,
            down: true,
            x: this.cursor.x,
            y: this.cursor.y,
          });
        }
      }
    };

    const onTouchMove = (e) => {
      if (!this.enabled) return;
      e.preventDefault();

      if (this._twoFinger && e.touches.length >= 2) {
        const t0 = e.touches[0];
        const t1 = e.touches[1];
        const midX = (t0.clientX + t1.clientX) / 2;
        const midY = (t0.clientY + t1.clientY) / 2;
        const dx = midX - this._lastMid.x;
        const dy = midY - this._lastMid.y;
        const dist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
        const distDelta = Math.abs(dist - this._pinchStartDist);
        const ratio = this._pinchStartDist > 8 ? dist / this._pinchStartDist : 1;
        if (Math.hypot(dx, dy) > 4 || distDelta > 6) this._moved2 = true;
        if (this._pinchMode === null) {
          if (this.zoom > 1.02 || distDelta > 10 || Math.abs(ratio - 1) > 0.08) {
            this._pinchMode = "zoom_pan";
          } else if (Math.hypot(dx, dy) > 8) {
            this._pinchMode = "scroll";
          }
        }
        if (this._pinchMode === "zoom_pan") {
          const stepScale = this._pinchLastDist > 8 && dist > 8 ? dist / this._pinchLastDist : 1;
          this.setZoom(this.zoom * stepScale, this._lastMid.x, this._lastMid.y, dx, dy);
          this._pinchLastDist = dist;
          this._lastMid = { x: midX, y: midY };
          return;
        }
        this._pinchLastDist = dist;
        this._lastMid = { x: midX, y: midY };
        if (this._pinchMode === "scroll") {
          this.sendInput({ type: "mouse_wheel", dx: -dx * 0.04, dy: -dy * 0.04 });
        }
        return;
      }

      if (!this._twoFinger && e.touches.length === 1) {
        const t = e.touches[0];
        if (Math.hypot(t.clientX - this._touchStart.x, t.clientY - this._touchStart.y) > 5) {
          this._moved1 = true;
        }

        if (this.inputMode === "touch") {
          const pt = clientToNormalized(this.videoEl, t.clientX, t.clientY);
          this._updateCursor(pt.x, pt.y);
          this.sendInput({ type: "mouse_move", x: this.cursor.x, y: this.cursor.y });
        } else {
          const box = getVideoContentRect(this.videoEl);
          const dx = ((t.clientX - this._touchLast.x) / Math.max(1, box.width)) * 1.45;
          const dy = ((t.clientY - this._touchLast.y) / Math.max(1, box.height)) * 1.45;
          this._updateCursor(this.cursor.x + dx, this.cursor.y + dy);
          this.sendInput({ type: "mouse_move", x: this.cursor.x, y: this.cursor.y });
        }
        this._touchLast = { x: t.clientX, y: t.clientY };
      }
    };

    const onTouchEnd = (e) => {
      if (!this.enabled) return;
      e.preventDefault();

      if (this._twoFinger) {
        if (e.touches.length === 0) {
          if (!this._moved2) {
            // Two-finger tap -> Right click
            this.sendInput({
              type: "mouse_button",
              button: 2,
              down: true,
              x: this.cursor.x,
              y: this.cursor.y,
            });
            this.sendInput({
              type: "mouse_button",
              button: 2,
              down: false,
              x: this.cursor.x,
              y: this.cursor.y,
            });
          }
          this._twoFinger = false;
          this._pinchMode = null;
        }
        return;
      }

      if (e.touches.length === 0) {
        if (this.inputMode === "touch") {
          this.sendInput({
            type: "mouse_button",
            button: 0,
            down: false,
            x: this.cursor.x,
            y: this.cursor.y,
          });
        } else if (this._doubleTapDragging) {
          this._doubleTapDragging = false;
          this.sendInput({
            type: "mouse_button",
            button: 0,
            down: false,
            x: this.cursor.x,
            y: this.cursor.y,
          });
        } else if (!this._moved1 && performance.now() - this._touchStartTime < 280) {
          this.sendInput({
            type: "mouse_button",
            button: 0,
            down: true,
            x: this.cursor.x,
            y: this.cursor.y,
          });
          this.sendInput({
            type: "mouse_button",
            button: 0,
            down: false,
            x: this.cursor.x,
            y: this.cursor.y,
          });
          this._lastTapAt = performance.now();
        }
      }
    };

    const onMouseEnter = () => {
      const activeTag = document.activeElement ? document.activeElement.tagName : "";
      if (!["INPUT", "TEXTAREA"].includes(activeTag)) {
        this.stageEl.focus({ preventScroll: true });
      }
    };

    const onContextMenu = (e) => {
      if (this.enabled) e.preventDefault();
    };

    const onWheel = (e) => {
      if (!this.enabled) return;
      if (e.ctrlKey) {
        e.preventDefault();
        const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
        this.setZoom(this.zoom * factor, e.clientX, e.clientY);
        return;
      }
      if (this.videoEl.classList.contains("fit-original") && e.altKey) {
        return;
      }
      e.preventDefault();
      const step = e.deltaMode === 1 ? 1 / 3 : 1 / 100;
      this.sendInput({
        type: "mouse_wheel",
        dx: e.deltaX * step,
        dy: e.deltaY * step,
      });
    };

    const onKeyDown = (e) => {
      if (!this.enabled) return;
      const tag = document.activeElement ? document.activeElement.tagName : "";
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) {
        return;
      }
      e.preventDefault();
      this.sendInput({
        type: "key_event",
        code: e.code,
        key: e.key,
        down: true,
      });
    };

    const onKeyUp = (e) => {
      if (!this.enabled) return;
      const tag = document.activeElement ? document.activeElement.tagName : "";
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) {
        return;
      }
      e.preventDefault();
      this.sendInput({
        type: "key_event",
        code: e.code,
        key: e.key,
        down: false,
      });
    };

    const onBlur = () => {
      this._dragging = false;
      this._doubleTapDragging = false;
      this.sendInput({ type: "release_all_keys" });
    };

    this._listen(this.videoEl, "mousemove", onVideoMouseMove);
    this._listen(this.videoEl, "mouseenter", onMouseEnter);
    this._listen(this.videoEl, "mousedown", onMouseDown);
    this._listen(this.videoEl, "touchstart", onTouchStart, { passive: false });
    this._listen(this.videoEl, "touchmove", onTouchMove, { passive: false });
    this._listen(this.videoEl, "touchend", onTouchEnd, { passive: false });
    this._listen(this.videoEl, "touchcancel", onTouchEnd, { passive: false });
    this._listen(this.videoEl, "contextmenu", onContextMenu);
    this._listen(this.videoEl, "wheel", onWheel, { passive: false });
    this._listen(window, "mousemove", onWindowMouseMove);
    this._listen(window, "mouseup", onWindowMouseUp);
    this._listen(window, "keydown", onKeyDown);
    this._listen(window, "keyup", onKeyUp);
    this._listen(window, "blur", onBlur);

    this.onZoomChange(this.zoom);
    this.onCursorMove(this.cursor.x, this.cursor.y, this.inputMode);
  }

  _listen(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    this._bound.push([target, type, fn, opts]);
  }

  detach() {
    this._dragging = false;
    this._doubleTapDragging = false;
    this.resetZoom();
    this.sendInput({ type: "release_all_keys" });
    for (const [target, type, fn, opts] of this._bound) {
      target.removeEventListener(type, fn, opts);
    }
    this._bound = [];
  }
}
