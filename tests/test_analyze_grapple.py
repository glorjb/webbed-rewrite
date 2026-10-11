"""analyze_grapple.py must recover a known pull strength from synthetic frames built with the recovered
force structure (sim/webbed_model.grapple_pull_force) and Box2D's semi-implicit Euler step. This tests the
inversion only; the real P is unknown until W* captures exist."""
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'sim'))
sys.path.insert(0, os.path.join(HERE, '..', 'tools', 'reference'))
import analyze_grapple as ag   # noqa: E402
import webbed_model as m       # noqa: E402


def synth(x, y, vx, vy, gx, gy, P, drag=True, frame=10):
    """Rows a, b: player at (x, y) with velocity (vx, vy) px/s, grapple anchor (gx, gy), pull strength P."""
    mass = m.player_mass_kg()
    d = math.hypot(gx - x, gy - y)
    F = m.grapple_pull_force(d, vx / 60, vy / 60, m.point_direction(x, y, gx, gy), P, mass)
    ax = F[0] / mass / m.M_PER_PX
    ay = F[1] / mass / m.M_PER_PX + m.GRAVITY_MPS2[1] / m.M_PER_PX
    if drag:
        dax, day = m.player_air_drag_accel_pxps2(vx, vy)
        ax, ay = ax + dax, ay + day
    h = 1 / 60
    v = {'gx': gx, 'gy': gy, 'attached': 77, 'grapple': 1, 'ground': -4, 'webwalk': -4, 'webshot_node': -4}
    a = {'frame': frame, 'torn': 0, 'io': {'hm': 0, 'grapple': 1}, 'shots': [],
         'player': {'body': {'pos_px': [x, y], 'vel_pxps': [vx, vy]}, 'vars': v}}
    b = {'frame': frame + 1, 'torn': 0, 'io': {'hm': 0, 'grapple': 1}, 'shots': [],
         'player': {'body': {'pos_px': [x + (vx + h * ax) * h, y + (vy + h * ay) * h],
                             'vel_pxps': [vx + h * ax, vy + h * ay]}, 'vars': dict(v)}}
    return a, b


class GrappleInversion(unittest.TestCase):
    def test_recovers_known_P_with_zero_perpendicular_residual(self):
        rows = []
        for (x, y, vx, vy, gx, gy) in [(100, 300, 30, -12, 160, 120), (200, 250, -80, 40, 150, 100),
                                       (0, 0, 0, 0, 0, -150), (50, 60, 120, 5, -20, -40)]:
            a, b = synth(x, y, vx, vy, gx, gy, P=5.0)
            self.assertTrue(ag.eligible(a, b))
            r = ag.implied(a, b)
            self.assertEqual(r['regime'], 'pull')
            self.assertAlmostEqual(r['P'], 5.0, places=4)
            self.assertAlmostEqual(r['perp'], 0.0, places=4)
            rows.append(r)
        s = ag.summarise(rows)
        self.assertAlmostEqual(s['P']['median'], 5.0, places=4)
        self.assertLess(s['perp_rms'], 1e-4)
        self.assertLess(s['candidates_rms']['pull_power (5)'], 1e-4)

    def test_wrong_drag_hypothesis_shows_up_as_residual(self):
        a, b = synth(100, 300, 200, -150, 160, 120, P=3.0, drag=True)
        with_drag = ag.implied(a, b, drag=True)
        without = ag.implied(a, b, drag=False)
        self.assertAlmostEqual(with_drag['P'], 3.0, places=4)
        self.assertGreater(abs(without['P'] - 3.0) + abs(without['perp']), 1e-3)

    def test_ineligible_frames(self):
        a, b = synth(100, 300, 0, 0, 160, 120, P=5.0)
        a['io']['hm'] = 1                                   # air torque adds a force: excluded
        self.assertFalse(ag.eligible(a, b))
        a, b = synth(100, 300, 0, 0, 160, 120, P=5.0)
        b['player']['vars']['ground'] = 3                   # contact: excluded
        self.assertFalse(ag.eligible(a, b))
        a, b = synth(100, 300, 0, 0, 105, 295, P=5.0)       # d <= 16: no pull expected
        self.assertEqual(ag.implied(a, b)['regime'], 'd<=16 (no pull expected)')


if __name__ == '__main__':
    unittest.main()
