"""AI face-recognition worker for ordinary IP cameras (RTSP / webcam / video file).

One process per camera. Pipeline:
  RTSP frame (latest only) -> YuNet face detector -> quality gates (size, blur)
  -> SFace embedding -> cosine match against the school's enrolled photos
  -> temporal confirmation (N hits in a time window) -> POST /api/ingest (face_match)
Every minute it sends a heartbeat, so the platform knows when a camera was blind and
reports "no data" instead of wrongly marking children absent.

Privacy: frames and face crops are never written to disk; face templates live in memory only.

    pip install -r requirements-ai.txt
    python face_worker.py --download-models
    $env:ATTENDANCE_INGEST_TOKEN="cat_..."            # school device token from the panel
    $env:ATTENDANCE_RTSP_URL="rtsp://user:pass@192.168.1.20:554/Streaming/Channels/101"
    python face_worker.py --camera-key school1_room201
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import logging
import os
import statistics
import sys
import threading
import time
import uuid
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
        self.by_sha: dict[str, tuple[str, np.ndarray | None]] = {}

    def rebuild(self, entries):
        """entries: iterable of (student_code, feature or None)."""
        feats, codes = [], []
        for code, feat in entries:
            if feat is not None:
                feats.append(feat)
                codes.append(code)
        self.features = np.vstack(feats).astype(np.float32) if feats else np.zeros((0, 128), dtype=np.float32)
        self.codes = codes

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

    def observe(self, code: str, score: float, now: float):
        q = self.hits.setdefault(code, collections.deque())
        q.append((now, score))
        while q and now - q[0][0] > self.window:
            q.popleft()
        if len(q) < self.min_hits:
            return None
        if now - self.last_sent.get(code, -1e9) < self.cooldown:
            return None
        self.last_sent[code] = now
        confidence = statistics.median(s for _, s in q)
        q.clear()
        return confidence


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
    def __init__(self, api: str, token: str, camera_key: str):
        self.api = api.rstrip('/')
        self.camera_key = camera_key
        self.session = requests.Session()
        self.session.headers['X-Ingest-Token'] = token
        self.outbox: collections.deque = collections.deque(maxlen=2000)

    def roster(self):
        r = self.session.get(self.api + '/api/worker/roster', timeout=15)
        r.raise_for_status()
        return r.json()

    def photo(self, pid):
        r = self.session.get(f'{self.api}/api/worker/photos/{pid}', timeout=30)
        r.raise_for_status()
        return r.content

    def photo_quality(self, pid, status):
        try:
            self.session.post(f'{self.api}/api/worker/photos/{pid}/quality', json={'face_status': status}, timeout=10)
        except requests.RequestException:
            pass

    def heartbeat(self, ok, stats):
        try:
            r = self.session.post(self.api + '/api/worker/heartbeat', json={'camera_key': self.camera_key, 'ok': ok, **stats}, timeout=10)
            if r.status_code >= 400:
                LOG.warning('Heartbeat rejected: HTTP %s %s', r.status_code, r.text[:200])
        except requests.RequestException as exc:
            LOG.warning('Heartbeat failed: %s', type(exc).__name__)

    def send_match(self, code, confidence):
        self.outbox.append({'camera_key': self.camera_key, 'event_uid': 'face-' + uuid.uuid4().hex,
                            'event_type': 'face_match', 'verification_method': 'ai_face', 'person_code': code,
                            'confidence': round(float(confidence), 4),
                            'observed_at': datetime.now(timezone.utc).isoformat()})
        self.flush()

    def flush(self):
        """Deliver queued events; keep them on network failure (events are idempotent by event_uid)."""
        while self.outbox:
            event = self.outbox[0]
            try:
                r = self.session.post(self.api + '/api/ingest', json=event, timeout=10)
            except requests.RequestException:
                LOG.warning('Platform unreachable; %d event(s) queued', len(self.outbox))
                return
            self.outbox.popleft()
            if r.status_code >= 400:
                LOG.warning('Event rejected: HTTP %s %s', r.status_code, r.text[:200])
            else:
                body = r.json()
                LOG.info('Recognised %s (%.2f) -> %s', event['person_code'], event['confidence'],
                         'confirmed' if body.get('verified') else body.get('review_status') or body.get('reason'))


def sync_gallery(engine: FaceEngine, platform: Platform, gallery: Gallery, min_face_px=60):
    roster = platform.roster()
    entries = []
    for student in roster['students']:
        for photo in student['photos']:
            cached = gallery.by_sha.get(photo['sha256'])
            if cached is None:
                feature, status = None, 'unreadable'
                try:
                    data = np.frombuffer(platform.photo(photo['id']), dtype=np.uint8)
                    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
                    if image is not None:
                        faces = engine.detect(image, max_width=1280)
                        status, idx = enrollment_quality(faces, min_face_px)
                        if idx is not None:
                            feature, _ = engine.embed(image, faces[idx])
                except requests.RequestException as exc:
                    LOG.warning('Photo download failed: %s', type(exc).__name__)
                    continue
                cached = (status, feature)
                gallery.by_sha[photo['sha256']] = cached
                if status != photo.get('face_status'):
                    platform.photo_quality(photo['id'], status)
            entries.append((student['code'], cached[1]))
    gallery.rebuild(entries)
    LOG.info('Gallery: %d template(s) for %d student(s)', len(gallery.codes), gallery.students)
    return roster


def run(args):
    token = os.environ.get('ATTENDANCE_INGEST_TOKEN')
    source = args.source or os.environ.get('ATTENDANCE_RTSP_URL')
    if not token:
        raise SystemExit('Set ATTENDANCE_INGEST_TOKEN (school device token from the admin panel)')
    if not source and not args.check_photos:
        raise SystemExit('Set ATTENDANCE_RTSP_URL or pass --source (0 = local webcam, or a video file)')
    paths = ensure_models(Path(args.models_dir), download=args.download_models)
    engine = FaceEngine(paths, det_score=args.det_score)
    platform = Platform(args.api, token, args.camera_key)
    gallery = Gallery()
    roster = sync_gallery(engine, platform, gallery, args.enroll_min_face)
    review_threshold = roster['face_review_threshold']
    if args.check_photos:
        LOG.info('Photo check finished; see statuses in the admin panel')
        return
    if not gallery.codes:
        LOG.warning('No usable reference photos yet: upload clear frontal photos (2-5 per child)')

    grabber = FrameGrabber(source)
    LOG.info('Camera %s, source %s', args.camera_key, redact(str(source)) if not str(source).isdigit() else 'webcam')
    grabber.start()
    confirmer = Confirmer(args.min_hits, args.window, args.cooldown)
    stats = collections.Counter()
    last_frames, last_heartbeat, last_sync, last_frame_time = 0, time.monotonic(), time.monotonic(), 0.0
    period = 1.0 / args.fps
    try:
        while True:
            loop_start = time.monotonic()
            frame, frame_time = grabber.latest()
            if frame is not None and frame_time != last_frame_time:
                last_frame_time = frame_time
                stats['processed'] += 1
                faces = engine.detect(frame, max_width=args.det_width)
                stats['faces'] += len(faces)
                for face in faces:
                    if face[2] < args.min_face:
                        stats['small_faces'] += 1
                        continue
                    feature, crop = engine.embed(frame, face)
                    if blur_score(crop) < args.min_sharpness:
                        stats['blurry'] += 1
                        continue
                    found = gallery.match(feature, margin=args.margin)
                    if found is None:
                        stats['ambiguous'] += 1
                        continue
                    code, score, _ = found
                    if score < review_threshold:
                        stats['unknown'] += 1
                        continue
                    confidence = confirmer.observe(code, score, time.monotonic())
                    if confidence is not None:
                        stats['matches'] += 1
                        platform.send_match(code, confidence)
                if args.show:
                    preview = frame.copy()
                    for face in faces:
                        x, y, w, h = face[:4].astype(int)
                        cv2.rectangle(preview, (x, y), (x + w, y + h), (0, 255, 0) if w >= args.min_face else (0, 0, 255), 2)
                    cv2.imshow('face_worker (q = quit)', preview)
                    if cv2.waitKey(1) & 0xFF == ord('q'):
                        break
            now = time.monotonic()
            if now - last_heartbeat >= 60:
                frames = grabber.frames - last_frames
                last_frames = grabber.frames
                platform.heartbeat(frames > 0, {'frames': frames, 'faces': stats['faces'], 'matches': stats['matches'],
                                                'small_faces': stats['small_faces'], 'gallery': gallery.students,
                                                'fps': round(stats['processed'] / (now - last_heartbeat), 2)})
                if stats['small_faces'] > stats['faces'] * 0.5 and stats['faces'] > 20:
                    LOG.warning('Most faces are too small (<%d px). Use a higher-resolution stream (main stream, '
                                'not sub stream) or move/zoom the camera closer to the seats.', args.min_face)
                stats.clear()
                last_heartbeat = now
                platform.flush()
            if now - last_sync >= args.sync_every:
                try:
                    review_threshold = sync_gallery(engine, platform, gallery, args.enroll_min_face)['face_review_threshold']
                except requests.RequestException as exc:
                    LOG.warning('Gallery sync failed: %s', type(exc).__name__)
                last_sync = now
            if grabber.stop.is_set():
                break
            time.sleep(max(0.0, period - (time.monotonic() - loop_start)))
    except KeyboardInterrupt:
        pass
    finally:
        grabber.stop.set()
        platform.flush()
        if args.show:
            cv2.destroyAllWindows()


def main(argv=None):
    parser = argparse.ArgumentParser(description='AI face recognition worker (one camera per process)')
    parser.add_argument('--camera-key', help='camera_key registered in the admin panel')
    parser.add_argument('--api', default='http://127.0.0.1:8000')
    parser.add_argument('--source', help='RTSP URL, webcam index (0) or video file; default: $ATTENDANCE_RTSP_URL')
    parser.add_argument('--models-dir', default=str(ROOT / 'models'))
    parser.add_argument('--download-models', action='store_true', help='Download & verify models (then continue if --camera-key given)')
    parser.add_argument('--check-photos', action='store_true', help='Only validate enrolled photos and report their quality')
    parser.add_argument('--fps', type=float, default=2.0, help='Frames analysed per second (CPU budget)')
    parser.add_argument('--det-width', type=int, default=1280, help='Max width for face detection (higher finds smaller faces)')
    parser.add_argument('--det-score', type=float, default=0.75)
    parser.add_argument('--min-face', type=int, default=40, help='Ignore faces narrower than this (pixels, full frame)')
    parser.add_argument('--enroll-min-face', type=int, default=60)
    parser.add_argument('--min-sharpness', type=float, default=15.0, help='Laplacian variance; lower = accept blurrier faces')
    parser.add_argument('--margin', type=float, default=0.04, help='Required score gap between best and 2nd-best child')
    parser.add_argument('--min-hits', type=int, default=3)
    parser.add_argument('--window', type=float, default=20.0)
    parser.add_argument('--cooldown', type=float, default=300.0)
    parser.add_argument('--sync-every', type=float, default=300.0)
    parser.add_argument('--show', action='store_true', help='Debug preview window (local use only)')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.download_models and not args.camera_key:
        ensure_models(Path(args.models_dir), download=True)
        LOG.info('Models ready in %s', args.models_dir)
        return
    if not args.camera_key:
        parser.error('--camera-key is required')
    if not 0.1 <= args.fps <= 15 or args.min_hits < 1 or args.window <= 0:
        parser.error('fps 0.1-15, min-hits >= 1, window > 0')
    run(args)


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
