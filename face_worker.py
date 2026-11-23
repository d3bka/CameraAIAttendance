"""AI face-recognition worker for ordinary classroom IP cameras (RTSP / webcam / video file).

Runs unattended: ONE process watches ALL cameras of a school (one thread per camera) and
connects to each camera only during its lessons, taken live from the platform schedule.
Pipeline per frame:
  latest RTSP frame -> YuNet face detector -> quality gates (size, blur)
  -> SFace embedding -> cosine match against the school's enrolled photos
  -> temporal confirmation (N hits in a time window) -> POST /api/ingest (face_match)
Every minute each camera sends a heartbeat, so the platform knows when a camera was blind
and reports "no data" instead of wrongly marking a class absent.

Privacy: frames and face crops are never written to disk; face templates live in memory only.

    pip install -r requirements-ai.txt
    python face_worker.py --download-models
    python face_worker.py --config data/cameras.json            # all cameras, schedule-driven
    python face_worker.py --camera-key room201 --source 0 --show # quick test with a webcam
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import logging
import os
import statistics
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    import cv2
    import numpy as np
    import requests
except ImportError:  # pragma: no cover
    raise SystemExit('Install AI worker requirements: pip install -r requirements-ai.txt')

LOG = logging.getLogger('face_worker')
ROOT = Path(__file__).resolve().parent
MODELS = {
    'detector': ('face_detection_yunet_2023mar.onnx',
                 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx',
                 '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'),
    'recognizer': ('face_recognition_sface_2021dec.onnx',
                   'https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx',
                   '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79'),
}


# Optional second-opinion model (ArcFace R50, InsightFace "buffalo_l" pack).
# LICENSE: InsightFace pretrained models are for NON-COMMERCIAL research use; obtain a commercial
# licence or swap in another 512-d ONNX face model (same 112x112 aligned input) before selling the product.
VERIFIER = ('w600k_r50.onnx', 'https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip',
            '4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43')
VERIFIER_ACCEPT = 0.40   # ArcFace cosine: same person with high probability
VERIFIER_REJECT = 0.20   # ArcFace cosine: almost certainly a different person -> veto


def redact(url: str) -> str:
    try:
        parts = urlsplit(url)
        return f'{parts.scheme}://{parts.hostname or "?"}{":" + str(parts.port) if parts.port else ""}{parts.path}'
    except ValueError:
        return '<source>'


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def ensure_models(models_dir: Path, download=False) -> dict:
    models_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, (name, url, digest) in MODELS.items():
        path = models_dir / name
        if not path.exists():
            if not download:
                raise SystemExit(f'Model {name} not found. Run: python face_worker.py --download-models')
            LOG.info('Downloading %s ...', name)
            tmp = path.with_suffix('.part')
            with requests.get(url, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with tmp.open('wb') as fh:
                    for chunk in resp.iter_content(1 << 16):
                        fh.write(chunk)
            tmp.replace(path)
        actual = file_sha256(path)
        if actual != digest:
            path.unlink(missing_ok=True)
            raise SystemExit(f'Checksum mismatch for {name}; file removed. Re-run --download-models.')
        paths[key] = path
    return paths


# --------------------------------------------------------------------------- vision engine
class FaceEngine:
    def __init__(self, paths: dict, det_score=0.75):
        self.detector = cv2.FaceDetectorYN.create(str(paths['detector']), '', (320, 320), det_score, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(paths['recognizer']), '')

    def detect(self, image, max_width=None):
        """Return faces as an (N, 15) array in ORIGINAL image coordinates."""
        h, w = image.shape[:2]
        scale = 1.0
        work = image
        if max_width and w > max_width:
            scale = max_width / w
            work = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        self.detector.setInputSize((work.shape[1], work.shape[0]))
        _, faces = self.detector.detect(work)
        if faces is None:
            return np.zeros((0, 15), dtype=np.float32)
        faces = faces.copy()
        faces[:, :14] /= scale
        return faces

    def embed(self, image, face):
        aligned = self.recognizer.alignCrop(image, face)
        feature = self.recognizer.feature(aligned).flatten().astype(np.float32)
        norm = float(np.linalg.norm(feature))
        return (feature / norm if norm else feature), aligned


class Verifier:
    """Second, stronger face model. Runs only on faces the main model already matched to a child,
    so CPU cost stays low. ONNX Runtime sessions are safe to share between camera threads."""

    def __init__(self, path: Path):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(path), opts, providers=['CPUExecutionProvider'])
        self.input = self.session.get_inputs()[0].name

    def embed(self, aligned_bgr):
        rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB).astype(np.float32)
        blob = ((rgb - 127.5) / 127.5).transpose(2, 0, 1)[None]
        feature = self.session.run(None, {self.input: blob})[0][0].astype(np.float32)
        norm = float(np.linalg.norm(feature))
        return feature / norm if norm else feature


def load_verifier(models_dir: Path, path=None, download=False, disabled=False):
    """Return a Verifier or None (the worker runs fine without it)."""
    if disabled:
        return None
    name, url, digest = VERIFIER
    target = Path(path) if path else models_dir / name
    if not target.exists():
        if not download:
            LOG.info('Second-opinion model not installed (optional): python face_worker.py --download-verifier')
            return None
        LOG.info('Downloading second-opinion model pack (~290 MB, non-commercial licence) ...')
        models_dir.mkdir(parents=True, exist_ok=True)
        tmp = models_dir / 'buffalo_l.zip.part'
        with requests.get(url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with tmp.open('wb') as fh:
                for chunk in resp.iter_content(1 << 20):
                    fh.write(chunk)
        with zipfile.ZipFile(tmp) as zf:
            member = next(m for m in zf.namelist() if m.endswith(name))
            target.write_bytes(zf.read(member))
        tmp.unlink(missing_ok=True)
    if not path and file_sha256(target) != digest:
        target.unlink(missing_ok=True)
        raise SystemExit(f'Checksum mismatch for {name}; file removed. Re-run --download-verifier.')
    try:
        verifier = Verifier(target)
    except ImportError:
        LOG.warning('onnxruntime is not installed; second-opinion model disabled (pip install onnxruntime)')
        return None
    LOG.info('Second-opinion model enabled: %s (accept >= %.2f, veto < %.2f)', target.name, VERIFIER_ACCEPT, VERIFIER_REJECT)
    return verifier


def blur_score(crop) -> float:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def enrollment_quality(faces, min_face_px=60):
    """Pick the face to enrol from a reference photo. Returns (status, index|None)."""
    if len(faces) == 0:
        return 'no_face', None
    areas = faces[:, 2] * faces[:, 3]
    order = np.argsort(areas)[::-1]
    best = int(order[0])
    if len(faces) > 1 and areas[order[1]] * 2.0 > areas[best]:
        return 'multiple_faces', None
    if faces[best, 2] < min_face_px:
        return 'too_small', None
    return 'ok', best


# --------------------------------------------------------------------------- matching logic (pure numpy)
class Gallery:
    """Enrolled face templates of one school (several photos per child)."""

    def __init__(self):
        self.features = np.zeros((0, 128), dtype=np.float32)
        self.codes: list[str] = []
        self.second: dict[str, np.ndarray] = {}   # student code -> stacked second-model templates
        self.by_sha: dict[str, tuple] = {}

    def rebuild(self, entries):
        """entries: iterable of (student_code, feature or None[, second_feature or None])."""
        feats, codes, second = [], [], {}
        for entry in entries:
            code, feat = entry[0], entry[1]
            feat2 = entry[2] if len(entry) > 2 else None
            if feat is not None:
                feats.append(feat)
                codes.append(code)
                if feat2 is not None:
                    second.setdefault(code, []).append(feat2)
        self.features = np.vstack(feats).astype(np.float32) if feats else np.zeros((0, 128), dtype=np.float32)
        self.codes = codes
        self.second = {code: np.vstack(v).astype(np.float32) for code, v in second.items()}

    def verify(self, code, feature2):
        """Second-model score of this face against the child's own templates (None if unavailable)."""
        templates = self.second.get(code)
        if templates is None or feature2 is None:
            return None
        return float((templates @ feature2).max())

    @property
    def students(self):
        return len(set(self.codes))

    def match(self, feature, margin=0.04):
        """Best student and score; None if ambiguous between two different children."""
        if not len(self.codes):
            return None
        scores = self.features @ feature
        best_per_code: dict[str, float] = {}
        for code, score in zip(self.codes, scores.tolist()):
            if score > best_per_code.get(code, -1.0):
                best_per_code[code] = score
        ranked = sorted(best_per_code.items(), key=lambda kv: kv[1], reverse=True)
        code, best = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        if best - second < margin:
            return None
        return code, best, second


class Confirmer:
    """Require several consistent sightings before reporting a child (filters one-frame mistakes)."""

    def __init__(self, min_hits=3, window=20.0, cooldown=300.0):
        self.min_hits, self.window, self.cooldown = min_hits, window, cooldown
        self.hits: dict[str, collections.deque] = {}
        self.last_sent: dict[str, float] = {}

    def observe(self, code: str, score: float, now: float, second: float | None = None):
        """Returns None, or (median main score, median second-model score or None) when confirmed."""
        q = self.hits.setdefault(code, collections.deque())
        q.append((now, score, second))
        while q and now - q[0][0] > self.window:
            q.popleft()
        if len(q) < self.min_hits:
            return None
        if now - self.last_sent.get(code, -1e9) < self.cooldown:
            return None
        self.last_sent[code] = now
        confidence = statistics.median(s for _, s, _ in q)
        seconds = [s2 for _, _, s2 in q if s2 is not None]
        q.clear()
        return confidence, (statistics.median(seconds) if seconds else None)


# --------------------------------------------------------------------------- I/O
class FrameGrabber(threading.Thread):
    """Continuously reads the stream and keeps only the newest frame (no RTSP lag on slow CPUs)."""

    def __init__(self, source):
        super().__init__(daemon=True)
        self.source = int(source) if str(source).isdigit() else source
        self.is_file = isinstance(self.source, str) and Path(self.source).is_file()
        self.lock = threading.Lock()
        self.frame = None
        self.frame_time = 0.0
        self.frames = 0
        self.stop = threading.Event()

    def latest(self):
        with self.lock:
            return self.frame, self.frame_time

    def run(self):
        while not self.stop.is_set():
            cap = cv2.VideoCapture(self.source)
            if not cap.isOpened():
                LOG.warning('Cannot open video source; retrying in 5 s')
                self.stop.wait(5)
                continue
            fps = cap.get(cv2.CAP_PROP_FPS) or 25
            LOG.info('Video source connected')
            try:
                while not self.stop.is_set():
                    ok, frame = cap.read()
                    if not ok:
                        break
                    with self.lock:
                        self.frame, self.frame_time = frame, time.monotonic()
                        self.frames += 1
                    if self.is_file:
                        time.sleep(1.0 / max(1.0, fps))
            finally:
                cap.release()
            if self.is_file:
                LOG.info('Video file finished')
                self.stop.set()
                return
            LOG.warning('Video stream lost; reconnecting in 5 s')
            self.stop.wait(5)


class Platform:
    """HTTP client for one thread (requests.Session is not shared between threads)."""

    def __init__(self, api: str, token: str, camera_key: str = ''):
        self.api = api.rstrip('/')
        self.camera_key = camera_key
        self.session = requests.Session()
        self.session.headers['X-Ingest-Token'] = token
        self.outbox: collections.deque = collections.deque(maxlen=2000)

    def get_json(self, path):
        r = self.session.get(self.api + path, timeout=15)
        r.raise_for_status()
        return r.json()

    def roster(self):
        return self.get_json('/api/worker/roster')

    def schedule(self):
        return self.get_json('/api/worker/schedule')

    def photo(self, pid):
        r = self.session.get(f'{self.api}/api/worker/photos/{pid}', timeout=30)
        r.raise_for_status()
        return r.content

    def photo_quality(self, pid, status):
        try:
            self.session.post(f'{self.api}/api/worker/photos/{pid}/quality', json={'face_status': status}, timeout=10)
        except requests.RequestException:
            pass

    def heartbeat(self, ok, stats, idle=False):
        try:
            r = self.session.post(self.api + '/api/worker/heartbeat',
                                  json={'camera_key': self.camera_key, 'ok': ok, 'idle': idle, **stats}, timeout=10)
            if r.status_code >= 400:
                LOG.warning('[%s] heartbeat rejected: HTTP %s %s', self.camera_key, r.status_code, r.text[:200])
        except requests.RequestException as exc:
            LOG.warning('[%s] heartbeat failed: %s', self.camera_key, type(exc).__name__)

    def send_match(self, code, confidence, second=None):
        event = {'camera_key': self.camera_key, 'event_uid': 'face-' + uuid.uuid4().hex,
                 'event_type': 'face_match', 'verification_method': 'ai_face', 'person_code': code,
                 'confidence': round(float(confidence), 4), 'observed_at': datetime.now(timezone.utc).isoformat()}
        if second is not None:
            event['second_score'] = round(min(1.0, max(0.0, float(second))), 4)
        self.outbox.append(event)
        self.flush()

    def flush(self):
        """Deliver queued events; keep them on network failure (events are idempotent by event_uid)."""
        while self.outbox:
            event = self.outbox[0]
            try:
                r = self.session.post(self.api + '/api/ingest', json=event, timeout=10)
            except requests.RequestException:
                LOG.warning('[%s] platform unreachable; %d event(s) queued', self.camera_key, len(self.outbox))
                return
            self.outbox.popleft()
            if r.status_code >= 400:
                LOG.warning('[%s] event rejected: HTTP %s %s', self.camera_key, r.status_code, r.text[:200])
            else:
                body = r.json()
                second = f", 2nd model {event['second_score']:.2f}" if 'second_score' in event else ''
                LOG.info('[%s] recognised %s (%.2f%s) -> %s', self.camera_key, event['person_code'], event['confidence'],
                         second, 'confirmed' if body.get('verified') else body.get('review_status') or body.get('reason'))


class SharedGallery:
    """Gallery used by all camera threads; rebuilt atomically by the sync thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.gallery = Gallery()
        self.review_threshold = 0.363
        self.verifier: Verifier | None = None

    def match(self, feature, margin):
        with self.lock:
            return self.gallery.match(feature, margin), self.review_threshold

    def verify(self, code, feature2):
        with self.lock:
            return self.gallery.verify(code, feature2)


def sync_gallery(engine: FaceEngine, platform: Platform, shared: SharedGallery, min_face_px=60):
    roster = platform.roster()
    entries = []
    cache = shared.gallery.by_sha
    for student in roster['students']:
        for photo in student['photos']:
            cached = cache.get(photo['sha256'])
            if cached is None:
                feature, feature2, status = None, None, 'unreadable'
                try:
                    data = np.frombuffer(platform.photo(photo['id']), dtype=np.uint8)
                    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
                    if image is not None:
                        faces = engine.detect(image, max_width=1280)
                        status, idx = enrollment_quality(faces, min_face_px)
                        if idx is not None:
                            feature, aligned = engine.embed(image, faces[idx])
                            if shared.verifier is not None:
                                feature2 = shared.verifier.embed(aligned)
                except requests.RequestException as exc:
                    LOG.warning('Photo download failed: %s', type(exc).__name__)
                    continue
                cached = (status, feature, feature2)
                if status != photo.get('face_status'):
                    platform.photo_quality(photo['id'], status)
            entries.append((student['code'], photo['sha256'], cached))
    fresh = Gallery()
    fresh.by_sha = {sha: cached for _, sha, cached in entries}
    fresh.rebuild((code, cached[1], cached[2] if len(cached) > 2 else None) for code, _, cached in entries)
    with shared.lock:
        shared.gallery = fresh
        shared.review_threshold = roster['face_review_threshold']
    LOG.info('Gallery: %d template(s) for %d student(s)', len(fresh.codes), fresh.students)
    return roster


class Schedule:
    """Today's watch windows per camera, refreshed from the platform (lessons can change any time)."""

    def __init__(self, always_on=False):
        self.always_on = always_on
        self.lock = threading.Lock()
        self.windows: dict[str, list] = {}
        self.loaded = False

    def update(self, data):
        parsed = {key: [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in spans]
                  for key, spans in data['windows'].items()}
        with self.lock:
            self.windows, self.loaded = parsed, True

    def active(self, camera_key, now=None):
        if self.always_on:
            return True
        now = now or datetime.now(timezone.utc)
        with self.lock:
            if not self.loaded:
                return True  # platform unreachable at start: better to watch than to miss a lesson
            return any(a <= now <= b for a, b in self.windows.get(camera_key, []))

    def next_start(self, camera_key, now=None):
        now = now or datetime.now(timezone.utc)
        with self.lock:
            upcoming = [a for a, _ in self.windows.get(camera_key, []) if a > now]
        return min(upcoming) if upcoming else None


class CameraWorker(threading.Thread):
    """Watches one camera: connects only inside its schedule window, recognises, reports, heartbeats."""

    def __init__(self, camera_key, source, paths, token, shared, schedule, args, stop_event):
        super().__init__(daemon=True, name=f'cam-{camera_key}')
        self.camera_key, self.source, self.paths = camera_key, source, paths
        self.shared, self.schedule, self.args, self.stop_event = shared, schedule, args, stop_event
        self.platform = Platform(args.api, token, camera_key)
        self.confirmer = Confirmer(args.min_hits, args.window, args.cooldown)
        self.stats = collections.Counter()
        self.finished = False  # a video file reached its end

    def run(self):
        try:
            self._loop()
        except Exception:  # never let one broken camera kill the whole worker silently
            LOG.exception('[%s] camera thread crashed', self.camera_key)
        finally:
            self.finished = True

    def _loop(self):
        args = self.args
        engine = FaceEngine(self.paths, det_score=args.det_score)  # OpenCV DNN objects are per-thread
        grabber = None
        last_frames, last_heartbeat, last_frame_time = 0, time.monotonic(), 0.0
        period = 1.0 / args.fps
        src = str(self.source)
        src_label = 'webcam' if src.isdigit() else ('video file' if Path(src).is_file() else redact(src))
        try:
            while not self.stop_event.is_set():
                loop_start = time.monotonic()
                active = self.schedule.active(self.camera_key)
                if active and grabber is None:
                    LOG.info('[%s] lesson window open -> connecting to %s', self.camera_key, src_label)
                    grabber = FrameGrabber(self.source)
                    grabber.start()
                    last_frames = 0
                elif not active and grabber is not None:
                    nxt = self.schedule.next_start(self.camera_key)
                    LOG.info('[%s] window closed -> camera released%s', self.camera_key,
                             f'; next at {nxt.astimezone().strftime("%H:%M")}' if nxt else '; no more lessons today')
                    grabber.stop.set()
                    grabber = None
                if grabber is not None:
                    frame, frame_time = grabber.latest()
                    if frame is not None and frame_time != last_frame_time:
                        last_frame_time = frame_time
                        self.process(engine, frame)
                    if grabber.is_file and grabber.stop.is_set():
                        LOG.info('[%s] video file finished', self.camera_key)
                        return
                now = time.monotonic()
                if now - last_heartbeat >= 60:
                    if grabber is not None:
                        frames = grabber.frames - last_frames
                        last_frames = grabber.frames
                        self.platform.heartbeat(frames > 0, self.heartbeat_stats(now - last_heartbeat, frames))
                        self.warn_small_faces()
                    else:
                        self.platform.heartbeat(True, {}, idle=True)
                    self.stats.clear()
                    last_heartbeat = now
                    self.platform.flush()
                sleep = period if grabber is not None else 5.0
                self.stop_event.wait(max(0.0, sleep - (time.monotonic() - loop_start)))
        finally:
            if grabber is not None:
                grabber.stop.set()
            self.platform.flush()

    def process(self, engine, frame):
        args = self.args
        self.stats['processed'] += 1
        faces = engine.detect(frame, max_width=args.det_width)
        self.stats['faces'] += len(faces)
        for face in faces:
            if face[2] < args.min_face:
                self.stats['small_faces'] += 1
                continue
            feature, crop = engine.embed(frame, face)
            if blur_score(crop) < args.min_sharpness:
                self.stats['blurry'] += 1
                continue
            found, review_threshold = self.shared.match(feature, args.margin)
            if found is None:
                self.stats['ambiguous'] += 1
                continue
            code, score, _ = found
            if score < review_threshold:
                self.stats['unknown'] += 1
                continue
            second = None
            if self.shared.verifier is not None:
                second = self.shared.verify(code, self.shared.verifier.embed(crop))
                if second is not None and second < VERIFIER_REJECT:
                    self.stats['vetoed'] += 1  # the stronger model says: not this child
                    continue
            confirmed = self.confirmer.observe(code, score, time.monotonic(), second)
            if confirmed is not None:
                self.stats['matches'] += 1
                self.platform.send_match(code, *confirmed)
        if args.show:
            preview = frame.copy()
            for face in faces:
                x, y, w, h = face[:4].astype(int)
                cv2.rectangle(preview, (x, y), (x + w, y + h), (0, 255, 0) if w >= args.min_face else (0, 0, 255), 2)
            cv2.imshow(f'{self.camera_key} (q = quit)', preview)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                self.stop_event.set()

    def heartbeat_stats(self, elapsed, frames):
        with self.shared.lock:
            gallery = self.shared.gallery.students
        return {'frames': frames, 'faces': self.stats['faces'], 'matches': self.stats['matches'],
                'small_faces': self.stats['small_faces'], 'gallery': gallery,
                'fps': round(self.stats['processed'] / max(elapsed, 1e-6), 2)}

    def warn_small_faces(self):
        if self.stats['faces'] > 20 and self.stats['small_faces'] > self.stats['faces'] * 0.5:
            LOG.warning('[%s] most faces are too small (<%d px): use the main 1080p stream, not the sub stream, '
                        'or move/zoom the camera closer to the desks.', self.camera_key, self.args.min_face)


def load_cameras(args):
    """Camera list from --config JSON (all cameras of a school) or the single --camera-key/--source pair."""
    if args.config:
        try:
            config = json.loads(Path(args.config).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise SystemExit(f'Cannot read config {args.config}: {exc}')
        cameras = config.get('cameras')
        if not isinstance(cameras, dict) or not cameras:
            raise SystemExit('Config must contain "cameras": {"camera_key": "rtsp://..."}')
        if config.get('api'):
            args.api = config['api']
        token = os.environ.get('ATTENDANCE_INGEST_TOKEN') or config.get('token')
        return {str(k): str(v) for k, v in cameras.items()}, token
    source = args.source or os.environ.get('ATTENDANCE_RTSP_URL')
    if not source and not args.check_photos:
        raise SystemExit('Use --config cameras.json, or --camera-key with ATTENDANCE_RTSP_URL / --source (0 = webcam)')
    return ({args.camera_key: source} if args.camera_key else {}), os.environ.get('ATTENDANCE_INGEST_TOKEN')


def run(args):
    cameras, token = load_cameras(args)
    if not token:
        raise SystemExit('Set ATTENDANCE_INGEST_TOKEN (school device token from the admin panel) or "token" in the config')
    paths = ensure_models(Path(args.models_dir), download=args.download_models)
    engine = FaceEngine(paths, det_score=args.det_score)
    control = Platform(args.api, token, 'control')
    shared = SharedGallery()
    shared.verifier = load_verifier(Path(args.models_dir), args.verifier, args.download_verifier, args.no_verifier)
    sync_gallery(engine, control, shared, args.enroll_min_face)
    if args.check_photos:
        LOG.info('Photo check finished; see statuses in the admin panel')
        return
    if not shared.gallery.codes:
        LOG.warning('No usable reference photos yet: upload clear frontal photos (2-5 per child)')
    schedule = Schedule(always_on=args.always_on)
    try:
        schedule.update(control.schedule())
    except requests.RequestException as exc:
        LOG.warning('Schedule unavailable (%s); cameras stay on until it loads', type(exc).__name__)
    unknown = [k for k in cameras if schedule.loaded and k not in schedule.windows]
    for key in unknown:
        LOG.warning('[%s] camera_key is not registered for this school in the panel; its events will be rejected', key)
    stop_event = threading.Event()
    workers = [CameraWorker(key, src, paths, token, shared, schedule, args, stop_event) for key, src in cameras.items()]
    for w in workers:
        w.start()
    LOG.info('Watching %d camera(s): %s%s', len(workers), ', '.join(cameras),
             ' (always on)' if args.always_on else ' (only during lessons)')
    last_sync = last_sched = time.monotonic()
    try:
        while not stop_event.is_set() and not all(w.finished for w in workers):
            stop_event.wait(5)
            now = time.monotonic()
            if now - last_sched >= args.schedule_every:
                try:
                    schedule.update(control.schedule())
                except requests.RequestException as exc:
                    LOG.warning('Schedule refresh failed: %s', type(exc).__name__)
                last_sched = now
            if now - last_sync >= args.sync_every:
                try:
                    sync_gallery(engine, control, shared, args.enroll_min_face)
                except requests.RequestException as exc:
                    LOG.warning('Gallery sync failed: %s', type(exc).__name__)
                last_sync = now
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        for w in workers:
            w.join(10)
        if args.show:
            cv2.destroyAllWindows()


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI face recognition worker for classroom IP cameras')
    parser.add_argument('--config', help='JSON: {"api": "...", "cameras": {"camera_key": "rtsp://..."}} - all cameras in one process')
    parser.add_argument('--camera-key', help='single camera mode: camera_key registered in the admin panel')
    parser.add_argument('--api', default='http://127.0.0.1:8080')
    parser.add_argument('--source', help='single camera mode: RTSP URL, webcam index (0) or video file; default $ATTENDANCE_RTSP_URL')
    parser.add_argument('--always-on', action='store_true', help='Ignore the schedule and watch cameras 24/7')
    parser.add_argument('--models-dir', default=str(ROOT / 'models'))
    parser.add_argument('--download-models', action='store_true', help='Download & verify models')
    parser.add_argument('--check-photos', action='store_true', help='Only validate enrolled photos and report their quality')
    parser.add_argument('--fps', type=float, default=2.0, help='Frames analysed per second per camera (CPU budget)')
    parser.add_argument('--det-width', type=int, default=1280, help='Max width for face detection (higher finds smaller faces)')
    parser.add_argument('--det-score', type=float, default=0.75)
    parser.add_argument('--min-face', type=int, default=40, help='Ignore faces narrower than this (pixels, full frame)')
    parser.add_argument('--enroll-min-face', type=int, default=60)
    parser.add_argument('--min-sharpness', type=float, default=15.0, help='Laplacian variance; lower = accept blurrier faces')
    parser.add_argument('--margin', type=float, default=0.04, help='Required score gap between best and 2nd-best child')
    parser.add_argument('--min-hits', type=int, default=3)
    parser.add_argument('--window', type=float, default=20.0)
    parser.add_argument('--cooldown', type=float, default=300.0)
    parser.add_argument('--sync-every', type=float, default=300.0, help='Seconds between photo/gallery refreshes')
    parser.add_argument('--schedule-every', type=float, default=120.0, help='Seconds between schedule refreshes')
    parser.add_argument('--show', action='store_true', help='Debug preview window (local use only)')
    parser.add_argument('--verifier', help='Path to a second-opinion 512-d ONNX face model (default: models/w600k_r50.onnx)')
    parser.add_argument('--download-verifier', action='store_true', help='Download ArcFace R50 (InsightFace, non-commercial licence)')
    parser.add_argument('--no-verifier', action='store_true', help='Disable the second-opinion model')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if (args.download_models or args.download_verifier) and not (args.camera_key or args.config):
        if args.download_models:
            ensure_models(Path(args.models_dir), download=True)
        if args.download_verifier:
            load_verifier(Path(args.models_dir), args.verifier, download=True)
        LOG.info('Models ready in %s', args.models_dir)
        return
    if not (args.camera_key or args.config or args.check_photos):
        parser.error('--config or --camera-key is required')
    if not 0.1 <= args.fps <= 15 or args.min_hits < 1 or args.window <= 0:
        parser.error('fps 0.1-15, min-hits >= 1, window > 0')
    run(args)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
