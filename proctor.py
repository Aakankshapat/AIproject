"""Live proctoring: WebRTC video processor + rule-based violation detector."""
import collections
import threading
import time

import av

from focus_engine import FocusEngine

ANALYZE_EVERY = 4  # analyse every Nth frame; other frames reuse the cached overlay (fast)


class ProctorProcessor:
    """Runs in the WebRTC worker thread. Never touches st.session_state."""

    def __init__(self, model=None, sensitivity=0.65):
        self.engine = FocusEngine()
        self.model = model
        self.sensitivity = sensitivity
        self.latest = None
        self.frame_count = 0
        self._samples = collections.deque(maxlen=600)
        self._lock = threading.Lock()

    def recv(self, frame):
        img = frame.to_ndarray(format="rgb24")
        self.frame_count += 1
        if self.frame_count % ANALYZE_EVERY == 1:
            self.latest = self.engine.analyze(img, self.sensitivity, self.model)
            with self._lock:
                self._samples.append((time.time(), self.latest))
        self.engine.draw(img, self.latest)
        return av.VideoFrame.from_ndarray(img, format="rgb24")

    def drain(self):
        with self._lock:
            out = list(self._samples)
            self._samples.clear()
        return out


class ViolationDetector:
    """Turns a stream of samples into violations (no face, multiple faces, sustained low focus)."""

    def __init__(self, cfg):
        self.no_face_s = float(cfg["no_face_seconds"])
        self.low_thr = float(cfg["low_focus_threshold"])
        self.low_s = float(cfg["low_focus_seconds"])
        self.cooldown = float(cfg["violation_cooldown"])
        self.no_face_since = None
        self.low_since = None
        self.last_fired = {}

    def _fire(self, kind, detail, ts, out):
        if ts - self.last_fired.get(kind, float('-inf')) >= self.cooldown:
            self.last_fired[kind] = ts
            out.append((ts, kind, detail))

    def feed(self, samples):
        out = []
        for ts, r in samples:
            if r.faces == 0:
                if self.no_face_since is None:
                    self.no_face_since = ts
                if ts - self.no_face_since >= self.no_face_s:
                    self._fire("No face", f"Face missing for {ts - self.no_face_since:.0f}s", ts, out)
            else:
                self.no_face_since = None
            if r.faces > 1:
                self._fire("Multiple faces", f"{r.faces} faces in frame", ts, out)
            if r.focus_score < self.low_thr and r.faces >= 1:
                if self.low_since is None:
                    self.low_since = ts
                if ts - self.low_since >= self.low_s:
                    self._fire("Low focus", f"Score {r.focus_score:.0f}% for {ts - self.low_since:.0f}s", ts, out)
            else:
                self.low_since = None
        return out


def aggregate(samples):
    """Collapse raw samples into one averaged row (stored once per second)."""
    n = len(samples)
    if not n:
        return None
    ts = samples[-1][0]
    rs = [r for _, r in samples]
    avg = lambda f: sum(f(r) for r in rs) / n
    return (ts, avg(lambda r: r.focus_score), avg(lambda r: r.signals["face"]),
            avg(lambda r: r.signals["eyes"]), avg(lambda r: r.signals["posture"]), max(r.faces for r in rs))
