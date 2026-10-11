"""Compare free-flight physics hypotheses against original-game captures (M* scenarios).

    python tools/reference/analyze_free_flight.py [prefix=M] [--plot out.png]

Selects consecutive, untorn free-flight frame pairs exactly as tests/test_reference_captures.py does,
then reports RMS / max residuals of the next-frame vertical velocity for:
  H0  gravity only, damping 0
  H1  Box2D linear damping c (editor value 0.1, and best-fit c)
  H2  gravity + scripted quadratic air drag (oPlayer_Step_0 L1575-1583, recovered model)
  H2f same form with a blind least-squares drag coefficient (independent check of the constant)
The plot shows dv_y - g*h against v_y with each hypothesis' prediction.
"""
import argparse
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'tests'))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'sim'))
import capture_lib as cap   # noqa: E402
import webbed_model as m    # noqa: E402

H = 1.0 / m.ROOM_SPEED
G = m.GRAVITY_MPS2[1] / m.M_PER_PX        # px/s^2


def pairs(prefix):
    out = []
    for s in cap.discover(prefix):
        if not s.usable:
            continue
        segs = cap.web_segments(s.webs)
        for a, b in cap.consecutive(s.frames):
            if not (cap.free_flight(a, segs) and cap.free_flight(b, segs)):
                continue
            va, vb = a['player']['body']['vel_pxps'], b['player']['body']['vel_pxps']
            if abs(vb[1] - va[1]) > 3 * G * H:
                continue
            out.append((va[0], va[1], vb[0], vb[1]))
    return out


def stats(P, f):
    r = [f(vx, vy) - vy2 for vx, vy, _, vy2 in P]
    return math.sqrt(sum(x * x for x in r) / len(r)), max(abs(x) for x in r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix', nargs='?', default='M')
    ap.add_argument('--plot')
    a = ap.parse_args()
    P = pairs(a.prefix)
    if not P:
        sys.exit('no usable free-flight pairs')
    print('free-flight pairs: %d   vy range %.1f .. %.1f px/s' % (len(P), min(p[1] for p in P), max(p[1] for p in P)))
    hyps = {
        'H0 gravity only': lambda vx, vy: vy + G * H,
        'H1 linear damping c=0.1 (editor)': lambda vx, vy: (vy + G * H) / (1 + H * 0.1),
    }
    best_c = min((i * 0.001 for i in range(400)), key=lambda c: stats(P, lambda vx, vy: (vy + G * H) / (1 + H * c))[0])
    hyps['H1 linear damping c=%.3f (best fit)' % best_c] = lambda vx, vy: (vy + G * H) / (1 + H * best_c)
    hyps['H2 recovered model (drag 0.005)'] = lambda vx, vy: vy + m.player_free_flight_dv(vx, vy)[1]
    # blind fit of the quadratic coefficient k (1/px): a = g - k*vy*|v|
    num = sum((G * H - (vy2 - vy)) * H * vy * math.hypot(vx, vy) for vx, vy, _, vy2 in P)
    den = sum((H * vy * math.hypot(vx, vy)) ** 2 for vx, vy, _, _ in P)
    k = num / den
    hyps['H2f quadratic, blind fit k=%.4e /px' % k] = lambda vx, vy: vy + H * (G - k * vy * math.hypot(vx, vy))
    k_pred = m.PLAYER_AIR_DRAG / (m.ROOM_SPEED ** 2 * m.player_mass_kg() * m.M_PER_PX)
    print('%-40s %12s %12s' % ('hypothesis', 'RMS px/s', 'max px/s'))
    for name, f in hyps.items():
        print('%-40s %12.5f %12.5f' % ((name,) + stats(P, f)))
    print('drag coefficient: blind fit k = %.4e /px, recovered constant 0.005 N -> k = %.4e /px (%.2f%% diff)' % (
        k, k_pred, 100 * (k - k_pred) / k_pred))
    if a.plot:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        vy = [p[1] for p in P]
        obs = [p[3] - p[1] - G * H for p in P]
        fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=120)
        ax.scatter(vy, obs, s=8, color='#222222', label='original game (M3 capture)', zorder=3)
        xs = sorted(vy)
        for name, f, col in (('H0 gravity only', hyps['H0 gravity only'], '#999999'),
                             ('H1 linear damping 0.1', hyps['H1 linear damping c=0.1 (editor)'], '#d95f02'),
                             ('H2 recovered drag 0.005', hyps['H2 recovered model (drag 0.005)'], '#1b9e77')):
            ax.plot(xs, [f(0.0, v) - v - G * H for v in xs], color=col, lw=1.6, label=name)
        ax.axhline(0, color='#cccccc', lw=0.8)
        ax.set_xlabel('vertical velocity v_y before step (px/s, +down)')
        ax.set_ylabel('dv_y - g*h  (px/s per step)')
        ax.set_title('Player free flight: velocity change beyond gravity')
        ax.legend(frameon=False, fontsize=8)
        fig.tight_layout()
        fig.savefig(a.plot)
        print('plot ->', a.plot)


if __name__ == '__main__':
    main()
