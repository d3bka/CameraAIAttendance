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
        self.assertAlmostEqual(c.observe('A', 0.7, 2), 0.6)
        for t in (3, 4, 5):
            self.assertIsNone(c.observe('A', 0.9, t))   # cooldown
        self.assertIsNone(c.observe('B', 0.9, 0))
        self.assertIsNone(c.observe('B', 0.9, 20))      # first hit expired
        self.assertIsNone(c.observe('B', 0.9, 21))

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

    def test_models_pinned(self):
        for name, url, digest in fw.MODELS.values():
            self.assertTrue(url.startswith('https://'))
            self.assertEqual(len(digest), 64)


if __name__ == '__main__':
    unittest.main()
