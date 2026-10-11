"""Scripted, state-aware reference scenarios.

A scenario is a generator function `fn(ctx)`. Every bare `yield` hands control back to the runner, which
captures the next game frame and stores it in `ctx.row` before resuming the generator. Waiting helpers are
generators too and must be called with `yield from`. A scenario fails by raising ScenarioFailure(kind, msg).

Inputs are never assumed to arrive: each press/release is acknowledged by the game's own input object (oIO)
within ACK_FRAMES frames, and the measured latency and held duration (in game frames) are recorded. No
frame-perfect timing is claimed: the report shows the measured distribution.

Scenario IDs follow docs/REFERENCE_TEST_PROCEDURE.md so the existing tests pick the captures up
(M2_walk_flat, W1_attach_hang, W2_swing, W3_cut_mid_swing, W4_pull). The permanent-web suite uses W5_* IDs
(the procedure's W5 "web bridge" family), because W3 is already the grapple-release scenario.
"""
import math

from . import aim as A
from . import bindings as B
from . import state as S

ACK_FRAMES = 8               # oIO must reflect a press/release within this many game frames
AIM_CHECK_TOL_DEG = 5.0      # a real shot must leave within this angle of the intended direction
SIGHT_ANGLES = (240, 300, 270)   # sighting shots go into the floor next to the spider (short, no swing)
SIGHT_RMS_MAX_PX = 4.0       # rays must meet within this (client px) or the uniform-view assumption fails
LEAD_IN_FRAMES = 30          # recorded at rest before the first input (onset reference)
GRAVITY_DV = 10 / 0.03 / 60  # px/s per step (sim/webbed_model.gravity_dv_pxps_per_step)


class ScenarioFailure(Exception):
    """kind: not_ready | missed_input | setup_invalid | binding_missing | aim_failed | timeout |
    release_failed | no_web_created | capture_invalid | inconsistent | aborted | game_lost"""
    def __init__(self, kind, msg):
        super().__init__('%s: %s' % (kind, msg))
        self.kind, self.msg = kind, msg


class Ctx:
    def __init__(self, driver, bindings, log=None):
        self.driver, self.bindings = driver, bindings
        self.log = log or (lambda e: None)
        self.row = self.prev = None
        self.frames = 0                  # frames observed by this scenario
        self.new_webs = 0                # oWeb instances created since the scenario started
        self.marker = 0
        self.acks = []                   # {'action', 'edge', 'latency_frames'}
        self.holds = []                  # {'action', 'requested_frames', 'observed_frames', ...}
        self.transitions = []            # (frame, scope, var, old, new)
        self.obs = {}                    # scenario observations (measured, not asserted)
        self.track_transitions = False
        self.aimer = None                # SightedAim from the pre-scenario sighting, or None
        self.aim_sources = None          # feedback sources verified by -AimScan, or None (defaults)

    # ---------------------------------------------------------------- called by the runner each frame
    def observe(self, row):
        self.prev, self.row = self.row, row
        self.frames += 1
        self.new_webs += S.new_webs(row)
        if self.track_transitions:
            for t in S.transitions(self.prev, row):
                self.transitions.append((row['frame'],) + t)

    @property
    def frame(self):
        return self.row['frame'] if self.row else None

    def note(self, key, value):
        self.obs.setdefault(key, []).append(value)

    # ---------------------------------------------------------------- primitive inputs (no waiting)
    def _key(self, action):
        k = self.bindings.key(action)
        if not k:
            raise ScenarioFailure('binding_missing', 'no key bound for %r (run auto_record.ps1 -Calibrate)' % action)
        return k

    def press(self, action):
        k = self._key(action)
        self.driver.press(k)
        self.log({'event': 'press', 'action': action, 'key': k, 'after_frame': self.frame})

    def release(self, action):
        k = self._key(action)
        self.driver.release(k)
        self.log({'event': 'release', 'action': action, 'key': k, 'after_frame': self.frame})

    def mark(self):
        self.marker += 1
        self.log({'event': 'marker', 'marker': self.marker, 'after_frame': self.frame})

    # ---------------------------------------------------------------- waiting helpers (yield from)
    def wait(self, n, check=None):
        for _ in range(n):
            yield
            if check:
                check(self)

    def wait_until(self, pred, timeout, kind='timeout', msg='condition not reached', check=None):
        """Wait until pred(row) is true; return frames waited. kind=None returns None on timeout instead."""
        for i in range(timeout + 1):
            if self.row is not None and pred(self.row):
                return i
            if i == timeout:
                break
            yield
            if check:
                check(self)
        if kind is None:
            return None
        raise ScenarioFailure(kind, '%s (after %d frames)' % (msg, timeout))

    def await_ack(self, action, down=True):
        issued = self.frame
        n = yield from self.wait_until(
            lambda r: B.signal(S.io(r), action) == down, ACK_FRAMES, 'missed_input',
            'oIO did not show %s %s' % (action, 'pressed' if down else 'released'))
        self.acks.append({'action': action, 'edge': 'down' if down else 'up', 'latency_frames': n,
                          'issued_after_frame': issued})
        self.log({'event': 'ack', 'action': action, 'edge': 'down' if down else 'up', 'frame': self.frame,
                  'latency_frames': n})
        return n

    def hold(self, action, frames, check=None):
        """Press, wait for the oIO acknowledgement, release `frames` frames after the press was issued, wait for
        the release acknowledgement. With equal press/release latency the game sees exactly `frames` held frames;
        the frames it actually saw (from oIO) are recorded, so latency jitter shows up as a deviation."""
        self.press(action)
        lat_dn = yield from self.await_ack(action, True)
        seen = 1
        for _ in range(max(frames - lat_dn, 0)):
            yield
            seen += 1 if B.held(S.io(self.row), action) else 0
            if check:
                check(self)
        self.release(action)
        lat_up = yield from self.await_ack(action, False)
        self.holds.append({'action': action, 'requested_frames': frames, 'observed_frames': seen + max(lat_up - 1, 0),
                           'press_latency_frames': lat_dn, 'release_latency_frames': lat_up})

    def settle(self, timeout=600, need_support='any'):
        """Wait until the spider is supported and at rest (SETTLE_FRAMES frames below MOVE_MAX_PXPS)."""
        tr = S.SettleTracker()

        def ok(r):
            tr.update(r)
            return tr.settled and (need_support is None or S.support(r) is not None)
        return (yield from self.wait_until(ok, timeout, kind=None))

    # feedback sources tried when no verified source exists. NOT valid in Webbed (live 2026-10-11:
    # crosshair_tx/ty static, oIO has no aimx/aimy); kept as the last resort behind sighting.
    AIM_SOURCES = (('player', 'crosshair_tx', 'crosshair_ty'), ('io', 'aimx', 'aimy'))

    def aim(self, angle_deg):
        """Point the game's aim at `angle_deg`: sighted placement when a sighting was done for this spot,
        otherwise closed loop on aim-scan-verified feedback variables (or the unverified defaults)."""
        if self.aimer is not None:
            return (yield from self.aimer.aim(self, angle_deg))
        return (yield from self.aim_feedback(angle_deg))

    def aim_feedback(self, angle_deg, radius_px=160, tol_deg=2.0, max_iter=8):
        """Closed-loop aim: move the mouse until the game's own aim point (player crosshair_tx/ty, or oIO
        aimx/aimy if the crosshair does not follow the mouse) lies in direction `angle_deg` (GameMaker degrees,
        0 = right, 90 = up) from the player's body. The screen->room mapping is measured, never assumed."""
        w, h = self.driver.client_size()
        src = [None]

        def read(r):
            scope, kx, ky = src[0]
            v = S.pvars(r) if scope == 'player' else S.io(r)
            cur = (v.get(kx), v.get(ky))
            ok = all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in cur)
            return cur if ok else None

        def crosshair():
            last = None
            for _ in range(20):
                yield
                cur = read(self.row)
                if cur is None:
                    raise ScenarioFailure('aim_failed', '%s.%s/%s not readable: no aim feedback' % src[0])
                if cur == last:
                    return cur
                last = cur
            return last

        def angle_err(c):
            px, py = S.body(self.row)['pos_px']
            a = math.degrees(math.atan2(-(c[1] - py), c[0] - px)) % 360
            return ((a - angle_deg + 540) % 360) - 180, a

        m = [w / 2.0, h / 2.0]
        step = max(20.0, min(w, h) / 16.0)
        tried = []
        for source in (self.aim_sources or self.AIM_SOURCES):
            src[0] = source
            if read(self.row) is None:
                tried.append('%s.%s/%s unreadable' % source)
                continue
            self.driver.move_mouse(*m)
            c0 = yield from crosshair()
            self.driver.move_mouse(m[0] + step, m[1])
            c1 = yield from crosshair()
            self.driver.move_mouse(m[0], m[1] + step)
            c2 = yield from crosshair()
            j = ((c1[0] - c0[0]) / step, (c2[0] - c0[0]) / step,
                 (c1[1] - c0[1]) / step, (c2[1] - c0[1]) / step)        # d(room)/d(mouse), row-major 2x2
            det = j[0] * j[3] - j[1] * j[2]
            if abs(det) >= 1e-6:
                break
            tried.append('%s.%s/%s does not follow the mouse' % source)
        else:
            raise ScenarioFailure('aim_failed', 'no aim feedback follows the mouse: ' + '; '.join(tried))
        self.driver.move_mouse(*m)
        c = yield from crosshair()
        err, a = angle_err(c)
        for it in range(max_iter):
            if abs(err) <= tol_deg:
                break
            px, py = S.body(self.row)['pos_px']
            tx = px + radius_px * math.cos(math.radians(angle_deg))
            ty = py - radius_px * math.sin(math.radians(angle_deg))
            dx, dy = tx - c[0], ty - c[1]
            m[0] += (j[3] * dx - j[1] * dy) / det
            m[1] += (-j[2] * dx + j[0] * dy) / det
            m = [max(2, min(w - 3, m[0])), max(2, min(h - 3, m[1]))]
            self.driver.move_mouse(*m)
            c = yield from crosshair()
            err, a = angle_err(c)
        self.log({'event': 'aim', 'target_deg': angle_deg, 'achieved_deg': round(a, 3), 'error_deg': round(err, 3),
                  'source': '.'.join(src[0]), 'mouse_client': [round(m[0], 1), round(m[1], 1)], 'aim_point': c,
                  'jacobian': j, 'rejected_sources': tried})
        if abs(err) > tol_deg:
            raise ScenarioFailure('aim_failed', 'could not aim at %.0f deg (best %.1f deg; target off-screen?)'
                                  % (angle_deg, a))
        self.note('aim', {'target_deg': angle_deg, 'achieved_deg': round(a, 2), 'source': '.'.join(src[0])})
        return a


class SightedAim:
    """Aim from a measured screen position P of the shot origin (see aim.py): cursor at P + R * dir(theta)."""
    def __init__(self, P, sighting):
        self.P, self.sighting = P, sighting

    def aim(self, ctx, angle_deg):
        size = ctx.driver.client_size()
        r = A.best_radius(self.P, angle_deg, size)
        if r is None:
            raise ScenarioFailure('aim_failed', 'aiming at %.0f deg from the spider\'s screen position %s leaves the '
                                  'game window' % (angle_deg, [round(v) for v in self.P]))
        m = A.cursor_for_angle(self.P, angle_deg, r, size)
        ctx.driver.move_mouse(*m)
        yield from ctx.wait(4)                     # let the game read the cursor (acknowledged by the shot check)
        ctx.log({'event': 'aim', 'method': 'sighted', 'target_deg': angle_deg, 'P': self.P, 'radius': r,
                 'mouse_client': [round(m[0], 1), round(m[1], 1)]})
        ctx.note('aim', {'target_deg': angle_deg, 'method': 'sighted', 'radius_px': r})
        return angle_deg


class RelativeAim:
    """Aim through the game's own relative-mouse aim state (profile measured by -AimScan, aim.choose_relative_profile).
    The cursor is locked by the game, so the runner sends delta impulses: an absolute move to 'lock point + d'
    (method 'abs', exact pixels) or a relative move (method 'rel'), then re-reads the aim pair after the measured
    lag, until the pair points at the wanted direction from the player (kind 'point') or is that direction
    (kind 'vector'). The real shot is direction-checked afterwards as always."""
    TOL_DEG = 1.5

    def __init__(self, profile):
        self.p = profile
        (a, b), (c, d) = profile['gain']                    # room units per screen px: [[dX/dx, dX/dy], [dY/dx, dY/dy]]
        det = a * d - b * c
        if abs(det) < 1e-12:
            raise ScenarioFailure('aim_failed', 'relative aim profile has a singular gain matrix')
        self.inv = (d / det, -b / det, -c / det, a / det)

    def read(self, ctx):
        vals = []
        for full in self.p['pair']:
            scope, name = full.split('.', 1)
            v = (S.pvars(ctx.row) if scope == 'player' else S.io(ctx.row)).get(name)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                raise ScenarioFailure('aim_failed', 'relative aim variable %s is not readable now' % full)
            vals.append(v)
        return vals

    def impulse(self, ctx, dx, dy):
        w, h = ctx.driver.client_size()
        lim = max(20.0, min(w, h) / 2.0 - 20.0) if self.p['method'] == 'abs' else 300.0
        dx, dy = max(-lim, min(lim, dx)), max(-lim, min(lim, dy))
        if self.p['method'] == 'abs':
            c = ctx.driver.cursor_client() or (w / 2.0, h / 2.0)   # the game's lock point
            ctx.driver.move_mouse(c[0] + dx, c[1] + dy)
        else:
            ctx.driver.move_mouse_relative(dx, dy)
        return dx, dy

    def aim(self, ctx, angle_deg, max_iter=14):
        point = self.p.get('kind', 'point') == 'point'
        a = err = None
        for it in range(max_iter + 1):
            x, y = self.read(ctx)
            ox, oy = S.body(ctx.row)['pos_px'] if point else (0.0, 0.0)
            dist = math.hypot(x - ox, y - oy)
            a = math.degrees(math.atan2(-(y - oy), x - ox)) % 360 if dist > 1e-9 else None
            err = A.angle_diff(a, angle_deg) if a is not None else 180.0
            if abs(err) <= self.TOL_DEG and (not point or dist >= 40):
                break
            if it == max_iter:
                break
            R = min(max(dist, 120.0), 250.0) if point else max(dist, 1e-3)
            tx = ox + R * math.cos(math.radians(angle_deg))
            ty = oy - R * math.sin(math.radians(angle_deg))
            dX, dY = tx - x, ty - y
            sx = self.inv[0] * dX + self.inv[1] * dY
            sy = self.inv[2] * dX + self.inv[3] * dY
            self.impulse(ctx, sx, sy)
            yield from ctx.wait(int(self.p.get('lag_frames') or 1) + 2)
        ctx.log({'event': 'aim', 'method': 'relative', 'target_deg': angle_deg,
                 'achieved_deg': None if a is None else round(a, 2), 'error_deg': round(err, 2),
                 'pair': self.p['pair'], 'iterations': it})
        ctx.note('aim', {'target_deg': angle_deg, 'method': 'relative', 'pair': '/'.join(self.p['pair']),
                         'achieved_deg': None if a is None else round(a, 2), 'iterations': it})
        if abs(err) > self.TOL_DEG:
            raise ScenarioFailure('aim_failed', 'relative aim did not converge on %.0f deg (best %s deg) using %s'
                                  % (angle_deg, None if a is None else round(a, 1), '/'.join(self.p['pair'])))
        return a


def check_shot_direction(ctx, intended, what='shot'):
    """After a shot fired: its launch vector (player gh, gv) must point within AIM_CHECK_TOL_DEG of `intended`."""
    sd = A.shot_direction(S.pvars(ctx.row))
    if sd is None:
        ctx.note('aim_check', {'intended_deg': intended, 'actual_deg': None, 'note': 'gh/gv not readable'})
        return None
    err = A.angle_diff(sd[1], intended)
    ctx.note('aim_check', {'intended_deg': intended, 'actual_deg': round(sd[1], 2), 'error_deg': round(err, 2)})
    if abs(err) > AIM_CHECK_TOL_DEG:
        raise ScenarioFailure('aim_failed', 'the %s left at %.1f deg, intended %.0f deg (error %.1f deg > %.0f)'
                              % (what, sd[1], intended, err, AIM_CHECK_TOL_DEG))
    return sd[1]


def sight(ctx, angles=SIGHT_ANGLES, radius=110, guess=None):
    """Sighting: fire short grapple taps into the floor with the cursor at known client points and intersect
    the shot directions (aim.ray_intersection). Returns a SightedAim; raises aim_failed with the measured rays
    when they do not meet (relative / hidden-cursor aim, non-uniform view, or aim assist)."""
    size = ctx.driver.client_size()
    P0 = guess or (size[0] / 2.0, size[1] / 2.0)
    rays, shots = [], []
    for a in angles:
        m = A.cursor_for_angle(P0, a, radius, size) or (max(3, min(size[0] - 3, P0[0])),
                                                        max(3, min(size[1] - 3, P0[1] + radius)))
        ctx.driver.move_mouse(*m)
        yield from ctx.wait(4)
        ctx.press('grapple')
        yield from ctx.await_ack('grapple', True)
        n = yield from ctx.wait_until(lambda r: S.live_shot(r) or S.grapple_engaged(r), 10, kind=None)
        if n is None:
            ctx.release('grapple')
            raise ScenarioFailure('aim_failed', 'sighting: grapple acknowledged by oIO but no shot fired')
        sd = A.shot_direction(S.pvars(ctx.row))
        pv = S.pvars(ctx.row)
        shots.append({'cursor': [round(m[0], 1), round(m[1], 1)], 'angle_deg': round(sd[1], 2) if sd else None,
                      'sx': pv.get('sx'), 'sy': pv.get('sy'), 'body': (S.body(ctx.row) or {}).get('pos_px')})
        ctx.release('grapple')
        yield from ctx.await_ack('grapple', False)
        done = yield from ctx.wait_until(lambda r: not S.live_shot(r) and not S.grapple_engaged(r), 60, kind=None)
        if done is None:                           # toggle-style release: one tap
            ctx.press('grapple')
            yield from ctx.await_ack('grapple', True)
            ctx.release('grapple')
            yield from ctx.await_ack('grapple', False)
            yield from ctx.wait_until(lambda r: not S.live_shot(r) and not S.grapple_engaged(r), 60,
                                      'release_failed', 'sighting: could not detach from the sighting shot')
        yield from ctx.settle(300)
        if sd is None:
            raise ScenarioFailure('aim_failed', 'sighting: shot fired but gh/gv were not readable')
        rays.append((m[0], m[1], sd[0][0], sd[0][1]))
    fit = A.ray_intersection(rays)
    result = {'shots': shots, 'fit': {k: (list(v) if isinstance(v, tuple) else v) for k, v in fit.items()}}
    ctx.note('sighting', result)
    ctx.log(dict(event='sighting', **result))
    if fit.get('P') is None:
        raise ScenarioFailure('aim_failed', 'sighting: shot direction did not change with the cursor position '
                              '(spread %.1f deg): the game does not aim at the absolute cursor; run -AimScan'
                              % fit['spread_deg'])
    w, h = size
    P = fit['P']
    if fit['rms_px'] > SIGHT_RMS_MAX_PX or fit['min_t'] <= 0 or not (-50 <= P[0] <= w + 50 and -50 <= P[1] <= h + 50):
        raise ScenarioFailure('aim_failed', 'sighting: shot rays do not meet consistently (rms %.1f px, min_t %.1f, '
                              'P %s): non-uniform view, aim assist or relative aim; run -AimScan'
                              % (fit['rms_px'], fit['min_t'], [round(v) for v in P]))
    return SightedAim(P, result)


# ------------------------------------------------------------------------------------------ shared pieces
def keep_grounded(max_air=20, origin=None):
    """check(): fail when the spider leaves solid ground for > max_air consecutive frames while walking. The message
    says where and how (distance from the start, position, velocity, last ground instance and angle), so the
    failed recording can be compared with the pre-walk ground scan (tools/reference/diagnose_capture.py)."""
    st = {'air': 0, 'last_ground': None, 'lost_at': None}

    def check(ctx):
        b = S.body(ctx.row)
        if origin is not None and 'x0' not in st and b:
            st['x0'] = origin[0] if origin else b['pos_px'][0]
        if S.support(ctx.row) == 'ground':
            st['air'] = 0
            v = S.pvars(ctx.row)
            st['last_ground'] = {'id': v.get('ground'), 'angle': v.get('ground_angle'),
                                 'pos': [round(c, 1) for c in b['pos_px']] if b else None, 'frame': ctx.frame}
            return
        if st['air'] == 0:
            st['lost_at'] = {'frame': ctx.frame, 'pos': [round(c, 1) for c in b['pos_px']] if b else None,
                             'vel_pxps': [round(c, 1) for c in b['vel_pxps']] if b else None,
                             'support': S.support(ctx.row), 'webwalk': S.pvars(ctx.row).get('webwalk')}
        st['air'] += 1
        if st['air'] > max_air:
            lost = st['lost_at'] or {}
            dist = (lost.get('pos') or [0])[0] - st['x0'] if 'x0' in st and lost.get('pos') else None
            ctx.note('left_ground', {'lost': lost, 'last_ground': st['last_ground'], 'distance_from_start_px': dist})
            raise ScenarioFailure('setup_invalid', 'the spider left solid ground while walking: lost support at frame '
                                  '%s, %s px from the start, pos %s, vel %s px/s, last ground %s (angle %s); see '
                                  'diagnose_capture.py' % (lost.get('frame'), None if dist is None else round(dist),
                                                           lost.get('pos'), lost.get('vel_pxps'),
                                                           (st['last_ground'] or {}).get('id'),
                                                           (st['last_ground'] or {}).get('angle')))
    return check


def shoot_grapple(ctx, angle):
    """Aim, press grapple and keep it held; wait for the shot and the attachment. Returns the anchor."""
    yield from ctx.aim(angle)
    ctx.mark()
    ctx.press('grapple')
    yield from ctx.await_ack('grapple', True)
    t0 = ctx.frame
    yield from ctx.wait_until(lambda r: S.live_shot(r) or S.grapple_attached(r), 10, 'missed_input',
                              'grapple acknowledged by oIO but no web shot appeared')
    shot_seen = S.live_shot(ctx.row)
    check_shot_direction(ctx, angle, 'grapple shot')
    yield from ctx.wait_until(lambda r: not S.live_shot(r), 60, 'timeout', 'web shot still flying')
    yield from ctx.wait_until(S.grapple_attached, 6, 'setup_invalid',
                              'the web shot ended without attaching: no surface within reach at %.0f deg' % angle)
    a = S.anchor(ctx.row)
    ctx.note('attach', {'angle_deg': angle, 'frames_press_to_attach': ctx.frame - t0, 'shot_seen': shot_seen,
                        'anchor': a, 'player_vars': {k: S.pvars(ctx.row).get(k) for k in ('attached', 'grapple')}})
    return a


def release_grapple(ctx):
    """Release the grapple input and confirm the player detaches; tries one tap if it stays attached
    (records which behaviour the game showed: release-on-button-up or toggle)."""
    ctx.mark()
    t0 = ctx.frame
    ctx.release('grapple')
    yield from ctx.await_ack('grapple', False)
    n = yield from ctx.wait_until(lambda r: not S.grapple_engaged(r), 10, kind=None)
    if n is None:
        ctx.note('release', {'mode': 'stayed attached after button-up; tapping once'})
        ctx.press('grapple')
        yield from ctx.await_ack('grapple', True)
        ctx.release('grapple')
        yield from ctx.await_ack('grapple', False)
        yield from ctx.wait_until(lambda r: not S.grapple_engaged(r), 10, 'release_failed',
                                  'the player stayed on the grapple after releasing and tapping the button')
        ctx.note('release', {'mode': 'toggle', 'frames': ctx.frame - t0})
    else:
        ctx.note('release', {'mode': 'button-up', 'frames_to_detach': ctx.frame - t0})


def land(ctx, timeout=600):
    n = yield from ctx.wait_until(lambda r: S.support(r) is not None, timeout, 'timeout',
                                  'the spider did not land after release')
    ctx.note('land', {'frames_after_release': n})
    s = yield from ctx.settle(300)
    if s is None:
        ctx.note('land', {'settled': False})


def weave_line(ctx, angle):
    """Aim, hold weave until the shot finishes, release; expect at least one new permanent web (oWeb).
    The weave control sequence is INFERRED (io.weave / weave_pressed exist; the gesture is not recovered)."""
    yield from ctx.aim(angle)
    ctx.mark()
    webs0 = ctx.new_webs
    t0 = ctx.frame
    ctx.press('weave')
    yield from ctx.await_ack('weave', True)
    yield from ctx.wait_until(lambda r: S.live_shot(r) or ctx.new_webs > webs0, 10, kind=None)
    yield from ctx.wait_until(lambda r: not S.live_shot(r), 60, 'timeout', 'weave shot still flying')
    yield from ctx.wait(10)
    ctx.release('weave')
    yield from ctx.await_ack('weave', False)
    yield from ctx.wait_until(lambda r: ctx.new_webs > webs0, 30, 'no_web_created',
                              'weave produced no new oWeb (control sequence inferred; surface out of reach?)')
    ctx.note('weave', {'angle_deg': angle, 'new_webs': ctx.new_webs - webs0, 'frames': ctx.frame - t0})


# ------------------------------------------------------------------------------------------ scenarios
def m2_walk_flat(ctx):
    """Procedure M2: hold right 4 s, release 2 s (marker), hold left 4 s, release 2 s.
    Needs about 3.5 px/step * 240 + stopping ~ 900 px of flat ground to the RIGHT."""
    yield from ctx.wait(LEAD_IN_FRAMES)
    for action in ('right', 'left'):
        ctx.mark()
        g = keep_grounded(origin=())
        yield from ctx.hold(action, 240, check=g)
        yield from ctx.wait(120, check=g)


def m2_walk_short(ctx, hold=90):
    """M2 in less space: right 1.5 s, stop 1 s, left 1.5 s, stop 1 s, then the mirror (left first). Each hold
    still gives >= 60 frames at the plateau (enough for test_walk_plateau_speed) and the spider ends where it
    started. Needs about 3.5 * 90 + stopping ~ 380 px on EACH side."""
    yield from ctx.wait(LEAD_IN_FRAMES)
    g = keep_grounded(origin=())
    for action in ('right', 'left', 'left', 'right'):
        ctx.mark()
        yield from ctx.hold(action, hold, check=g)
        yield from ctx.wait(60, check=g)


def w1_attach_hang(ctx):
    """Procedure W1: shoot straight up, hang hands-off 8 s, release, fall to the ground."""
    ctx.track_transitions = True
    yield from ctx.wait(LEAD_IN_FRAMES)
    yield from shoot_grapple(ctx, 90)
    yield from ctx.wait(480)
    yield from release_grapple(ctx)
    yield from land(ctx)


def w2_swing(ctx, angles=(45, 135)):
    """Procedure W2: attach at 45 deg ahead, swing hands-off 8 s, release; repeat on the other side."""
    ctx.track_transitions = True
    yield from ctx.wait(LEAD_IN_FRAMES)
    for a in angles:
        yield from shoot_grapple(ctx, a)
        yield from ctx.wait(480)
        yield from release_grapple(ctx)
        yield from land(ctx)


def w3_cut_mid_swing(ctx, repeats=3, angle=45):
    """Procedure W3: attach diagonally, swing 3 s, release at the bottom of the swing (player passes under the
    anchor), land. Repeated."""
    ctx.track_transitions = True
    yield from ctx.wait(LEAD_IN_FRAMES)
    for i in range(repeats):
        a = yield from shoot_grapple(ctx, angle if i % 2 == 0 else 180 - angle)
        yield from ctx.wait(180)
        if a is not None:
            side = [None]

            def crossed(r):
                b = S.body(r)
                s = math.copysign(1, b['pos_px'][0] - a[0]) if b else 0
                if side[0] is None:
                    side[0] = s
                return s != side[0]
            n = yield from ctx.wait_until(crossed, 300, kind=None)
            ctx.note('bottom_of_swing', {'found': n is not None, 'frames_waited': n,
                                         'vel_pxps': S.body(ctx.row)['vel_pxps'] if S.body(ctx.row) else None})
        yield from release_grapple(ctx)
        yield from land(ctx)


def w4_pull(ctx, angle=20):
    """Procedure W4: attach to a surface ~6 lengths away, hold retract 3 s (marker), release, wait 2 s."""
    ctx.track_transitions = True
    yield from ctx.wait(LEAD_IN_FRAMES)
    yield from shoot_grapple(ctx, angle)
    ctx.mark()
    yield from ctx.hold('retract', 180)
    yield from ctx.wait(120)
    yield from release_grapple(ctx)
    yield from land(ctx)


def w4_retract_probe(ctx, angle=60, hold=60, rest=30):
    """EXPERIMENT (not a validation): while hanging on a held grapple, press each candidate key in
    auto_bindings.json `retract_probe` for `hold` frames and record what changes:
      * oIO variables whose values during the key differ from the grapple-only baseline (is it an input at all?),
      * player `retract` / `weave` / `grapple` / `attached` / `pull_target` values,
      * the rope-distance trend d(t) to the anchor vs the baseline trend (does it reel in / pay out?).
    Answers: which control (if any) changes the grapple length, and whether it needs an active grapple. The
    recovered pull-strength block (oPlayer_Step_0 L661-L677) branches on weave, so RMB/weave is tried first."""
    ctx.track_transitions = True
    yield from ctx.wait(LEAD_IN_FRAMES)
    anchor = yield from shoot_grapple(ctx, angle)
    if anchor is None:
        raise ScenarioFailure('setup_invalid', 'grapple attached but no anchor (gx, gy) is readable')
    skip = {(ctx.bindings.key(a) or '').upper() for a in ('grapple', 'jump', 'left', 'right')}
    pvar_keys = ('retract', 'weave', 'grapple', 'attached', 'pull_target', 'webwalk', 'ground')

    def measure(n):
        ds, io_vals, pv_vals, engaged = [], {}, {}, 0
        for _ in range(n):
            yield
            b = S.body(ctx.row)
            if b:
                ds.append(math.hypot(b['pos_px'][0] - anchor[0], b['pos_px'][1] - anchor[1]))
            for k, v in S.io(ctx.row).items():
                if k not in ('aimx', 'aimy', 'mvdir', 'mvmag'):
                    io_vals.setdefault(k, set()).add(round(v, 3) if isinstance(v, float) else v)
            for k in pvar_keys:
                v = S.pvars(ctx.row).get(k)
                pv_vals.setdefault(k, set()).add(round(v, 3) if isinstance(v, float) else v)
            engaged += 1 if S.grapple_engaged(ctx.row) else 0
        slope = (ds[-1] - ds[0]) / (len(ds) - 1) if len(ds) > 1 else None
        return {'d_start': ds[0] if ds else None, 'd_end': ds[-1] if ds else None, 'd_slope_px_per_frame': slope,
                'io': io_vals, 'player': pv_vals, 'engaged_frames': engaged, 'frames': n}

    base = yield from measure(hold)
    trials = []
    for key in B.load_probe_candidates():
        k = key.upper()
        if k in skip:
            continue
        if not S.grapple_engaged(ctx.row):
            trials.append({'key': k, 'result': 'not tried: the player was no longer on the grapple'})
            break
        ctx.mark()
        ctx.driver.press(k)
        ctx.log({'event': 'probe_press', 'key': k, 'after_frame': ctx.frame})
        try:
            m = yield from measure(hold)
        finally:
            ctx.driver.release(k)
            ctx.log({'event': 'probe_release', 'key': k, 'after_frame': ctx.frame})
        after = yield from measure(rest)
        new_io = {v: sorted(map(str, m['io'][v] - base['io'].get(v, set()))) for v in m['io']
                  if m['io'][v] - base['io'].get(v, set())}
        new_pv = {v: sorted(map(str, m['player'][v] - base['player'].get(v, set()))) for v in m['player']
                  if m['player'][v] - base['player'].get(v, set())}
        bs, ms = base['d_slope_px_per_frame'], m['d_slope_px_per_frame']
        trials.append({'key': k, 'oio_changed': new_io, 'player_changed': new_pv,
                       'd_slope_px_per_frame': ms, 'd_slope_minus_baseline': (ms - bs) if None not in (ms, bs) else None,
                       'd_start': m['d_start'], 'd_end': m['d_end'], 'engaged_frames': m['engaged_frames'],
                       'still_engaged_after': after['engaged_frames'] == rest})
    ctx.note('retract_probe', {'angle_deg': angle, 'anchor': anchor, 'hold_frames': hold,
                               'baseline': {'d_slope_px_per_frame': base['d_slope_px_per_frame'],
                                            'd_start': base['d_start'], 'd_end': base['d_end'],
                                            'oio_values': {k: sorted(map(str, v)) for k, v in base['io'].items()},
                                            'player_values': {k: sorted(map(str, v)) for k, v in base['player'].items()}},
                               'trials': trials,
                               'interpretation': 'measured only; a key that changes oIO or the d trend is a CANDIDATE '
                                                 'for the pull control, to be confirmed against the decoded code'})
    if S.grapple_engaged(ctx.row):
        yield from release_grapple(ctx)
    else:
        ctx.release('grapple')
        yield from ctx.await_ack('grapple', False)
    yield from land(ctx)


def w5_weave_line(ctx, angle=20):
    """Permanent web: weave one line, observe it hands-off 5 s (tension / slack), then walk away from it for
    2 s to load it, release, wait 2 s."""
    ctx.track_transitions = True
    yield from ctx.wait(LEAD_IN_FRAMES)
    yield from weave_line(ctx, angle)
    yield from ctx.wait(300)
    ctx.mark()
    yield from ctx.hold('left', 120)
    yield from ctx.wait(120)


def w5_weave_snap(ctx, angle=20, jerks=6):
    """Permanent web: weave a line, then jerk it (direction reversals and jumps) to provoke the length-reversal
    snap rule. Snaps are observed (new oWeb from snap_web), not required."""
    ctx.track_transitions = True
    yield from ctx.wait(LEAD_IN_FRAMES)
    yield from weave_line(ctx, angle)
    webs_after_line = ctx.new_webs
    yield from ctx.wait(60)
    for i in range(jerks):
        ctx.mark()
        yield from ctx.hold('left' if i % 2 == 0 else 'right', 12)
        yield from ctx.hold('jump', 4)
        yield from ctx.wait(40)
    yield from ctx.wait(120)
    ctx.note('snap', {'new_webs_after_line': ctx.new_webs - webs_after_line,
                      'interpretation': 'snap_web creates a new oWeb; re-anchoring can too (see transitions)'})


class Scenario:
    def __init__(self, sid, suite, fn, setup, support='ground', max_frames=6000, needs=(), space=None, aim=False):
        self.id, self.suite, self.fn, self.setup = sid, suite, fn, setup
        self.support, self.max_frames, self.needs = support, max_frames, tuple(needs)
        self.space = space or {}          # clear ground needed before inputs: {'right': px, 'left': px}
        self.aim = aim                    # aims with the mouse: sighting runs first (unless aim-scan verified)


SCENARIOS = {s.id: s for s in [
    Scenario('M2_walk_short', 'M2', m2_walk_short,
             'Flat solid ground with >= 380 px (about 17 spider-lengths) clear on BOTH sides.',
             needs=('right', 'left'), max_frames=1200, space={'right': 380, 'left': 380}),
    Scenario('M2_walk_flat', 'M2', m2_walk_flat,
             'Flat solid ground with >= 900 px (about 40 spider-lengths) clear to the RIGHT.',
             needs=('right', 'left'), max_frames=1200, space={'right': 900}),
    Scenario('W1_attach_hang', 'W1', w1_attach_hang,
             'Solid ground under a ceiling or branch about 4-8 spider-lengths straight ABOVE.',
             needs=('grapple',), aim=True, max_frames=2400),
    Scenario('W2_swing', 'W2', w2_swing,
             'Solid ground with a ceiling reachable at 45 deg up-right AND 45 deg up-left (open space below).',
             needs=('grapple',), aim=True, max_frames=4800),
    Scenario('W3_cut_mid_swing', 'W2', w3_cut_mid_swing,
             'Same spot as W2_swing.', needs=('grapple',), aim=True, max_frames=6000),
    Scenario('W4_retract_probe', 'W4', w4_retract_probe,
             'Solid ground under a ceiling about 6-10 spider-lengths away at ~60 deg up-right (long rope, open space).',
             needs=('grapple',), aim=True, max_frames=3600),
    Scenario('W4_pull', 'W4', w4_pull,
             'Solid ground with a wall/surface about 6 spider-lengths away at ~20 deg up-right.',
             needs=('grapple', 'retract'), aim=True, max_frames=2400),
    Scenario('W5_weave_line', 'W3', w5_weave_line,
             'Solid ground with a wall/surface about 4-8 spider-lengths away at ~20 deg up-right, open space to the left.',
             needs=('weave', 'left'), aim=True, max_frames=2400),
    Scenario('W5_weave_snap', 'W3', w5_weave_snap,
             'Same spot as W5_weave_line.', needs=('weave', 'left', 'right', 'jump'), aim=True, max_frames=3000),
]}

SUITES = {
    'M2': ['M2_walk_short', 'M2_walk_flat'],                  # walking acceleration, stopping, max speed
    'W1': ['W1_attach_hang'],                                 # grapple projectile, attachment, hang, release
    'W2': ['W2_swing', 'W3_cut_mid_swing'],                   # grapple tension, swinging, release
    'W4': ['W4_retract_probe', 'W4_pull'],                    # pull / retract control: experiment, then scenario
    'W3': ['W5_weave_line', 'W5_weave_snap'],                 # permanent webs: creation, pulling, snapping
}
SUITE_ORDER = ['M2', 'W1', 'W2', 'W4', 'W3']
