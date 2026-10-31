"""Focus detection engine (logic from the original FocusLab project, optimised for proctoring).

Changes vs. original: one face detection pass per frame (was two), frames are downscaled
before detection, face count is reported (multi-person detection) and a fast overlay path.
"""
from dataclasses import dataclass, field
from pathlib import Path
import pickle

import cv2
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

MODEL_PATH = Path("data") / "focus_model.pkl"
DETECT_WIDTH = 320  # frames are downscaled to this width for detection (big speed-up)


@dataclass
class AnalysisResult:
    focus_score: float
    state: str
    faces: int
    signals: dict
    boxes: list = field(default_factory=list)  # in ORIGINAL frame coordinates


class FocusModel:
    """Optional learned classifier trained from one focused + one distracted example image."""

    def __init__(self, classifier=None, trained=False):
        self.classifier = classifier
        self.trained = trained

    @staticmethod
    def _augment(features):
        features = np.asarray(features, dtype=np.float32)
        variations = [features, features * [0.98, 0.95, 1.02], features * [1.02, 1.05, 0.98]]
        return np.clip(np.vstack(variations), 0, 1)

    @classmethod
    def train(cls, focused_bytes, distracted_bytes):
        engine = FocusEngine()
        imgs = [cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR) for b in (focused_bytes, distracted_bytes)]
        if any(i is None for i in imgs):
            raise ValueError("Both uploads must be valid images.")
        feats = [engine.feature_vector(cv2.cvtColor(i, cv2.COLOR_BGR2RGB)) for i in imgs]
        x = np.vstack([cls._augment(feats[0]), cls._augment(feats[1])])
        y = np.array([1] * 3 + [0] * 3)
        clf = make_pipeline(StandardScaler(), LogisticRegression(random_state=7, max_iter=500)).fit(x, y)
        model = cls(clf, trained=True)
        MODEL_PATH.parent.mkdir(exist_ok=True)
        with MODEL_PATH.open("wb") as f:
            pickle.dump(model, f)
        return model

    @classmethod
    def load(cls):
        try:
            with MODEL_PATH.open("rb") as f:
                model = pickle.load(f)
            return model if isinstance(model, cls) else cls()
        except Exception:
            return cls()

    @staticmethod
    def delete():
        MODEL_PATH.unlink(missing_ok=True)

    def predict(self, features):
        if not self.trained or self.classifier is None:
            return None
        return float(self.classifier.predict_proba([features])[0, 1])


class FocusEngine:
    def __init__(self):
        base = Path(cv2.data.haarcascades)
        self.face = cv2.CascadeClassifier(str(base / "haarcascade_frontalface_default.xml"))
        self.eyes = cv2.CascadeClassifier(str(base / "haarcascade_eye_tree_eyeglasses.xml"))
        self.last_signals = {}

    def _detect(self, rgb):
        """Return (gray_small, faces_small, scale) using a downscaled frame."""
        h, w = rgb.shape[:2]
        scale = DETECT_WIDTH / w if w > DETECT_WIDTH else 1.0
        small = cv2.resize(rgb, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA) if scale != 1.0 else rgb
        gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY) if small.ndim == 3 else small
        faces = self.face.detectMultiScale(gray, 1.15, 5, minSize=(30, 30))
        return gray, faces, scale

    def _signals(self, gray, faces):
        face_score = min(1.0, len(faces))
        eye_score = posture_score = 0.0
        if len(faces):
            x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
            roi = gray[y:y + int(h * .65), x:x + w]
            eyes = self.eyes.detectMultiScale(roi, 1.1, 4, minSize=(8, 8))
            eye_score = min(1.0, len(eyes) / 2)
            offset = abs((x + w / 2) - gray.shape[1] / 2) / max(1, gray.shape[1] / 2)
            posture_score = max(0.0, 1.0 - offset)
        return np.array([face_score, eye_score, posture_score], dtype=np.float32)

    def feature_vector(self, frame):
        gray, faces, _ = self._detect(frame)
        return self._signals(gray, faces)

    def analyze(self, frame, sensitivity=0.65, model=None):
        gray, faces, scale = self._detect(frame)
        features = self._signals(gray, faces)
        signals = dict(zip(("face", "eyes", "posture"), map(float, features)))
        self.last_signals = signals
        learned = model.predict(features) if model else None
        heuristic = float(np.dot(features, [0.35, 0.4, 0.25]))
        prob = learned if learned is not None else heuristic
        score = float(np.clip(prob * 100 * (0.85 + 0.15 * sensitivity), 0, 100))
        state = "Deep focus" if score >= 72 else "Settling in" if score >= 42 else "Needs attention"
        boxes = [tuple(int(v / scale) for v in f) for f in faces]
        return AnalysisResult(score, state, len(faces), signals, boxes)

    @staticmethod
    def draw(frame, result):
        """Cheap overlay on the full-resolution frame (in place)."""
        if result is None:
            return frame
        for x, y, w, h in result.boxes:
            cv2.rectangle(frame, (x, y), (x + w, y + h), (91, 211, 137), 3)
            cv2.putText(frame, f"{result.focus_score:.0f}% focus", (x, max(28, y - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, .7, (91, 211, 137), 2)
        if result.faces > 1:
            cv2.putText(frame, "MULTIPLE FACES", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, .9, (255, 80, 80), 2)
        elif result.faces == 0:
            cv2.putText(frame, "NO FACE DETECTED", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, .9, (255, 80, 80), 2)
        return frame
