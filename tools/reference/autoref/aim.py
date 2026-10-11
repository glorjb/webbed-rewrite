"""Aim measurement: sighting-shot geometry and aim-scan analysis (pure functions, unit-tested).

Live finding (2026-10-11): player.crosshair_tx/ty did not follow the cursor and oIO.aimx/aimy are not
variables of oIO, so neither is usable as aim feedback. Neither is assumed any more.

What IS known (oPlayer_Step_0 L183-L219, lifted, High): on firing, the shot velocity is
    (gh, gv) = lengthdir(web_shot_speed, point_direction(sx, sy, crosshair_tx, crosshair_ty)),
and the probe records gh, gv, sx, sy every frame. So every shot reveals the direction from the shot origin to
wherever the game's aim point was. With the cursor at known client positions m_i, each shot gives a screen-space
ray "origin + t * (gh, gv) passes through m_i" IF the game maps the cursor to the room with a uniform-scale view
(no rotation; letterboxing is fine). Two or more non-parallel rays intersect at the shot origin's screen position
P; the residual of a least-squares intersection over >= 3 rays tests that assumption. Aiming at angle theta is
then: cursor at P + R * (cos theta, -sin theta). Every real shot's direction is checked afterwards.

aim-scan (no firing): every numeric instance variable of oIO and oPlayer is recorded while the runner moves the
cursor; `analyze_aimscan` fits each against the cursor and reports which (if any) follow it, and whether the
game moves the cursor itself (recentring / clipping / DPI scaling) - the cause of the false takeover aborts.
"""
import math


def _num(*vs):
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vs)


def shot_direction(pv):
    """Unit (dx, dy) of the shot (y down) and its GameMaker angle, from the player's variables:
    the launch vector (gh, gv) if it is set, else the travelled vector (gx - sx, gy - sy): the shot moves on a
    straight line from its origin (sx, sy), so both give the same direction. The fallback covers a shot that
    attached within its first frame, if the game clears gh/gv on attachment (not known yet)."""
    gh, gv = pv.get('gh'), pv.get('gv')
    if _num(gh, gv) and math.hypot(gh, gv) > 1e-6:
        dx, dy = gh, gv
    else:
        gx, gy, sx, sy = pv.get('gx'), pv.get('gy'), pv.get('sx'), pv.get('sy')
        if not (_num(gx, gy, sx, sy) and math.hypot(gx - sx, gy - sy) >= 2.0):
            return None
        dx, dy = gx - sx, gy - sy
    n = math.hypot(dx, dy)
    return (dx / n, dy / n), math.degrees(math.atan2(-dy, dx)) % 360


def angle_diff(a, b):
    return ((a - b + 540.0) % 360.0) - 180.0


def ray_intersection(rays):
    """Least-squares point P closest to every line through (mx, my) with direction (dx, dy).
    Returns dict(P, rms_px, max_px, min_t, spread_deg) or None when the rays are (nearly) parallel.
    min_t > 0 means every cursor point lies AHEAD of P along its shot (as it must)."""
    a11 = a12 = a22 = b1 = b2 = 0.0
    lines = []
    for mx, my, dx, dy in rays:
        n = math.hypot(dx, dy)
        dx, dy = dx / n, dy / n
        nx, ny = -dy, dx
        c = nx * mx + ny * my
        a11 += nx * nx
        a12 += nx * ny
        a22 += ny * ny
        b1 += nx * c
        b2 += ny * c
        lines.append((mx, my, dx, dy, nx, ny, c))
    det = a11 * a22 - a12 * a12
    angs = [math.degrees(math.atan2(-l[3], l[2])) for l in lines]
    spread = max(abs(angle_diff(x, y)) for x in angs for y in angs) if len(angs) > 1 else 0.0
    if len(rays) < 2 or abs(det) < 1e-6 or spread < 5.0:
        return {'P': None, 'spread_deg': spread, 'reason': 'shot directions are (nearly) parallel'}
    px = (a22 * b1 - a12 * b2) / det
    py = (a11 * b2 - a12 * b1) / det
    res = [nx * px + ny * py - c for _, _, _, _, nx, ny, c in lines]
    ts = [dx * (mx - px) + dy * (my - py) for mx, my, dx, dy, _, _, _ in lines]
    return {'P': (px, py), 'rms_px': math.sqrt(sum(r * r for r in res) / len(res)), 'max_px': max(map(abs, res)),
            'min_t': min(ts), 'spread_deg': spread}


def cursor_for_angle(P, angle_deg, radius, client_size, margin=3):
    """Client position that puts the aim point in direction `angle_deg` from P; None if it leaves the window."""
    x = P[0] + radius * math.cos(math.radians(angle_deg))
    y = P[1] - radius * math.sin(math.radians(angle_deg))
    w, h = client_size
    if not (margin <= x <= w - margin and margin <= y <= h - margin):
        return None
    return x, y


def best_radius(P, angle_deg, client_size, preferred=160, minimum=40):
    """Largest radius <= preferred that keeps the cursor inside the window (aim precision grows with radius)."""
    r = preferred
    while r >= minimum:
        if cursor_for_angle(P, angle_deg, r, client_size):
            return r
        r -= 10
    return None


# ------------------------------------------------------------------------------------------ aim scan
def _fit3(xs, ys, zs):
    """z = a + b x + c y (least squares). Returns (a, b, c, r2) or None."""
    n = len(zs)
    if n < 4:
        return None
    sx, sy, sz = sum(xs), sum(ys), sum(zs)
    sxx = sum(x * x for x in xs)
    syy = sum(y * y for y in ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sxz = sum(x * z for x, z in zip(xs, zs))
    syz = sum(y * z for y, z in zip(ys, zs))
    M = [[n, sx, sy], [sx, sxx, sxy], [sy, sxy, syy]]
    v = [sz, sxz, syz]
    try:
        sol = _solve3(M, v)
    except ZeroDivisionError:
        return None
    a, b, c = sol
    mean = sz / n
    ss_tot = sum((z - mean) ** 2 for z in zs)
    ss_res = sum((z - (a + b * x + c * y)) ** 2 for x, y, z in zip(xs, ys, zs))
    r2 = 1 - ss_res / ss_tot if ss_tot > 1e-12 else 0.0
    return a, b, c, r2


def _solve3(M, v):
    A = [row[:] + [vv] for row, vv in zip(M, v)]
    for i in range(3):
        piv = max(range(i, 3), key=lambda r: abs(A[r][i]))
        if abs(A[piv][i]) < 1e-12:
            raise ZeroDivisionError
        A[i], A[piv] = A[piv], A[i]
        for r in range(3):
            if r != i:
                f = A[r][i] / A[i][i]
                A[r] = [x - f * y for x, y in zip(A[r], A[i])]
    return [A[i][3] / A[i][i] for i in range(3)]


def analyze_aimscan(samples, r2_min=0.98):
    """samples: [{'req': (x, y) | None, 'cur': (x, y) | None, 'vars': {'scope.name': number}, 'phase': str}]
    taken at the END of each cursor hold (so the game has seen the position). Returns the analysis dict."""
    abs_s = [s for s in samples if s.get('phase') == 'absolute' and s.get('req')]
    out = {'samples': len(samples), 'absolute_samples': len(abs_s)}
    # 1. does the cursor stay where the runner puts it?
    pairs = [(s['req'], s['cur']) for s in abs_s if s.get('cur')]
    if pairs:
        errs = [math.hypot(c[0] - r[0], c[1] - r[1]) for r, c in pairs]
        cx = [c[0] for _, c in pairs]
        cy = [c[1] for _, c in pairs]
        rx = [r[0] for r, _ in pairs]
        spread_cur = math.hypot(max(cx) - min(cx), max(cy) - min(cy))
        spread_req = math.hypot(max(rx) - min(rx), max(r[1] for r, _ in pairs) - min(r[1] for r, _ in pairs))
        if max(errs) <= 3:
            mode = 'positional'
        elif spread_req > 50 and spread_cur < 0.1 * spread_req:
            mode = 'recentred_or_locked'
        else:
            fx = _fit3(rx, [r[1] for r, _ in pairs], cx)
            mode = 'scaled_or_offset' if fx and fx[3] > 0.99 else 'moved_by_game'
        out['cursor'] = {'mode': mode, 'max_error_px': round(max(errs), 1), 'median_error_px':
                         round(sorted(errs)[len(errs) // 2], 1), 'actual_spread_px': round(spread_cur, 1),
                         'requested_spread_px': round(spread_req, 1),
                         'fixed_point': [round(sum(cx) / len(cx), 1), round(sum(cy) / len(cy), 1)]
                         if mode == 'recentred_or_locked' else None}
    # 2. which variables follow the cursor (fit against the actual cursor, or the requested one if it is locked)
    use_req = out.get('cursor', {}).get('mode') in ('recentred_or_locked', None)
    pts = [(s['req'] if use_req or not s.get('cur') else s['cur'], s['vars']) for s in abs_s]
    names = sorted({k for _, v in pts for k in v})
    followers, changed = [], []
    for k in names:
        rows = [(p[0], p[1], v[k]) for p, v in pts if isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool)]
        if len(rows) < 4:
            continue
        zs = [r[2] for r in rows]
        if max(zs) - min(zs) < 1e-9:
            continue
        changed.append(k)
        f = _fit3([r[0] for r in rows], [r[1] for r in rows], zs)
        if f and f[3] >= r2_min and max(abs(f[1]), abs(f[2])) > 1e-4:
            followers.append({'var': k, 'r2': round(f[3], 5), 'a': f[0], 'b_per_px_x': f[1], 'c_per_px_y': f[2],
                              'axis': 'x' if abs(f[1]) >= abs(f[2]) else 'y'})
    followers.sort(key=lambda f: -f['r2'])
    out['fit_against'] = 'requested cursor' if use_req else 'actual cursor'
    out['variables_that_changed'] = changed
    out['followers'] = followers
    rel = [s for s in samples if s.get('phase') == 'relative']
    rel_changed = []
    for k in sorted({k for s in rel for k in s['vars']}):
        vals = [s['vars'][k] for s in rel if isinstance(s['vars'].get(k), (int, float))]
        if len(vals) > 1 and max(vals) - min(vals) > 1e-9:
            rel_changed.append(k)
    out['variables_that_changed_with_relative_motion'] = rel_changed
    bx = next((f for f in followers if f['axis'] == 'x'), None)
    by = next((f for f in followers if f['axis'] == 'y'), None)
    if bx and by:
        out['feedback_pair'] = [bx['var'], by['var']]
        out['recommendation'] = ('%s / %s follow the cursor (R^2 %.4f / %.4f): usable as closed-loop aim feedback '
                                 '(confirm with -AimSight)' % (bx['var'], by['var'], bx['r2'], by['r2']))
    else:
        out['feedback_pair'] = None
        out['recommendation'] = ('no instance variable of oIO / oPlayer follows the cursor: aim with sighting shots '
                                 '(-AimSight / automatic before each web scenario)')
    return out
