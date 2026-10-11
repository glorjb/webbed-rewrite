"""Walking analysis of original-game captures (M2), including FAILED automated recordings. Read-only.

    python tools/reference/analyze_walk.py <session_dir> [<session_dir> ...] [--tests] [--json out.json]

For every session:
  1. sampling consistency: one player instance id throughout?  instance x/y == Box2D body position?  does the
     position change match the recorded velocity (dx ~ vx * h)?  torn / skipped rows.  -> are pose and velocity
     sampled from the right live player instance, every frame?
  2. every walking-input phase (oIO.hm held): frames, displacement, peak |vx|, support (ground / web / air),
     ground instances and angles, locked / slipping / webwalk / attached values, and a classification measured
     from those variables only: moved | blocked_on_ground | blocked_steep (|ground_angle| > 30) | airborne |
     player_missing | locked. Nothing is inferred beyond what the game's own variables show.
  3. support transitions (ground instance / angle changes, lift-offs) with position and travel from the start.
  4. walking on LEVEL ground (|ground_angle| <= 5, hm held): plateau speed (px/step) vs the recovered
     max_walkspd = 3.5 (a hypothesis), frames to 90 % of the plateau, stopping after release.
  5. free flight after leaving the ground: the verified drag model (sim/webbed_model.player_free_flight_dv)
     against the recorded velocity change, as tests/test_reference_captures.test_free_fall_gravity does.
  6. --tests: copies (never moves) the sessions into a temporary capture root and runs the regression suite
     there, so their real data is checked against the recovered model while the originals stay preserved.
"""
import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'tests'))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'sim'))
import capture_lib as cap   # noqa: E402
import webbed_model as m    # noqa: E402

LEVEL_DEG = 5.0
STEEP_DEG = 30.0
H = 1.0 / m.ROOM_SPEED


def _p(r):
    return r.get('player') or {}


def _b(r):
    return _p(r).get('body')


def _v(r):
    return _p(r).get('vars') or {}


def _hm(r):
    v = (r.get('io') or {}).get('hm')
    return v if isinstance(v, (int, float)) else 0


def _ang(v):
    a = v.get('ground_angle')
    if not isinstance(a, (int, float)):
        return None
    return ((a + 180.0) % 360.0) - 180.0


def sampling(frames):
    ids = sorted({_p(r).get('id') for r in frames if _p(r)})
    xy_err, vel_res = [], []
    for r in frames:
        b, p = _b(r), _p(r)
        if b and p.get('xy'):
            xy_err.append(math.hypot(b['pos_px'][0] - p['xy'][0], b['pos_px'][1] - p['xy'][1]))
    for a, b in cap.consecutive(frames):
        ba, bb = _b(a), _b(b)
        if ba and bb and _p(a).get('id') == _p(b).get('id'):
            dx = bb['pos_px'][0] - ba['pos_px'][0]
            vel_res.append(abs(dx - bb['vel_pxps'][0] * H))       # Box2D: x_{n+1} = x_n + h * v_{n+1}
    out = {'player_ids': ids, 'rows': len(frames), 'rows_with_player': sum(1 for r in frames if _b(r)),
           'torn': sum(r['torn'] for r in frames), 'skipped': sum(r['skipped'] for r in frames[1:])}
    if xy_err:
        out['instance_xy_vs_body_px'] = {'median': round(statistics.median(xy_err), 4), 'max': round(max(xy_err), 4)}
    if vel_res:
        out['dx_vs_vx_h_residual_px'] = {'median': round(statistics.median(vel_res), 4),
                                         'p99': round(sorted(vel_res)[int(0.99 * (len(vel_res) - 1))], 4)}
    ok = len(ids) == 1 and (not xy_err or max(xy_err) < 1.0) and (not vel_res or statistics.median(vel_res) < 0.05)
    out['verdict'] = ('consistent: one player instance; body and instance agree; position follows velocity' if ok else
                      'CHECK: see the fields above (several player ids, body/instance mismatch or dx != vx*h)')
    return out


def phases(frames):
    """Walking-input phases: consecutive rows with the same sign of oIO.hm (non-zero)."""
    out, cur = [], None
    for i, r in enumerate(frames):
        s = (1 if _hm(r) > 0.5 else -1 if _hm(r) < -0.5 else 0)
        if s and (cur is None or cur['dir'] != s):
            if cur:
                out.append(cur)
            cur = {'dir': s, 'start': i, 'end': i}
        elif s:
            cur['end'] = i
        elif cur:
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return out


def classify_phase(frames, ph, x0):
    rows = frames[ph['start']:ph['end'] + 1]
    bodies = [r for r in rows if _b(r)]
    res = {'direction': 'right' if ph['dir'] > 0 else 'left', 'frames': len(rows),
           'first_frame': rows[0]['frame'], 'last_frame': rows[-1]['frame']}
    if not bodies:
        res['class'] = 'player_missing'
        return res
    xa, xb = bodies[0]['player']['body']['pos_px'][0], bodies[-1]['player']['body']['pos_px'][0]
    vx = [abs(r['player']['body']['vel_pxps'][0]) / m.ROOM_SPEED for r in bodies]
    sup = [cap.support(r) or 'air' for r in bodies]
    angs = [a for a in (_ang(_v(r)) for r in bodies) if a is not None]
    res.update(dx_px=round(xb - xa, 1), start_x_from_session_start=round(xa - x0, 1), peak_vx_px_step=round(max(vx), 3),
               support={k: sup.count(k) for k in sorted(set(sup))},
               ground_ids=sorted({_v(r).get('ground') for r in bodies if cap.support(r) == 'ground'}, key=str)[:8],
               ground_angle_deg=[round(min(angs), 1), round(max(angs), 1)] if angs else None,
               locked=sorted({str(_v(r).get('locked')) for r in bodies}),
               slipping=sorted({str(_v(r).get('slipping')) for r in bodies}),
               webwalk=sorted({str(_v(r).get('webwalk')) for r in bodies})[:4],
               attached=sorted({str(_v(r).get('attached')) for r in bodies})[:4],
               player_ids=sorted({_p(r).get('id') for r in bodies}))
    early = bodies[:30]
    moved_early = abs(early[-1]['player']['body']['pos_px'][0] - early[0]['player']['body']['pos_px'][0]) >= 2.0
    if any(str(_v(r).get('locked')) not in ('0', '0.0', 'None', 'False') for r in early):
        res['class'] = 'locked'
    elif sup.count('air') > len(sup) / 2:
        res['class'] = 'airborne'
    elif moved_early or abs(xb - xa) >= 2.0:
        res['class'] = 'moved'
    elif angs and max(abs(a) for a in angs) > STEEP_DEG:
        res['class'] = 'blocked_steep'
    else:
        res['class'] = 'blocked_on_ground'
    res['first_30_frames_dx_px'] = round(early[-1]['player']['body']['pos_px'][0] - early[0]['player']['body']['pos_px'][0], 2)
    return res


def transitions(frames, x0):
    out, prev = [], None
    for r in frames:
        if not _b(r):
            continue
        key = (cap.support(r) or 'air', _v(r).get('ground'))
        a = _ang(_v(r))
        if prev is None or key != prev[0] or (a is not None and prev[1] is not None and abs(a - prev[1]) > 10):
            x, y = _b(r)['pos_px']
            out.append({'frame': r['frame'], 'support': key[0], 'ground': key[1], 'ground_angle': None if a is None
                        else round(a, 1), 'x_from_start': round(x - x0, 1), 'y': round(y, 1), 'hm': _hm(r),
                        'vel_pxps': [round(c, 1) for c in _b(r)['vel_pxps']]})
            prev = (key, a)
    return out


def level_walk(frames, phs):
    out = []
    for ph in phs:
        rows = [r for r in frames[ph['start']:ph['end'] + 1] if _b(r)]
        lv = [r for r in rows if cap.support(r) == 'ground' and _ang(_v(r)) is not None and abs(_ang(_v(r))) <= LEVEL_DEG]
        if len(lv) < 20:
            continue
        sp = [abs(r['player']['body']['vel_pxps'][0]) / m.ROOM_SPEED for r in lv]
        plateau = statistics.median(sorted(sp)[len(sp) // 2:])
        t90 = next((i for i, v in enumerate(sp) if v >= 0.9 * plateau), None)
        after = [r for r in frames[ph['end'] + 1:ph['end'] + 121] if _b(r)]
        stop = next((i for i, r in enumerate(after) if abs(r['player']['body']['vel_pxps'][0]) / m.ROOM_SPEED < 0.05), None)
        out.append({'direction': 'right' if ph['dir'] > 0 else 'left', 'level_frames': len(lv),
                    'plateau_px_step': round(plateau, 4), 'max_walkspd_hypothesis': m.MAX_WALKSPD,
                    'diff': round(plateau - m.MAX_WALKSPD, 4), 'frames_to_90pct': t90,
                    'frames_to_stop_after_release': stop})
    return out


def free_flight(frames, s):
    segs = cap.web_segments(s.webs)
    res = []
    for a, b in cap.consecutive(frames):
        if not (cap.free_flight(a, segs) and cap.free_flight(b, segs)):
            continue
        va, vb = a['player']['body']['vel_pxps'], b['player']['body']['vel_pxps']
        if abs(vb[1] - va[1]) > 3 * m.gravity_dv_pxps_per_step():
            continue
        ex, ey = m.player_free_flight_dv(va[0], va[1])
        res.append((vb[0] - va[0] - ex, vb[1] - va[1] - ey))
    if not res:
        return {'pairs': 0}
    rms = lambda xs: math.sqrt(sum(x * x for x in xs) / len(xs))   # noqa: E731
    return {'pairs': len(res), 'rms_dvx_residual_pxps': round(rms([r[0] for r in res]), 6),
            'rms_dvy_residual_pxps': round(rms([r[1] for r in res]), 6),
            'max_abs_residual_pxps': round(max(max(abs(r[0]), abs(r[1])) for r in res), 6),
            'test_tolerance_pxps': 0.05}


def analyze(path):
    s = cap.Session(path)
    fr = s.frames
    first = next((r for r in fr if _b(r)), None)
    x0 = first['player']['body']['pos_px'][0] if first else 0.0
    phs = phases(fr)
    out = {'session': s.name, 'quality': s.summary(), 'usable': s.usable, 'sampling': sampling(fr),
           'start': None if not first else {
               'frame': first['frame'], 'pos': [round(c, 1) for c in first['player']['body']['pos_px']],
               'support': cap.support(first), 'ground': _v(first).get('ground'), 'ground_angle': _ang(_v(first)),
               'room': first.get('room')},
           'phases': [classify_phase(fr, ph, x0) for ph in phs], 'transitions': transitions(fr, x0)[:60],
           'level_walk': level_walk(fr, phs), 'free_flight': free_flight(fr, s)}
    xs = [r['player']['body']['pos_px'][0] - x0 for r in fr if _b(r)]
    out['travel'] = {'max_right_px': round(max(xs), 1), 'max_left_px': round(-min(xs), 1), 'final_px': round(xs[-1], 1)} if xs else None
    auto = os.path.join(path, 'auto_run.json')
    if os.path.exists(auto):
        with open(auto) as f:
            a = json.load(f)
        out['runner'] = {'status': a.get('status'), 'failure': a.get('failure'), 'ground_profile_confidence':
                         (a.get('ground_profile') or {}).get('confidence'), 'ground_scan_check': a.get('ground_scan_check')}
    return out


def run_tests_on_copies(paths):
    tmp = tempfile.mkdtemp(prefix='dp_walk_')
    root = os.path.join(tmp, 'captures')
    os.makedirs(root)
    for p in paths:
        shutil.copytree(p, os.path.join(root, os.path.basename(os.path.normpath(p))))
    script = os.path.join(HERE, 'autoref', 'testrun.py')
    out_json = os.path.join(tmp, 'tests.json')
    proc = subprocess.run([sys.executable, '-I', script, '--json', out_json, '--pattern', 'test_reference_captures.py'],
                          env=dict(os.environ, DP_CAPTURES=root), capture_output=True, text=True)
    try:
        with open(out_json) as f:
            res = json.load(f)
    except (OSError, ValueError):
        res = {'error': (proc.stdout + proc.stderr)[-3000:]}
    shutil.rmtree(tmp, ignore_errors=True)
    return res


def print_report(a):
    print('=' * 100)
    print('session %s   %s' % (a['session'], 'usable' if a['usable'] else 'NOT usable by the quality gate'))
    print('  quality: %s' % a['quality'])
    if a.get('runner'):
        print('  runner: %s' % a['runner'])
    print('  start: %s' % a['start'])
    print('  sampling: %s' % a['sampling'])
    print('  travel: %s' % a['travel'])
    print('  walking-input phases:')
    for p in a['phases']:
        print('    %-5s %-18s frames %-4d dx %7s  first30 dx %6s  peak|vx| %5s  support %s  angle %s  ground %s  '
              'locked %s slipping %s webwalk %s ids %s' % (
                  p['direction'], p.get('class'), p['frames'], p.get('dx_px'), p.get('first_30_frames_dx_px'),
                  p.get('peak_vx_px_step'), p.get('support'), p.get('ground_angle_deg'), p.get('ground_ids'),
                  p.get('locked'), p.get('slipping'), p.get('webwalk'), p.get('player_ids')))
    print('  support transitions (frame, support, ground id, angle, x from start, y, hm, vel):')
    for t in a['transitions'][:30]:
        print('    %(frame)d %(support)-6s %(ground)-8s %(ground_angle)-6s %(x_from_start)8s %(y)8s hm=%(hm)s v=%(vel_pxps)s' % t)
    print('  level-ground walking: %s' % (a['level_walk'] or 'no phase with >= 20 level-ground frames'))
    print('  free flight vs the verified drag model: %s' % a['free_flight'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('sessions', nargs='+')
    ap.add_argument('--tests', action='store_true', help='run test_reference_captures on COPIES of the sessions')
    ap.add_argument('--json')
    a = ap.parse_args()
    reports = [analyze(p) for p in a.sessions]
    for r in reports:
        print_report(r)
    if len(reports) > 1:
        print('=' * 100)
        print('comparison of starts (same spot?): ')
        for r in reports:
            print('  %-40s %s' % (r['session'], r['start']))
    out = {'sessions': reports}
    if a.tests:
        t = run_tests_on_copies(a.sessions)
        out['tests_on_copies'] = t
        print('=' * 100)
        print('regression tests on COPIES of these sessions (originals untouched):')
        for x in t.get('tests', []):
            det = (x.get('first_failures') or [x.get('detail') or ''])[0]
            print('  %-5s %s %s' % (x['outcome'].upper(), x['id'].split('.')[-1], str(det).strip().splitlines()[-1:] if det else ''))
        if t.get('counts'):
            print('  totals: %(pass)d pass, %(fail)d fail, %(error)d error, %(skip)d skip' % t['counts'])
        elif t.get('error'):
            print(t['error'])
    if a.json:
        with open(a.json, 'w') as f:
            json.dump(out, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    sys.exit(main())
