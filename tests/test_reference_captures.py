"""Regression tests: recovered model vs. measurements of the ORIGINAL game.

Each test reads capture sessions recorded with tools/reference/webbed_probe.py (v3) following
docs/REFERENCE_TEST_PROCEDURE.md. A test SKIPS only when the needed scenario was never recorded; it FAILS
when a matching capture exists but is unusable (quality gate in capture_lib) or when the original game
disagrees with the recovered model. Frame selection uses the game's own state variables; the physics
assertions and tolerances are fixed.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'sim'))
import capture_lib as cap   # noqa: E402
import webbed_model as m    # noqa: E402

POS_TOL_PX = 0.05      # float32 positions
VEL_TOL_PXPS = 0.05    # px/s


def need(prefix):
    """Usable sessions for a scenario prefix (FAIL if recorded but unusable, SKIP if never recorded)."""
    found = cap.discover(prefix)
    usable = [s for s in found if s.usable]
    if usable:
        return usable
    if found:
        raise AssertionError('captures for %r exist but none is usable:\n  %s' % (
            prefix + '*', '\n  '.join(s.summary() for s in found)))
    present = [s.name for s in cap.discover('')]
    raise unittest.SkipTest('no capture for %s* in %s (present: %s)' % (prefix, cap.CAPTURE_ROOT, present or 'none'))


def need_any(prefixes):
    """Usable sessions over several scenario prefixes; same FAIL/SKIP rule as need()."""
    out, errors = [], []
    for p in prefixes:
        try:
            out.extend(need(p))
        except unittest.SkipTest:
            pass
        except AssertionError as e:
            errors.append(str(e))
    if errors:
        raise AssertionError('\n'.join(errors))
    if not out:
        raise unittest.SkipTest('no capture for any of %s' % (prefixes,))
    return out


class CaptureInventory(unittest.TestCase):
    def test_every_capture_is_usable(self):
        """Every recorded session must pass the quality gate (player physics, coverage, torn rows)."""
        found = cap.discover('')
        if not found:
            self.skipTest('no captures in %s' % cap.CAPTURE_ROOT)
        for s in found:
            with self.subTest(session=s.name):
                self.assertTrue(s.usable, s.summary())


class WorldConfig(unittest.TestCase):
    def test_world_settings_every_frame(self):
        for s in need(''):
            for fr in s.frames:
                w = fr['world']
                if w['speed'] is None:
                    continue
                with self.subTest(session=s.name, frame=fr['frame']):
                    self.assertEqual(w['speed'], m.ROOM_SPEED)
                    self.assertIn(w['iterations'], (m.ITERATIONS_DEFAULT, m.ITERATIONS_SKATEPARK))
                    self.assertAlmostEqual(w['m_per_px'], m.M_PER_PX, places=6)


class PlayerMotion(unittest.TestCase):
    def test_free_fall_gravity(self):
        """M* and W* (e.g. after releasing a web): consecutive free-flight frames:
        dv = h*(g + scripted air drag), damping 0. Both velocity components are checked."""
        n = 0
        for s in need_any(['M', 'W']):
            segs = cap.web_segments(s.webs)
            for a, b in cap.consecutive(s.frames):
                if not (cap.free_flight(a, segs) and cap.free_flight(b, segs)):
                    continue
                va, vb = a['player']['body']['vel_pxps'], b['player']['body']['vel_pxps']
                dvx, dvy = vb[0] - va[0], vb[1] - va[1]
                if abs(dvy) > 3 * m.gravity_dv_pxps_per_step():
                    continue    # contact/impulse frame
                ex, ey = m.player_free_flight_dv(va[0], va[1])
                n += 1
                with self.subTest(session=s.name, frame=b['frame']):
                    self.assertAlmostEqual(dvy, ey, delta=VEL_TOL_PXPS)
                    self.assertAlmostEqual(dvx, ex, delta=VEL_TOL_PXPS)
        if n == 0:
            self.skipTest('no consecutive free-flight frames in M* captures')

    def test_ceiling_bounce_restitution(self):
        """Head bump while rising straight up (vx = 0): Box2D integrates gravity + drag, then the contact
        solver reflects the normal velocity with the player's restitution 0.15:
        v_b = -e * (v_a + h*(g + drag(v_a)))."""
        n = 0
        for s in need('M'):
            for a, b in cap.consecutive(s.frames):
                pa, pb = a.get('player') or {}, b.get('player') or {}
                if not (pa.get('body') and pb.get('body')) or cap.support(a):
                    continue
                va, vb = pa['body']['vel_pxps'], pb['body']['vel_pxps']
                if va[0] != 0 or vb[0] != 0 or not (va[1] < 0 < vb[1]):
                    continue                               # only straight-up motion reversed by a contact
                if vb[1] - va[1] <= 3 * m.gravity_dv_pxps_per_step():
                    continue                               # ordinary apex, not a contact
                _, dv = m.player_free_flight_dv(va[0], va[1])
                expected = -m.PLAYER_RESTITUTION * (va[1] + dv)
                n += 1
                with self.subTest(session=s.name, frame=b['frame']):
                    self.assertAlmostEqual(vb[1], expected, delta=VEL_TOL_PXPS)
        if n == 0:
            self.skipTest('no vertical head-bump contact in M* captures')

    LEVEL_DEG = 5.0

    def test_walk_plateau_speed(self):
        """M2: holding a direction on solid LEVEL ground; plateau |vx| (px/step) vs max_walkspd.
        The recovered speed target is xoff/yoff(max_walkspd*hm, 0, rclamp(ground_angle, -45, 45)), i.e. along the
        surface: its horizontal share is max_walkspd only on level ground (3.5*cos(27.5 deg) = 3.10 on a 27.5 deg
        slope). So only frames with |ground_angle| <= 5 deg are compared; sloped walking is reported by
        tools/reference/analyze_walk.py (code target vs recorded vector) and is INCONCLUSIVE here, because the
        force gate, clinging and contact friction on slopes are not recovered. Target and tolerance unchanged."""
        speeds, sloped, no_angle = [], 0, 0
        for s in need('M2'):
            for fr in s.frames:
                if cap.support(fr) == 'ground' and abs((fr.get('io') or {}).get('hm') or 0) == 1:
                    a = (fr['player'].get('vars') or {}).get('ground_angle')
                    if not isinstance(a, (int, float)):
                        no_angle += 1
                        continue
                    if abs(((a + 180.0) % 360.0) - 180.0) > self.LEVEL_DEG:
                        sloped += 1
                        continue
                    speeds.append(abs(fr['player']['body']['vel_pxps'][0]) / m.ROOM_SPEED)
        if len(speeds) < 60:
            self.skipTest('INCONCLUSIVE: %d level-ground walking frames (need >= 60); %d sloped frames and %d without '
                          'ground_angle are not comparable to a horizontal cap (see analyze_walk.py)'
                          % (len(speeds), sloped, no_angle))
        plateau = sorted(speeds)[int(0.9 * len(speeds))]
        self.assertAlmostEqual(plateau, m.MAX_WALKSPD, delta=0.25,
                               msg='recovered cap target is max_walkspd=3.5 px/step (hypothesis)')

    def test_jump_from_rest(self):
        """M3: jump frame = io.jmp fires while supported on the previous frame (the game's `ground`
        variable lags the take-off by ~2 frames, so it is not used to find the jump frame).
        All ground jumps: |v| >= jump_power px/step after the frame's physics step.
        Jumps from rest on level ground with no horizontal input: exact prediction
        v = (0, -jump_power) set in Step, then one Box2D step with gravity + scripted drag."""
        ground, web, exact = 0, 0, 0
        for s in need('M3'):
            for a, b in cap.consecutive(s.frames):
                if not (a['player'] and b['player'] and a['player']['body'] and b['player']['body']):
                    continue
                if not cap.jump_pressed(b) or not cap.support(a):
                    continue
                if cap.support(a) == 'web':
                    web += 1                               # web jump rule not recovered yet: not asserted
                    continue
                ground += 1
                va, vb = a['player']['body']['vel_pxps'], b['player']['body']['vel_pxps']
                sp = math.hypot(*vb) / m.ROOM_SPEED
                with self.subTest(session=s.name, frame=b['frame'], check='magnitude'):
                    self.assertGreaterEqual(sp + 0.2, m.JUMP_POWER)
                av = a['player']['vars']
                if math.hypot(*va) < 0.5 and av.get('ground_angle') == 0 and not (b['io'] or {}).get('hm'):
                    exact += 1
                    v0 = -m.JUMP_POWER * m.ROOM_SPEED
                    ex, ey = m.player_free_flight_dv(0.0, v0)
                    with self.subTest(session=s.name, frame=b['frame'], check='exact'):
                        self.assertAlmostEqual(vb[1], v0 + ey, delta=VEL_TOL_PXPS)
                        self.assertAlmostEqual(vb[0], ex, delta=VEL_TOL_PXPS)
        if ground == 0:
            self.skipTest('no jump from solid ground found (%d jump(s) from a web, not asserted)' % web)


class WebShot(unittest.TestCase):
    """oPlayer_Step_0 L183-L219 (firing) and L912-L916 (shot state copied to the kinematic oWebshotNode)."""

    def _shot_frames(self):
        for s in need_any(['W']):
            for fr in s.frames:
                p = fr.get('player') or {}
                v = p.get('vars') or {}
                shots = fr.get('shots') or []
                if fr['torn'] or len(shots) != 1 or None in (v.get('gx'), v.get('gy'), v.get('gh'), v.get('gv')):
                    continue
                if shots[0]['id'] != v.get('webshot_node') or not shots[0].get('body'):
                    continue
                yield s.name, fr, v, shots[0]

    def test_capture_saw_the_shots(self):
        """Diagnostic gate: every frame where the player had a live shot must have recorded it."""
        for s in need_any(['W']):
            missing = sum(fr.get('shot_missing', 0) for fr in s.frames)
            with self.subTest(session=s.name):
                self.assertEqual(missing, 0, 'probe missed the web shot on %d frames' % missing)

    def test_shot_body_follows_shot_state(self):
        """Kinematic shot body after the step: velocity = (gh, gv) px/step, position = (gx+gh, gy+gv)."""
        n = 0
        for name, fr, v, shot in self._shot_frames():
            (ex, ey), (evx, evy) = m.webshot_body_after_step(v['gx'], v['gy'], v['gh'], v['gv'])
            b = shot['body']
            n += 1
            with self.subTest(session=name, frame=fr['frame'], check='velocity'):
                self.assertAlmostEqual(b['vel_pxps'][0], evx, delta=VEL_TOL_PXPS)
                self.assertAlmostEqual(b['vel_pxps'][1], evy, delta=VEL_TOL_PXPS)
            with self.subTest(session=name, frame=fr['frame'], check='position'):
                self.assertAlmostEqual(b['pos_px'][0], ex, delta=POS_TOL_PX)
                self.assertAlmostEqual(b['pos_px'][1], ey, delta=POS_TOL_PX)
        if n == 0:
            self.skipTest('no web-shot frames in W* captures')

    def test_shot_launch_speed(self):
        """First frame of each new shot (not retracting): |(gh, gv)| = web_shot_speed px/step."""
        n, seen = 0, set()
        for name, fr, v, shot in self._shot_frames():
            if shot['id'] in seen:
                continue
            seen.add(shot['id'])
            if v.get('retract'):
                continue
            n += 1
            with self.subTest(session=name, frame=fr['frame'], shot=shot['id']):
                self.assertAlmostEqual(math.hypot(v['gh'], v['gv']), m.WEB_SHOT_SPEED, delta=1e-3)
        if n == 0:
            self.skipTest('no new web shots in W* captures')

    def test_grapple_pull_force(self):
        """oPlayer_Step_0 L681-L729: structure recovered, pull strength P (L661-L677) not decoded yet."""
        self.skipTest('pull strength P not decoded yet; see tools/reference/analyze_grapple.py for implied P')


class WebGeometry(unittest.TestCase):
    def _records(self, prefix='W'):
        for s in need(prefix):
            for r in s.webs:
                if not r['torn']:
                    yield s.name, r

    def test_length_and_dir_from_endpoints(self):
        """End Step L48-L50: dir/current_length are recomputed from ox,oy,ax,ay."""
        n = 0
        for name, r in self._records():
            v = r['vars']
            if None in (v.get('ox'), v.get('oy'), v.get('ax'), v.get('ay'), v.get('current_length')):
                continue
            n += 1
            with self.subTest(session=name, frame=r['frame'], web=r['id']):
                self.assertAlmostEqual(v['current_length'], m.point_distance(v['ox'], v['oy'], v['ax'], v['ay']), delta=1e-3)
                if v['current_length'] > 1:
                    d = m.angle_difference(v['dir'], m.point_direction(v['ox'], v['oy'], v['ax'], v['ay']))
                    self.assertLess(abs(d), 1e-2)
        if n == 0:
            self.skipTest('no web records')

    def test_endpoints_follow_attached_nodes(self):
        """End Step L42-L45: endpoint = node.xy + lengthdir(dist, node.image_angle - reldir)."""
        n = 0
        for name, r in self._records():
            v = r['vars']
            for end, dk, rk, xk, yk in (('o', 'odist', 'odir', 'ox', 'oy'), ('a', 'adist', 'adir', 'ax', 'ay')):
                pose = r.get(end + '_pose')
                if not pose or pose.get('xy') is None or v.get(dk) is None:
                    continue
                ex, ey = m.web_endpoint(pose['xy'][0], pose['xy'][1], pose['image_angle'], v[dk], v[rk])
                n += 1
                with self.subTest(session=name, frame=r['frame'], web=r['id'], end=end):
                    self.assertAlmostEqual(v[xk], ex, delta=POS_TOL_PX)
                    self.assertAlmostEqual(v[yk], ey, delta=POS_TOL_PX)
        if n == 0:
            self.skipTest('no web attached to a recorded node')

    def test_break_rule_predicts_snaps(self):
        """W3/W5: the rule runs in End Step (before the snapshot) using buffers it just updated; snap_web
        halves `length` in the same End Step and does not reset the buffers. So whenever a recorded frame's
        buffers satisfy the rule, that web must have changed length since the previous frame."""
        checked = 0
        for s in need('W'):
            per_web = {}
            for r in s.webs:
                if r['lane'] == 'focus' and not r['torn']:
                    per_web.setdefault(r['id'], {})[r['frame']] = r['vars']
            for wid, frames in per_web.items():
                for f, v in frames.items():
                    prev = frames.get(f - 1)
                    bufs = (v.get('length_buffer_1'), v.get('length_buffer_2'), v.get('length_buffer_3'))
                    if prev is None or None in bufs or prev.get('length') is None:
                        continue
                    if m.web_should_break(*bufs, breakable=bool(v.get('breakable'))):
                        checked += 1
                        with self.subTest(session=s.name, frame=f, web=wid):
                            self.assertNotAlmostEqual(v['length'], prev['length'], places=6,
                                                      msg='rule fired but web %s kept its length' % wid)
        if checked == 0:
            self.skipTest('rule never fired in captures (record W3/W5)')

    def test_stretch_matches_rest_length(self):
        """reset_web_tracking: stretch = max(length/20, 1), snap_threshold = 2 + max(0, 5-stretch)."""
        n = 0
        for name, r in self._records():
            v = r['vars']
            if v.get('length') is None or v.get('stretch') is None:
                continue
            n += 1
            with self.subTest(session=name, frame=r['frame'], web=r['id']):
                self.assertAlmostEqual(v['stretch'], m.web_stretch_for(v['length']), delta=1e-6)
                self.assertAlmostEqual(v['snap_threshold'], m.web_snap_threshold_for(v['stretch']), delta=1e-6)
        if n == 0:
            self.skipTest('no web records')


if __name__ == '__main__':
    unittest.main()
