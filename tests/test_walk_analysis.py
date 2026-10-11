"""The walking analysis on synthetic captures with known answers (no game data): the code-target comparison,
rejection reasons, and the level-ground restriction of test_walk_plateau_speed."""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'sim'))
sys.path.insert(0, os.path.join(HERE, '..', 'tools', 'reference'))
import analyze_walk as AW            # noqa: E402
import capture_lib as cap            # noqa: E402
import test_reference_captures as T  # noqa: E402
import webbed_model as m             # noqa: E402


def make_walk(angle, hm=-1, walk=90, plateau=3.5, accel_frames=8, frame0=1000):
    """Rows in the probe schema: rest, accelerate to `plateau` along the code-target direction, hold, release."""
    tx, ty = m.walk_target_velocity(hm, angle)
    ux, uy = tx / 3.5, ty / 3.5
    rows, x, y = [], 500.0, 300.0
    seq = [(0, 0.0)] * 20 + [(hm, plateau * min(1.0, (k + 1) / accel_frames)) for k in range(walk)] + \
          [(0, plateau * max(0.0, 1 - (k + 1) / 10)) for k in range(30)]
    for n, (h, sp) in enumerate(seq):
        vx, vy = ux * sp * 60, uy * sp * 60
        x, y = x + vx / 60, y + vy / 60
        rows.append({'frame': frame0 + n, 'skipped': 0, 'torn': 0, 't': n / 60, 'marker': 0, 'room': 1,
                     'world': {'speed': 60, 'iterations': 10, 'm_per_px': 0.03}, 'io': {'hm': float(h)},
                     'skateboards': [],
                     'player': {'id': 7, 'xy': [x, y], 'image_angle': 0.0,
                                'vars': {'ground': 42, 'ground_angle': angle, 'webwalk': -4, 'locked': 0},
                                'body': {'pos_m': [x * .03, y * .03], 'vel_mps': [vx * .03, vy * .03], 'angle_rad': 0.0,
                                         'angvel_rps': 0.0, 'pos_px': [x, y], 'vel_pxps': [vx, vy]}}})
    return rows


class CodeTargetComparison(unittest.TestCase):
    def test_slope_walk_matches_code_target_and_stays_inconclusive(self):
        rows = make_walk(-27.5)
        c = AW.model_comparison(rows, AW.phases(rows))[0]
        self.assertGreaterEqual(c['valid_frames'], 60)
        self.assertEqual(c['level_cap']['verdict'], 'INCONCLUSIVE')
        sl = c['sloped']
        self.assertEqual(sl['verdict'], 'INCONCLUSIVE')
        self.assertAlmostEqual(sl['median_err_speed_vs_3.5'], 0.0, places=3)
        self.assertAlmostEqual(sl['median_abs_vx'], 3.5 * math.cos(math.radians(27.5)), places=3)   # 3.1045
        self.assertAlmostEqual(sl['median_dir_err_code_deg'], 0.0, places=2)
        self.assertAlmostEqual(sl['median_dir_err_flipped_deg'], 55.0, places=1)    # sign convention is decidable
        self.assertIn('accelerating', c['status_counts'])
        self.assertAlmostEqual(c['acceleration']['force_only_bound_px_step2'], 0.5413, places=4)
        self.assertEqual(c['deceleration']['frames_to_below_0.05'], 9)

    def test_level_walk_gets_a_cap_verdict(self):
        c = AW.model_comparison(make_walk(0.0), AW.phases(make_walk(0.0)))[0]
        self.assertEqual(c['level_cap']['verdict'], 'PASS')
        rows = make_walk(1.0, plateau=3.08)
        self.assertEqual(AW.model_comparison(rows, AW.phases(rows))[0]['level_cap']['verdict'], 'FAIL')

    def test_rejection_reasons(self):
        rows = make_walk(-10.0)
        rows[60]['torn'] = 1
        rows[70]['player']['vars']['ground'] = -4
        c = AW.model_comparison(rows, AW.phases(rows))[0]
        st = {x['frame']: x['status'] for x in c['rows']}
        self.assertEqual(st[rows[60]['frame']], 'torn_row')
        self.assertEqual(st[rows[70]['frame']], 'not_on_ground')
        self.assertEqual(st[rows[68]['frame']], 'near_support_change')


class PlateauTestApplicability(unittest.TestCase):
    """test_walk_plateau_speed compares only level-ground frames; target 3.5 and tolerance 0.25 unchanged."""
    def run_plateau(self, rows):
        tmp = tempfile.mkdtemp(prefix='dp_walk_test_')
        d = os.path.join(tmp, 'M2_walk_dir_20990101-000000')
        os.makedirs(d)
        with open(os.path.join(d, 'segment_01.jsonl'), 'w') as f:
            for r in rows:
                f.write(json.dumps(r) + '\n')
        old = cap.CAPTURE_ROOT
        cap.CAPTURE_ROOT = tmp
        try:
            res = unittest.TestResult()
            T.PlayerMotion('test_walk_plateau_speed').run(res)
            return res
        finally:
            cap.CAPTURE_ROOT = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sloped_capture_is_inconclusive_not_failed(self):
        res = self.run_plateau(make_walk(-27.5))      # the live situation: horizontal ~3.10, along-slope 3.5
        self.assertEqual(len(res.failures) + len(res.errors), 0, res.failures)
        self.assertEqual(len(res.skipped), 1)
        self.assertIn('INCONCLUSIVE', res.skipped[0][1])

    def test_level_capture_still_fails_when_too_slow(self):
        res = self.run_plateau(make_walk(0.0, plateau=3.08))
        self.assertEqual(len(res.failures), 1)
        res = self.run_plateau(make_walk(0.0, plateau=3.5))
        self.assertTrue(res.wasSuccessful() and not res.skipped)


if __name__ == '__main__':
    unittest.main()
