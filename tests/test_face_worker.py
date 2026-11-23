import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import numpy as np
    import face_worker as fw
except (ImportError, SystemExit):  # optional AI dependencies not installed
    fw = None


@unittest.skipIf(fw is None, 'requirements-ai.txt not installed')
class FaceWorkerLogicTest(unittest.TestCase):
    def unit(self, seed):
        v = np.random.default_rng(seed).normal(size=128).astype(np.float32)
        return v / np.linalg.norm(v)

    def test_gallery_match_and_margin(self):
        a, b = self.unit(1), self.unit(2)
        g = fw.Gallery()
        g.rebuild([('A', a), ('A', self.unit(3)), ('B', b), ('C', None)])
        self.assertEqual(g.students, 2)
        code, score, _ = g.match(a)
        self.assertEqual(code, 'A')
        self.assertAlmostEqual(score, 1.0, places=4)
        twin = fw.Gallery()
        twin.rebuild([('A', a), ('B', a)])           # two children with identical templates
        self.assertIsNone(twin.match(a))              # ambiguous -> never guess
        self.assertIsNone(fw.Gallery().match(a))

    def test_confirmer_requires_repeated_sightings(self):
        c = fw.Confirmer(min_hits=3, window=10, cooldown=60)
        self.assertIsNone(c.observe('A', 0.6, 0))
        self.assertIsNone(c.observe('A', 0.5, 1))
        self.assertEqual(c.observe('A', 0.7, 2), (0.6, None))
        for t in (3, 4, 5):
            self.assertIsNone(c.observe('A', 0.9, t))   # cooldown
        self.assertIsNone(c.observe('B', 0.9, 0))
        self.assertIsNone(c.observe('B', 0.9, 20))      # first hit expired
        self.assertIsNone(c.observe('B', 0.9, 21))

    def test_second_opinion_scores(self):
        a, b = self.unit(1), self.unit(2)
        a2, b2 = np.ones(512, np.float32) / np.sqrt(512), -np.ones(512, np.float32) / np.sqrt(512)
        g = fw.Gallery()
        g.rebuild([('A', a, a2), ('B', b, None), ('C', self.unit(3))])
        self.assertAlmostEqual(g.verify('A', a2), 1.0, places=4)
        self.assertAlmostEqual(g.verify('A', b2), -1.0, places=4)
        self.assertIsNone(g.verify('B', a2))          # no second template for B -> no opinion
        self.assertIsNone(g.verify('A', None))
        c = fw.Confirmer(min_hits=2, window=10, cooldown=60)
        self.assertIsNone(c.observe('A', 0.40, 0, 0.5))
        conf, second = c.observe('A', 0.42, 1, 0.6)
        self.assertAlmostEqual(conf, 0.41)
        self.assertAlmostEqual(second, 0.55)

    def test_verifier_is_optional(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fw.load_verifier(Path(tmp)))                  # not installed -> worker runs without it
            self.assertIsNone(fw.load_verifier(Path(tmp), disabled=True))

    def test_camera_identity_pinning(self):
        import tempfile
        macs = {'mac': 'aa:bb:cc:00:11:22'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'pins.json'
            pins = fw.CameraPins(path, mac_lookup=lambda ip, port: macs['mac'])
            url = 'rtsp://admin:secret@192.168.1.64:554/Streaming/Channels/101'
            self.assertTrue(pins.verify('room', url))                      # first use: pinned
            self.assertTrue(fw.CameraPins(path, mac_lookup=lambda ip, port: macs['mac']).verify('room', url))  # persisted
            macs['mac'] = '11:22:33:44:55:66'                             # same IP, other network/device
            self.assertFalse(pins.verify('room', url))
            macs['mac'] = None                                            # unreachable -> do not send credentials
            self.assertFalse(pins.verify('room', url))
            self.assertTrue(pins.verify('webcam', '0'))                   # local webcam / files are not pinned
            self.assertTrue(fw.CameraPins(path, enabled=False, mac_lookup=lambda ip, port: None).verify('room', url))
            self.assertTrue(pins.reset('room'))
            macs['mac'] = '11:22:33:44:55:66'
            self.assertTrue(pins.verify('room', url))                     # re-pinned after a deliberate reset
        self.assertIsNone(fw.camera_ip('rtsp://u:p@8.8.8.8/x'))           # public addresses are never treated as LAN cameras

    def test_self_learning_rules(self):
        class FakePlatform:
            uploads = []

            def auto_photo(self, code, data):
                self.uploads.append(code)
                return True
        shared = fw.SharedGallery()
        shared.auto_learn = True
        w = object.__new__(fw.CameraWorker)
        w.shared, w.platform, w.camera_key = shared, FakePlatform(), 'room'
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        aligned = rng.integers(0, 255, (112, 112, 3), dtype=np.uint8)        # high-frequency noise = sharp
        face = np.array([500, 200, 120, 150] + [0] * 11, dtype=np.float32)
        self.assertFalse(w.maybe_learn('A', 0.50, None, frame, face, aligned))  # not confident enough
        self.assertFalse(w.maybe_learn('A', 0.92, None, frame, face, aligned))  # nothing new to learn
        self.assertTrue(w.maybe_learn('A', 0.62, None, frame, face, aligned))
        self.assertFalse(w.maybe_learn('A', 0.62, None, frame, face, aligned))  # once per day per child
        small = face.copy()
        small[2] = 50
        self.assertFalse(w.maybe_learn('B', 0.62, None, frame, small, aligned))  # face too small
        shared.verifier = object()
        self.assertFalse(w.maybe_learn('C', 0.62, 0.30, frame, face, aligned))   # second model not convinced
        self.assertTrue(w.maybe_learn('C', 0.62, 0.70, frame, face, aligned))
        shared.auto_learn = False
        self.assertFalse(w.maybe_learn('D', 0.62, 0.70, frame, face, aligned))   # school switched it off
        self.assertEqual(FakePlatform.uploads, ['A', 'C'])

    def test_enrollment_quality(self):
        face = lambda w: [0, 0, w, w] + [0] * 11
        self.assertEqual(fw.enrollment_quality(np.zeros((0, 15)))[0], 'no_face')
        self.assertEqual(fw.enrollment_quality(np.array([face(100)], dtype=np.float32)), ('ok', 0))
        self.assertEqual(fw.enrollment_quality(np.array([face(30)], dtype=np.float32))[0], 'too_small')
        self.assertEqual(fw.enrollment_quality(np.array([face(100), face(95)], dtype=np.float32))[0], 'multiple_faces')
        self.assertEqual(fw.enrollment_quality(np.array([face(40), face(120)], dtype=np.float32)), ('ok', 1))

    def test_rtsp_credentials_never_logged(self):
        red = fw.redact('rtsp://admin:S3cret!@10.0.0.5:554/Streaming/Channels/101')
        self.assertNotIn('S3cret', red)
        self.assertNotIn('admin', red)
        self.assertIn('10.0.0.5', red)

    def test_schedule_windows(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 16, 5, 0, tzinfo=timezone.utc)
        s = fw.Schedule()
        self.assertTrue(s.active('room1', now))           # not loaded yet -> watch rather than miss a lesson
        s.update({'windows': {'room1': [[(now - timedelta(minutes=5)).isoformat(), (now + timedelta(minutes=40)).isoformat()]],
                              'room2': []}})
        self.assertTrue(s.active('room1', now))
        self.assertFalse(s.active('room1', now + timedelta(hours=2)))
        self.assertFalse(s.active('room2', now))
        self.assertFalse(s.active('unknown', now))
        self.assertIsNone(s.next_start('room1', now))
        self.assertTrue(fw.Schedule(always_on=True).active('room2', now))

    def test_config_loading(self):
        import json
        import os
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'cameras.json'
            cfg.write_text(json.dumps({'api': 'http://10.0.0.2:8080', 'token': 'cat_x',
                                       'cameras': {'room201': 'rtsp://u:p@10.0.0.5/101', 'gate': '0'}}), encoding='utf-8')
            args = SimpleNamespace(config=str(cfg), api='http://127.0.0.1:8080', source=None, camera_key=None, check_photos=False)
            saved = os.environ.pop('ATTENDANCE_INGEST_TOKEN', None)
            try:
                cams, token = fw.load_cameras(args)
            finally:
                if saved is not None:
                    os.environ['ATTENDANCE_INGEST_TOKEN'] = saved
            self.assertEqual(set(cams), {'room201', 'gate'})
            self.assertEqual((token, args.api), ('cat_x', 'http://10.0.0.2:8080'))
            cfg.write_text('{"cameras": {}}', encoding='utf-8')
            with self.assertRaises(SystemExit):
                fw.load_cameras(args)

    def test_models_pinned(self):
        for name, url, digest in fw.MODELS.values():
            self.assertTrue(url.startswith('https://'))
            self.assertEqual(len(digest), 64)


if __name__ == '__main__':
    unittest.main()
