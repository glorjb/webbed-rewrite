"""Implied grapple pull strength P from original-game captures (W* scenarios).

    python tools/reference/analyze_grapple.py [prefix=W] [--bbox-offset 0] [--json out.json]

The force STRUCTURE of the grapple pull is recovered (oPlayer_Step_0 L681-L729, sim/webbed_model.py):
    d > 16:  w = (1 - clamp((d - 10) / 64, 0, 1))^2,   c = min(max(5, |v|), d / 5)      (v in px/step)
             F = lerp(lengthdir(P, dir_g), lengthdir(c, dir_g) - v - (0, 10 m), w)       (N)
but the pull strength P (L661-L677) is NOT decoded. This tool measures what P must have been on every
captured frame where the player hangs on a grapple with no other scripted force, by inverting Box2D's
semi-implicit Euler step:
    F_applied = m * (dv / h) - m * g - F_drag       (F_drag: the verified quadratic air drag, hypothesis A)
    far = (F_applied - w * near) / (1 - w),   P = far . u(dir_g),   perp = far . n(dir_g)
`perp` must be ~0 if the recovered structure is right; P's dependence on d and |v| shows its formula.
Hypothesis B drops the air drag (drag while grappling is not verified). Results are MEASUREMENTS, not decoded
code; they become evidence once they agree with a decode of L661-L677.

Frame selection (game's own variables): consecutive untorn frames, player engaged on a grapple in both,
no web shot in flight, not supported, no horizontal input (air torque adds force), not retracting, not in
water / parachute / skateboard, and no contact impulse (|dv| < 3 g h beyond the predicted force).
`d` is distance_to_point(gx, gy): GameMaker measures it from the instance's BOUNDING BOX, which comes from the
player's sprite mask (not recovered). --bbox-offset subtracts an assumed box half-size from the centre
distance; compare runs with 0 and ~11 to see the sensitivity.
"""
import argparse
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'tests'))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'sim'))
sys.path.insert(0, HERE)
import capture_lib as cap   # noqa: E402
import webbed_model as m    # noqa: E402
from autoref import state as S   # noqa: E402

H = 1.0 / m.ROOM_SPEED
G_MPS2 = m.GRAVITY_MPS2[1]


def eligible(a, b):
    for r in (a, b):
        p = r.get('player') or {}
        v = p.get('vars') or {}
        io = r.get('io') or {}
        if not p.get('body') or S.support(r) or not S.grapple_engaged(r):
            return False
        if io.get('hm') or io.get('retract') or v.get('retract') or v.get('in_water') or v.get('parachute') \
                or v.get('riding_skateboard') or v.get('locked'):
            return False
        if v.get('gx') is None or v.get('gy') is None:
            return False
    return True


def implied(a, b, bbox_offset=0.0, drag=True):
    """Implied P and perpendicular residual for one frame pair; None when the geometry gives no information."""
    mass = m.player_mass_kg()
    pa = a['player']
    va = pa['body']['vel_pxps']
    vb = b['player']['body']['vel_pxps']
    x, y = pa['body']['pos_px']
    gx, gy = pa['vars']['gx'], pa['vars']['gy']
    dc = math.hypot(gx - x, gy - y)
    d = max(0.0, dc - bbox_offset)
    # net force on the body over the step (N), minus gravity and (hypothesis A) the scripted drag
    fx = mass * (vb[0] - va[0]) / H * m.M_PER_PX
    fy = mass * (vb[1] - va[1]) / H * m.M_PER_PX - mass * G_MPS2
    if drag:
        ax, ay = m.player_air_drag_accel_pxps2(va[0], va[1])
        fx -= mass * ax * m.M_PER_PX
        fy -= mass * ay * m.M_PER_PX
    out = {'frame': b['frame'], 'd_center': dc, 'd': d, 'speed_px_step': math.hypot(*va) / m.ROOM_SPEED,
           'F': (fx, fy)}
    if d <= 16:
        out['regime'] = 'd<=16 (no pull expected)'
        out['F_mag'] = math.hypot(fx, fy)
        return out
    dir_g = m.point_direction(x, y, gx, gy)
    w = (1 - min(max((d - 10) / 64.0, 0.0), 1.0)) ** 2
    if w > 0.98:
        out['regime'] = 'w~1 (P unobservable)'
        return out
    vxs, vys = va[0] / m.ROOM_SPEED, va[1] / m.ROOM_SPEED
    c = min(max(5.0, math.hypot(vxs, vys)), d / 5.0)
    near = (m.lengthdir_x(c, dir_g) - vxs, m.lengthdir_y(c, dir_g) - vys - 10 * mass)
    far = ((fx - w * near[0]) / (1 - w), (fy - w * near[1]) / (1 - w))
    ux, uy = math.cos(math.radians(dir_g)), -math.sin(math.radians(dir_g))
    out.update(regime='pull', dir_g=dir_g, w=w, P=far[0] * ux + far[1] * uy, perp=-far[0] * uy + far[1] * ux)
    return out


def pairs_from_sessions(prefix):
    for s in cap.discover(prefix):
        if not s.usable:
            continue
        for a, b in cap.consecutive(s.frames):
            if eligible(a, b):
                yield s.name, a, b


def summarise(rows):
    pull = [r for r in rows if r.get('regime') == 'pull']
    out = {'pairs': len(rows), 'pull_pairs': len(pull),
           'near_pairs_d_le_16': sum(1 for r in rows if r.get('regime', '').startswith('d<=16'))}
    if not pull:
        return out
    P = [r['P'] for r in pull]
    perp = [r['perp'] for r in pull]
    out['P'] = {'mean': statistics.fmean(P), 'median': statistics.median(P), 'stdev': statistics.pstdev(P),
                'min': min(P), 'max': max(P)}
    out['perp_rms'] = math.sqrt(sum(x * x for x in perp) / len(perp))
    # linear dependence of P on d and on speed (least squares, one regressor each)
    for key in ('d', 'speed_px_step'):
        xs = [r[key] for r in pull]
        mx, my = statistics.fmean(xs), statistics.fmean(P)
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx > 1e-12:
            b = sum((x - mx) * (y - my) for x, y in zip(xs, P)) / sxx
            out['P_vs_' + key] = {'slope': b, 'intercept': my - b * mx}
    # candidate formulas (from the partially decoded L661-L677) vs implied P
    cands = {'pull_power (5)': lambda r: m.PULL_POWER,
             'weave_pull_power*clamp(d/10,0,1)': lambda r: m.WEAVE_PULL_POWER * min(max(r['d'] / 10, 0), 1)}
    out['candidates_rms'] = {k: math.sqrt(sum((f(r) - r['P']) ** 2 for r in pull) / len(pull)) for k, f in cands.items()}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix', nargs='?', default='W')
    ap.add_argument('--bbox-offset', type=float, default=0.0)
    ap.add_argument('--json')
    a = ap.parse_args()
    report = {'capture_root': cap.CAPTURE_ROOT, 'prefix': a.prefix, 'bbox_offset': a.bbox_offset}
    pairs = list(pairs_from_sessions(a.prefix))
    if not pairs:
        print('no eligible grapple frames in usable %s* captures under %s' % (a.prefix, cap.CAPTURE_ROOT))
        return 1
    for label, drag in (('A_with_air_drag', True), ('B_without_air_drag', False)):
        rows = [dict(implied(x, y, a.bbox_offset, drag), session=n) for n, x, y in pairs]
        report[label] = summarise(rows)
        report[label + '_rows'] = rows if a.json else None
        print('hypothesis %s:' % label)
        print(json.dumps(report[label], indent=1))
    if a.json:
        with open(a.json, 'w') as f:
            json.dump(report, f, indent=1)
        print('->', a.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
