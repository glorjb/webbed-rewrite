"""Unit tests pinning the rules recovered from the original binary (no capture needed).

If one of these fails, the model no longer matches what the original code does.
Expected values are hand-derived from the recovered formulas, not from the model itself.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'sim'))
import webbed_model as m  # noqa: E402


class PhysicsWorld(unittest.TestCase):
    def test_one_substep_per_frame_at_60(self):
        self.assertEqual(m.physics_substeps(60, 60), [1 / 60])

    def test_substeps_when_update_speed_exceeds_room_speed(self):
        # runner loop: s = speed/room; full dt while s > 1, then s*dt remainder
        self.assertEqual(m.physics_substeps(120, 60), [1 / 120, 1 / 120])
        steps = m.physics_substeps(90, 60)
        self.assertAlmostEqual(steps[0], 1 / 90)
        self.assertAlmostEqual(steps[1], 0.5 / 90)

    def test_gravity_per_step(self):
        self.assertAlmostEqual(m.gravity_dv_pxps_per_step(), 10 / 0.03 / 60)

    def test_player_mass(self):
        self.assertAlmostEqual(m.player_mass_kg(), 0.5 * math.pi * 0.33 ** 2)

    def test_player_air_drag(self):
        # oPlayer_Step_0 L1575-1583: F = 0.005 * s^2 N (s in px/step) opposite velocity, damping 0.
        # Hand-derived: v = -222.59 px/s -> s = 3.70983 px/step -> F = 0.068815 N -> a = 0.40229 m/s^2
        # = 13.4097 px/s^2 (opposing the upward motion, i.e. +y) -> per step +0.223494 px/s on top of g*h.
        dvx, dvy = m.player_free_flight_dv(0.0, -222.59)
        self.assertAlmostEqual(dvx, 0.0)
        self.assertAlmostEqual(dvy, 10 / 0.03 / 60 + 0.223494, places=4)
        # parachute uses 0.02 (4x the drag)
        _, ay = m.player_air_drag_accel_pxps2(0.0, 100.0, parachute=True)
        _, ay0 = m.player_air_drag_accel_pxps2(0.0, 100.0)
        self.assertAlmostEqual(ay / ay0, 4.0)


class Intrinsics(unittest.TestCase):
    def test_lengthdir_snaps_tiny_components(self):
        self.assertEqual(m.lengthdir_x(10, 90), 0.0)
        self.assertEqual(m.lengthdir_y(10, 0), 0.0)
        self.assertAlmostEqual(m.lengthdir_y(10, 90), -10.0, places=5)

    def test_point_direction_y_down(self):
        self.assertAlmostEqual(m.point_direction(0, 0, 1, 0), 0.0)
        self.assertAlmostEqual(m.point_direction(0, 0, 0, -1), 90.0)
        self.assertAlmostEqual(m.point_direction(0, 0, -1, 0), 180.0, places=4)
        self.assertAlmostEqual(m.point_direction(0, 0, 0, 1), 270.0)


class Scripts(unittest.TestCase):
    def test_xoff_yoff_rotation(self):
        self.assertAlmostEqual(m.xoff(1, 0, 90), 0.0, places=9)
        self.assertAlmostEqual(m.yoff(1, 0, 90), 1.0, places=9)
        self.assertAlmostEqual(m.xoff(0, 1, 90), -1.0, places=9)
        self.assertAlmostEqual(m.yoff(0, 16, 0), 16.0)

    def test_rlerp_takes_short_way(self):
        self.assertAlmostEqual(m.rlerp(350, 10, 0.5), 0.0)
        self.assertAlmostEqual(m.rlerp(10, 350, 0.5), 0.0)

    def test_pitch_table(self):
        self.assertEqual(len(m.PITCH_TABLE), 48)
        self.assertEqual(m.get_pitch_in_tune(1.0), 1)
        self.assertEqual(m.get_pitch_in_tune(1.3), 1.33)
        self.assertEqual(m.get_pitch_in_tune(10), 3.77)


class Webs(unittest.TestCase):
    def test_tension_zero_when_slack(self):
        self.assertEqual(m.web_tension_magnitude(90, 100), 0.0)

    def test_tension_ramps_and_saturates(self):
        # rest length 100 -> stretch = max(100/20, 1) = 5
        self.assertAlmostEqual(m.web_tension_magnitude(102, 100), 0.4)   # (2/5) * 1
        self.assertAlmostEqual(m.web_tension_magnitude(200, 100), 1.0)   # capped at pull_power
        self.assertEqual(m.web_tension_magnitude(100.4, 100), 0.0)        # 0.08 <= 0.1 threshold
        # short web: stretch clamps to 1
        self.assertAlmostEqual(m.web_tension_magnitude(10.5, 10), 0.5)

    def test_stretch_and_snap_threshold_from_length(self):
        self.assertEqual(m.web_stretch_for(10), 1.0)
        self.assertEqual(m.web_stretch_for(200), 10.0)
        self.assertEqual(m.web_snap_threshold_for(1.0), 6.0)
        self.assertEqual(m.web_snap_threshold_for(10.0), 2.0)

    def test_break_rule(self):
        self.assertTrue(m.web_should_break(100, 95, 101))     # -5 then +6: reversal, both > 4
        self.assertFalse(m.web_should_break(100, 95, 90))     # monotonic stretch
        self.assertFalse(m.web_should_break(100, 97, 101))    # reversal but 3 px change
        self.assertFalse(m.web_should_break(100, 95, 101, breakable=False))

    def test_endpoint_follows_rotation(self):
        x, y = m.web_endpoint(100, 100, 90, 10, 0)
        self.assertAlmostEqual(x, 100.0, places=4)
        self.assertAlmostEqual(y, 90.0, places=4)

    def test_snap_pitch(self):
        self.assertAlmostEqual(m.snap_pitch(75), 1.0)
        self.assertAlmostEqual(m.snap_pitch(0.5), 75.0)


class Walking(unittest.TestCase):
    def test_target_velocity_follows_the_ground_angle(self):
        # xoff/yoff(3.5 * hm, 0, rclamp(angle, -45, 45)) = 3.5 * hm * (dcos a, dsin a)
        vx, vy = m.walk_target_velocity(1, 0)
        self.assertAlmostEqual((vx, vy), (3.5, 0.0))
        vx, vy = m.walk_target_velocity(-1, -27.5)
        self.assertAlmostEqual(vx, -3.5 * math.cos(math.radians(27.5)))     # -3.1045: horizontal share on a slope
        self.assertAlmostEqual(vy, 3.5 * math.sin(math.radians(27.5)))
        self.assertAlmostEqual(math.hypot(*m.walk_target_velocity(1, 60)), 3.5)   # clamped to 45, magnitude kept
        self.assertAlmostEqual(m.walk_target_velocity(1, 60)[1], 3.5 * math.sin(math.radians(45)))
        self.assertAlmostEqual(m.walk_target_velocity(1, 350)[1], 3.5 * math.sin(math.radians(-10)))

    def test_force_only_velocity_change(self):
        # 10 N on 0.5 * pi * 0.33^2 kg = 58.46 m/s^2 -> per step 0.97436 m/s = 32.479 px/s = 0.54131 px/step
        self.assertAlmostEqual(m.walk_force_dv_per_step(), 10 / (0.5 * math.pi * 0.33 ** 2) / 0.03 / 3600, places=9)
        self.assertAlmostEqual(m.walk_force_dv_per_step(), 0.54131, places=4)


if __name__ == '__main__':
    unittest.main()
