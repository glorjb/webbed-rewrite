"""Summarise one capture session (passing or failed) as short text: what the spider did and where it lost support.

    python tools/reference/diagnose_capture.py <session_dir> [--context 12] [--json out.json]

Made for failed automated recordings (DesktopPlaygroundLocal\\captures_failed\\...), so their cause can be read
without sharing the capture: quality, the runner's failure reason and measurements (auto_run.json), the input
log (inputs.jsonl), the support timeline (ground / web / air from the game's own variables), and a frame table
around every loss of support: position, velocity, ground instance and angle, inputs, travel from the start.
Read-only; nothing is changed.
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'tests'))
import capture_lib as cap   # noqa: E402


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def timeline(frames):
    """Runs of identical support: [(support, first_frame, last_frame, n)]."""
    out = []
    for r in frames:
        s = cap.support(r) or ('air' if (r.get('player') or {}).get('body') else 'no-player')
        if out and out[-1][0] == s and r['frame'] == out[-1][2] + 1:
            out[-1][2] = r['frame']
            out[-1][3] += 1
        else:
            out.append([s, r['frame'], r['frame'], 1])
    return out


def row_line(r, x0):
    p = r.get('player') or {}
    b = p.get('body') or {}
    v = p.get('vars') or {}
    io = r.get('io') or {}
    pos, vel = b.get('pos_px') or [math.nan] * 2, b.get('vel_pxps') or [math.nan] * 2
    return ('%7d %-7s x=%8.1f y=%8.1f dx=%7.1f vx=%7.1f vy=%7.1f ground=%-7s gang=%-6s webwalk=%-6s hm=%-4s jmp=%-2s '
            'slip=%-2s landed=%s' % (r['frame'], cap.support(r) or 'air', pos[0], pos[1], pos[0] - x0, vel[0], vel[1],
                                     v.get('ground'), v.get('ground_angle'), v.get('webwalk'), io.get('hm'),
                                     io.get('jmp'), v.get('slipping'), v.get('landed')))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('session')
    ap.add_argument('--context', type=int, default=12)
    ap.add_argument('--json')
    a = ap.parse_args()
    s = cap.Session(a.session)
    print('session: %s' % s.name)
    print('quality: %s' % s.summary())
    auto = load_json(os.path.join(a.session, 'auto_run.json'))
    if auto:
        print('runner result: %s %s' % (auto.get('status'), auto.get('failure')))
        for k in ('ground_profile', 'sighting'):
            if auto.get(k):
                print('%s: %s' % (k, json.dumps(auto[k], default=str)[:1500]))
        obs = auto.get('observations') or {}
        for k in ('left_ground', 'aim', 'aim_check', 'walk_metrics'):
            if obs.get(k):
                print('observation %s: %s' % (k, json.dumps(obs[k], default=str)[:1500]))
        print('inputs: %s' % json.dumps(auto.get('inputs'), default=str)[:800])
    inputs = []
    ip = os.path.join(a.session, 'inputs.jsonl')
    if os.path.exists(ip):
        with open(ip) as f:
            inputs = [json.loads(line) for line in f if line.strip()]
        print('input events (%d):' % len(inputs))
        for e in inputs[:60]:
            print('  ' + json.dumps(e, default=str)[:200])
    fr = [r for r in s.frames]
    if not fr:
        return 1
    first = next((r for r in fr if (r.get('player') or {}).get('body')), None)
    x0 = first['player']['body']['pos_px'][0] if first else 0.0
    tl = timeline(fr)
    print('support timeline (support, first frame, last frame, frames):')
    for t in tl[:80]:
        print('  %-9s %7d .. %7d  (%d)' % tuple(t))
    losses = [t for i, t in enumerate(tl) if i and t[0] in ('air', 'no-player') and tl[i - 1][0] in ('ground', 'web')]
    print('losses of support: %d' % len(losses))
    by_frame = {r['frame']: r for r in fr}
    report = {'session': s.name, 'quality': s.summary(), 'timeline': tl, 'losses': []}
    for t in losses[:6]:
        f0 = t[1]
        print('--- support lost at frame %d (%s for %d frames) ---' % (f0, t[0], t[3]))
        rows = [by_frame[f] for f in range(f0 - a.context, f0 + a.context + 1) if f in by_frame]
        for r in rows:
            print(row_line(r, x0))
        report['losses'].append({'frame': f0, 'frames_unsupported': t[3],
                                 'rows': [row_line(r, x0) for r in rows]})
    xs = [r['player']['body']['pos_px'][0] for r in fr if (r.get('player') or {}).get('body')]
    if xs:
        print('x range: %.1f .. %.1f (start %.1f; max travel right %.1f, left %.1f px)'
              % (min(xs), max(xs), x0, max(xs) - x0, x0 - min(xs)))
    if a.json:
        with open(a.json, 'w') as f:
            json.dump(report, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    sys.exit(main())
