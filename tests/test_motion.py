import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import numpy as np
    from rtsp_motion_bridge import movement_score
except (ImportError, SystemExit):  # optional RTSP dependencies not installed
    movement_score = None


@unittest.skipIf(movement_score is None, 'requirements-rtsp.txt not installed')
class MotionTest(unittest.TestCase):
    def test_unmoving_frame(self):
        old = np.zeros((270, 480), dtype=np.uint8)
        self.assertEqual(movement_score(old, old.copy()), 0)

    def test_moving_object(self):
        old = np.zeros((270, 480), dtype=np.uint8)
        new = old.copy()
        new[20:150, 40:140] = 255
        self.assertGreater(movement_score(old, new), 10000)


if __name__ == '__main__':
    unittest.main()
