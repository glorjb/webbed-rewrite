"""Physics baseline guard: the validated M3 capture must stay intact and keep matching the model.

The baseline record lives in docs/baselines/; the capture itself (original-game measurements) lives in
DesktopPlaygroundLocal\\captures (with a read-only copy in captures_backup). This test FAILS if the capture is
missing or changed, or if a model change breaks a previously validated result.
"""
import hashlib
import json
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'sim'))
import capture_lib as cap   # noqa: E402
import webbed_model as m    # noqa: E402

BASELINE = os.path.join(HERE, '..', 'docs', 'baselines', 'M3_jump_in_place_baseline.json')
BACKUP_ROOT = os.path.join(os.path.dirname(cap.CAPTURE_ROOT), 'captures_backup')


class M3Baseline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = json.load(open(BASELINE))
        path = os.path.join(cap.CAPTURE_ROOT, cls.base['session'])
        cls.path = path if os.path.isdir(path) else os.path.join(BACKUP_ROOT, cls.base['session'])

    def test_capture_present_and_unchanged(self):
        self.assertTrue(os.path.isdir(self.path), 'baseline capture missing: %s' % self.base['session'])
        for f, digest in self.base['sha256'].items():
            with self.subTest(file=f):
                self.assertEqual(hashlib.sha256(open(os.path.join(self.path, f), 'rb').read()).hexdigest(), digest)

    def test_validated_results_still_hold(self):
        s = cap.Session(self.path)
        self.assertTrue(s.usable, s.summary())
        # jumps: exact jump velocity after one step
        v0 = -m.JUMP_POWER * m.ROOM_SPEED
        expected = v0 + m.player_free_flight_dv(0.0, v0)[1]
        self.assertAlmostEqual(expected, self.base['validated']['jumps']['expected_vy_pxps'], places=3)
        frames = {r['frame']: r for r in s.frames}
        for j in self.base['validated']['jumps']['observed']:
            with self.subTest(jump=j['frame']):
                self.assertAlmostEqual(frames[j['frame']]['player']['body']['vel_pxps'][1], expected, delta=0.05)
        # free flight: residual RMS of the drag model
        segs = cap.web_segments(s.webs)
        res = []
        for a, b in cap.consecutive(s.frames):
            if cap.free_flight(a, segs) and cap.free_flight(b, segs):
                va, vb = a['player']['body']['vel_pxps'], b['player']['body']['vel_pxps']
                if abs(vb[1] - va[1]) > 3 * m.gravity_dv_pxps_per_step():
                    continue
                res.append(vb[1] - va[1] - m.player_free_flight_dv(*va)[1])
        self.assertEqual(len(res), self.base['validated']['free_flight']['pairs'])
        self.assertLess(math.sqrt(sum(r * r for r in res) / len(res)), 1e-3)


if __name__ == '__main__':
    unittest.main()
