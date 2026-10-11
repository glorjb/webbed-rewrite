"""Metrics + regression comparison for flat player traces (CSV).

CSV columns: frame, pos_x_px, pos_y_px, vel_x_pxps, vel_y_pxps, angle_rad. Use it for reconstruction traces;
original-game captures (JSONL from webbed_probe.py) are checked by tests/test_reference_captures.py.

    python trace_metrics.py metrics original.csv
    python trace_metrics.py compare original.csv reconstruction.csv [--pos-tol 0.5] [--vel-tol 1.0]

`compare` aligns frames from the first frame where |velocity| changes (input onset) and reports
per-frame position/velocity error. It exits non-zero when tolerances are exceeded, so it can be a
regression test. Tolerances are in pixels and pixels/second.
"""
import argparse
import csv
import math
import sys


def load(path):
    with open(path, newline='') as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k, v in r.items():
            try:
                r[k] = float(v)
            except (TypeError, ValueError):
                pass
    return rows


def onset(rows, thresh=1e-3):
    for i in range(1, len(rows)):
        dv = math.hypot(rows[i]['vel_x_pxps'] - rows[i - 1]['vel_x_pxps'], rows[i]['vel_y_pxps'] - rows[i - 1]['vel_y_pxps'])
        if dv > thresh:
            return i - 1
    return 0


def metrics(rows, fps=60.0):
    vx = [r['vel_x_pxps'] for r in rows]
    vy = [r['vel_y_pxps'] for r in rows]
    y = [r['pos_y_px'] for r in rows]
    out = {
        'frames': len(rows),
        'max_abs_vx_pxps': max(map(abs, vx)) if vx else 0,
        'max_abs_vy_pxps': max(map(abs, vy)) if vy else 0,
        'min_y_px': min(y) if y else 0,
        'max_y_px': max(y) if y else 0,
    }
    # frames for |vx| to reach 90% of its max (walking acceleration)
    if vx and out['max_abs_vx_pxps'] > 0:
        target = 0.9 * out['max_abs_vx_pxps']
        out['frames_to_90pct_vx'] = next((i for i, v in enumerate(vx) if abs(v) >= target), None)
    # largest single-frame upward velocity change (jump impulse) and resulting apex
    dvy = [vy[i] - vy[i - 1] for i in range(1, len(vy))]
    if dvy:
        k = min(range(len(dvy)), key=lambda i: dvy[i])      # most negative = strongest upward change (y down)
        out['jump_frame'] = k + 1
        out['jump_dvy_pxps'] = dvy[k]
        after = y[k + 1:]
        if after:
            apex = min(range(len(after)), key=lambda i: after[i])
            out['jump_apex_rise_px'] = y[k] - after[apex]
            out['jump_frames_to_apex'] = apex
    return out


def compare(a, b, pos_tol, vel_tol):
    ia, ib = onset(a), onset(b)
    n = min(len(a) - ia, len(b) - ib)
    worst = {'pos': 0.0, 'vel': 0.0}
    fails = []
    for k in range(n):
        ra, rb = a[ia + k], b[ib + k]
        pe = math.hypot(ra['pos_x_px'] - rb['pos_x_px'] - (a[ia]['pos_x_px'] - b[ib]['pos_x_px']),
                        ra['pos_y_px'] - rb['pos_y_px'] - (a[ia]['pos_y_px'] - b[ib]['pos_y_px']))
        ve = math.hypot(ra['vel_x_pxps'] - rb['vel_x_pxps'], ra['vel_y_pxps'] - rb['vel_y_pxps'])
        worst['pos'] = max(worst['pos'], pe)
        worst['vel'] = max(worst['vel'], ve)
        if pe > pos_tol or ve > vel_tol:
            fails.append((k, round(pe, 3), round(ve, 3)))
    return n, worst, fails


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('metrics')
    m.add_argument('csv')
    c = sub.add_parser('compare')
    c.add_argument('original')
    c.add_argument('candidate')
    c.add_argument('--pos-tol', type=float, default=0.5)
    c.add_argument('--vel-tol', type=float, default=1.0)
    a = ap.parse_args()
    if a.cmd == 'metrics':
        for k, v in metrics(load(a.csv)).items():
            print('%-24s %s' % (k, v))
        return
    n, worst, fails = compare(load(a.original), load(a.candidate), a.pos_tol, a.vel_tol)
    print('aligned frames: %d  worst pos err: %.3f px  worst vel err: %.3f px/s' % (n, worst['pos'], worst['vel']))
    if fails:
        print('FAIL: %d frames out of tolerance; first: %s' % (len(fails), fails[:5]))
        sys.exit(1)
    print('PASS')


if __name__ == '__main__':
    main()
