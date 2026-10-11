"""Read-only estimate of walkable ground around the spider, BEFORE any input is sent (M2 setup validation).

History
  v1 (2026-10-11) boxed every solid with an axis-aligned box and required every next piece's box top to be
  within 8 px of the foot. Live it reported 64 px clear to the right and 40 px to the left, while an earlier real
  recording had walked about 296 px right and 366 px left. Likely causes (each handled below, and
  measurable with compare_with_capture):
    * rotated pieces: an axis-aligned box's top is the highest corner, not the surface under the spider;
    * small steps between adjacent pieces treated as an edge or a wall;
    * solids whose collision shape is not their sprite rectangle (polygon / circle fixtures, or no sprite).
  v2 (this) uses the exact rotated rectangle, allows steps up to the spider's radius, and marks the scan
  'ambiguous' instead of 'estimate' when a solid near the path has a shape it cannot model.

Model (an estimate, stated with every scan)
  * solids = instances of objects descending from oSolid / oPlatform with physics enabled (objects.json);
  * a box-shaped fixture (objects.json shape 1) = the sprite rectangle (sprites.json w, h, origin) scaled by
    image_xscale / image_yscale and rotated by image_angle: an exact rotated quadrilateral ('exact');
  * any other shape (polygon / circle) or a missing sprite = 'inexact': its geometry is unknown;
  * image_yscale at CInstance+0xA8 (between the verified image_xscale +0xA4 and image_angle +0xAC);
  * the foot point (player x, body bottom) is walked in 4 px steps; support = an exact solid whose surface at
    x is within STEP_UP above or STEP_DOWN below the current foot level; an exact solid that reaches above
    STEP_UP and covers the body height at the leading edge is a wall (the spider would climb it); a level
    change of more than FLAT_PX from the start ends the flat section.
Confidence: 'estimate' (enforced by the runner) only when the spider's own ground is an exact solid at its feet
and no inexact solid lies in the walking corridor; 'ambiguous' or 'unavailable' otherwise (the runner skips
unless -TrustGround, a clearly marked diagnostic mode).
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
INV = os.path.join(HERE, '..', '..', '..', 'docs', 'inventory')
STEP = 4.0
PLAYER_R = 11.0
STEP_UP = PLAYER_R          # steps up to the body radius are walked over
STEP_DOWN = PLAYER_R
FLAT_PX = 24.0              # cumulative level change that ends a "flat" section
CORRIDOR_Y = 48.0           # inexact solids within this height of the foot level make the scan ambiguous
SOLID_ROOTS = ('oSolid', 'oPlatform')


def load_inventory():
    with open(os.path.join(INV, 'objects.json')) as f:
        objs = {o['name']: o for o in json.load(f)}
    with open(os.path.join(INV, 'sprites.json')) as f:
        sprites = {}
        for s in json.load(f):
            sprites.setdefault(s['name'], s)
    return objs, sprites


def solid_object_names(objs):
    """Objects descending from oSolid / oPlatform with physics enabled (with or without a sprite)."""
    out = set()
    for name, o in objs.items():
        seen, cur = set(), name
        while cur and cur not in seen:
            if cur in SOLID_ROOTS:
                if o.get('physics'):
                    out.add(name)
                break
            seen.add(cur)
            cur = (objs.get(cur) or {}).get('parent')
    return out


def instance_poly(x, y, xscale, yscale, angle_deg, sprite):
    """Corners of the sprite rectangle placed like GameMaker draws it (origin, scale, rotation; y down)."""
    ox, oy, w, h = sprite['ox'], sprite['oy'], sprite['w'], sprite['h']
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    pts = []
    for cx, cy in ((-ox, -oy), (w - ox, -oy), (w - ox, h - oy), (-ox, h - oy)):
        px, py = cx * xscale, cy * yscale
        pts.append((x + px * ca + py * sa, y - px * sa + py * ca))   # positive angle = counter-clockwise on screen
    return pts


def instance_box(x, y, xscale, yscale, angle_deg, sprite):
    p = instance_poly(x, y, xscale, yscale, angle_deg, sprite)
    return min(q[0] for q in p), min(q[1] for q in p), max(q[0] for q in p), max(q[1] for q in p)


def span_at(poly, x):
    """(top, bottom) of the polygon on the vertical line at x, or None."""
    ys = []
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if x1 == x2:
            if abs(x - x1) < 1e-9:
                ys += [y1, y2]
            continue
        if min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9:
            ys.append(y1 + (x - x1) / (x2 - x1) * (y2 - y1))
    return (min(ys), max(ys)) if ys else None


def make_solid(sid, obj, x, y, xs, ys, angle, sprite, shape):
    s = {'id': sid, 'obj': obj, 'x': x, 'y': y, 'xscale': xs, 'yscale': ys, 'angle': angle, 'shape': shape,
         'sprite': sprite['name'] if sprite else None}
    if sprite:
        s['poly'] = instance_poly(x, y, xs, ys, angle, sprite)
        s['box'] = (min(q[0] for q in s['poly']), min(q[1] for q in s['poly']),
                    max(q[0] for q in s['poly']), max(q[1] for q in s['poly']))
    s['exact'] = bool(sprite) and shape == 1
    return s


def support_at(solids, x, foot):
    """The exact solid whose surface near x is closest to the foot level within the step limits."""
    best = None
    for s in solids:
        if not s.get('exact'):
            continue
        for xx in (x, x - 1.5, x + 1.5):                 # bridges seams between adjacent pieces
            sp = span_at(s['poly'], xx)
            if sp and foot - STEP_UP <= sp[0] <= foot + STEP_DOWN:
                if best is None or abs(sp[0] - foot) < abs(best[1] - foot):
                    best = (s, sp[0])
                break
    return best


def scan_direction(solids, x0, foot0, direction, limit=1500.0):
    """Walk the foot point from x0 in `direction` (+1 right / -1 left): clear distance, why it ended, the pieces
    crossed, and inexact solids met in the corridor."""
    x, foot, d = x0, foot0, 0.0
    pieces, unknown = [], set()
    end = {'end': 'limit'}
    while d < limit:
        x += direction * STEP
        d += STEP
        lead = x + direction * PLAYER_R
        for s in solids:
            box = s.get('box')
            near = box and box[0] - 2 <= x <= box[2] + 2 and box[1] - CORRIDOR_Y <= foot and box[3] + CORRIDOR_Y >= foot
            if not s.get('exact'):
                if near or (not box and abs(s['x'] - x) <= 64 and abs(s['y'] - foot) <= CORRIDOR_Y + 32):
                    unknown.add((s['id'], s['obj']))
                continue
            sp = span_at(s['poly'], lead)
            if sp and sp[0] < foot - STEP_UP and sp[1] > foot - 2 * PLAYER_R:
                end = {'end': 'wall', 'by': [s['id'], s['obj']]}
                break
        if end['end'] == 'wall':
            break
        sup = support_at(solids, x, foot)
        if sup is None:
            end = {'end': 'edge'}
            break
        s, top = sup
        if not pieces or pieces[-1][0] != s['id']:
            pieces.append([s['id'], s['obj'], round(x, 1)])
        foot = top
        if abs(foot - foot0) > FLAT_PX:
            end = {'end': 'not_flat', 'level_change_px': round(foot - foot0, 1)}
            break
    clear = d - STEP if end['end'] != 'limit' else d
    return dict(end, clear_px=round(clear, 1), at_x=round(x, 1), pieces=pieces,
                unknown_solids=sorted(unknown))


def ground_profile(solids, body_pos, ground_id):
    """{'confidence': 'estimate' | 'ambiguous' | 'unavailable', 'right': {...}, 'left': {...}, ...}."""
    gx, gy = body_pos
    foot = gy + PLAYER_R
    own = [s for s in solids if s.get('id') == ground_id]
    out = {'solids': len(solids), 'inexact_solids': sum(1 for s in solids if not s.get('exact')),
           'ground_id': ground_id, 'player': [round(gx, 1), round(gy, 1)], 'foot_y': round(foot, 1),
           'model': 'groundscan v2: rotated sprite rectangles for box fixtures; steps <= %.0f px' % STEP_UP}
    if not own:
        out.update(confidence='unavailable', reason='the spider\'s ground instance %s is not an oSolid/oPlatform '
                   'physics instance (or the spider is not on solid ground)' % ground_id)
        return out
    g = own[0]
    out['ground_obj'] = g['obj']
    if not g.get('exact'):
        out.update(confidence='ambiguous', reason='the spider stands on %s, whose collision shape is not a box '
                   '(shape %s, sprite %s): geometry unknown' % (g['obj'], g.get('shape'), g.get('sprite')))
        return out
    sp = span_at(g['poly'], gx)
    if not sp or abs(sp[0] - foot) > STEP_UP + 4:
        out.update(confidence='ambiguous', reason='the ground piece %s (%s) has no surface at the spider\'s feet '
                   '(surface %s, feet %.1f): its fixture is not its sprite rectangle'
                   % (g['id'], g['obj'], None if not sp else round(sp[0], 1), foot))
        return out
    right = scan_direction(solids, gx, sp[0], +1)
    left = scan_direction(solids, gx, sp[0], -1)
    out.update(right=right, left=left)
    unknown = sorted(set(map(tuple, right['unknown_solids'] + left['unknown_solids'])))
    if unknown:
        out.update(confidence='ambiguous', reason='solids with unknown collision shape lie in the walking corridor: '
                   '%s' % unknown[:6])
    else:
        out['confidence'] = 'estimate'
    return out


def corridor_table(solids, body_pos, half_width=1000.0, half_height=160.0):
    """Solids around the spider, sorted by x, for the -GroundScan diagnostic."""
    gx, gy = body_pos
    rows = []
    for s in solids:
        b = s.get('box') or (s['x'], s['y'], s['x'], s['y'])
        if b[2] < gx - half_width or b[0] > gx + half_width or b[3] < gy - half_height or b[1] > gy + half_height:
            continue
        sp = span_at(s['poly'], gx) if s.get('poly') else None
        rows.append({'id': s['id'], 'obj': s['obj'], 'exact': s.get('exact'), 'shape': s.get('shape'),
                     'sprite': s.get('sprite'), 'x': round(s['x'], 1), 'y': round(s['y'], 1),
                     'xscale': round(s['xscale'], 3), 'yscale': round(s['yscale'], 3), 'angle': round(s['angle'], 2),
                     'box': [round(v, 1) for v in b], 'surface_at_player_x': None if not sp else round(sp[0], 1)})
    return sorted(rows, key=lambda r: r['box'][0])


def compare_with_capture(solids, frames):
    """Check the scan model against where the spider REALLY stood: for every captured frame on ground, the
    recorded ground instance's modelled surface at the player's x vs the recorded foot (body bottom).
    Per instance: missing from the scan (unknown object class), constant offset (sprite != fixture or wrong
    origin / yscale), or error that changes along x (rotation mishandled)."""
    by_id = {s['id']: s for s in solids}
    per = {}
    for r in frames:
        p = r.get('player') or {}
        b, v = p.get('body'), p.get('vars') or {}
        gid = v.get('ground')
        if not b or gid in (None, -4) or isinstance(gid, str):
            continue
        x, y = b['pos_px']
        e = per.setdefault(gid, {'id': gid, 'frames': 0, 'x_min': x, 'x_max': x, 'errs': [],
                                 'ground_angle': set()})
        e['frames'] += 1
        e['x_min'], e['x_max'] = min(e['x_min'], x), max(e['x_max'], x)
        e['ground_angle'].add(v.get('ground_angle'))
        s = by_id.get(gid)
        if s and s.get('poly'):
            sp = span_at(s['poly'], x)
            e['errs'].append((x, None if not sp else sp[0] - (y + PLAYER_R)))
    out = []
    for gid, e in sorted(per.items(), key=lambda kv: kv[1]['x_min']):
        s = by_id.get(gid)
        errs = [(x, er) for x, er in e['errs'] if er is not None]
        row = {'id': gid, 'frames': e['frames'], 'x_range': [round(e['x_min'], 1), round(e['x_max'], 1)],
               'ground_angle': sorted(a for a in e['ground_angle'] if a is not None)[:4],
               'obj': s['obj'] if s else None, 'exact': s.get('exact') if s else None}
        if not s:
            row['verdict'] = 'NOT IN SCAN: object class not modelled as solid (or instance gone)'
        elif not s.get('poly'):
            row['verdict'] = 'no sprite: geometry unknown'
        elif not errs:
            row['verdict'] = 'modelled surface does not span the walked x range (fixture larger than sprite?)'
        else:
            vals = [er for _, er in errs]
            mean = sum(vals) / len(vals)
            spread = max(vals) - min(vals)
            row.update(mean_err_px=round(mean, 2), err_spread_px=round(spread, 2))
            if abs(mean) <= 3 and spread <= 3:
                row['verdict'] = 'OK'
            elif spread <= 3:
                row['verdict'] = 'constant offset %.1f px: fixture differs from sprite rectangle / origin / yscale' % mean
            else:
                row['verdict'] = 'error varies along x by %.1f px: rotation or shape mismatch' % spread
        out.append(row)
    return out


def live_solids(game, probe, obj_names, objs=None, sprites=None):
    """Read-only: every active solid instance (one instance walk + one core read per solid)."""
    import struct
    if objs is None:
        objs, sprites = load_inventory()
    solid_names = solid_object_names(objs)
    out = []
    for inst, c in game.walk():
        name = obj_names[c['obj']] if c['obj'] < len(obj_names) else None
        if name not in solid_names:
            continue
        blob = game.m.raw(inst + probe.CORE_AT, probe.CORE_LEN)
        if not blob:
            continue
        xs, ys = struct.unpack_from('<ff', blob, 0xA4 - probe.CORE_AT)
        if not (math.isfinite(xs) and math.isfinite(ys)):
            continue
        o = objs[name]
        out.append(make_solid(c['id'], name, c['x'], c['y'], xs, ys, c['image_angle'],
                              sprites.get(o.get('sprite')), o.get('shape')))
    return out
