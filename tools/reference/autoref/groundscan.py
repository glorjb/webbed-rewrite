"""Read-only estimate of walkable ground around the spider, BEFORE any input is sent (M2 setup validation).

Live finding (2026-10-11): M2_walk_flat failed with "left solid ground". The scenario walks right for 4 s at up
to max_walkspd 3.5 px/step: about 840 px plus the stopping distance, far more than the "15 spider-lengths" the
procedure asked for. So the space requirement is now computed from the scenario, and checked here first.

Estimate (not exact physics):
  * solids = active instances of objects descending from oSolid / oPlatform with physics enabled
    (docs/inventory/objects.json), each approximated by its sprite rectangle (docs/inventory/sprites.json:
    w, h, origin) scaled by image_xscale / image_yscale and rotated by image_angle, as an axis-aligned box.
  * walk the foot point (player centre x, body bottom y) in 4 px steps: supported while some box spans x with
    its top within +-STEP_UP px of the current foot level; a box spanning the leading edge of the body above
    the foot level is a wall (the spider would climb it: also the end of flat walking).
Assumptions, reported with every scan: physics fixture = sprite rectangle (fixture vertices are not in the
inventory); image_yscale at CInstance+0xA8 (between the verified image_xscale +0xA4 and image_angle +0xAC);
sloped or rotated pieces are boxed. The scan is 'unavailable' when the spider's own ground instance is not
among the solids, and a scenario is only skipped on a confident scan.
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
INV = os.path.join(HERE, '..', '..', '..', 'docs', 'inventory')
STEP = 4.0
STEP_UP = 8.0
PLAYER_R = 11.0
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
    out = set()
    for name, o in objs.items():
        seen, cur = set(), name
        while cur and cur not in seen:
            if cur in SOLID_ROOTS:
                if o.get('physics') and o.get('sprite'):
                    out.add(name)
                break
            seen.add(cur)
            cur = (objs.get(cur) or {}).get('parent')
    return out


def instance_box(x, y, xscale, yscale, angle_deg, sprite):
    """Axis-aligned box of the sprite rectangle placed like GameMaker draws it (origin, scale, rotation)."""
    ox, oy, w, h = sprite['ox'], sprite['oy'], sprite['w'], sprite['h']
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    xs, ys = [], []
    for cx, cy in ((-ox, -oy), (w - ox, -oy), (w - ox, h - oy), (-ox, h - oy)):
        px, py = cx * xscale, cy * yscale
        xs.append(x + px * ca + py * sa)            # GameMaker: positive angle is counter-clockwise on screen
        ys.append(y - px * sa + py * ca)
    return min(xs), min(ys), max(xs), max(ys)


def scan_direction(solids, x0, foot_y, direction, limit=1500.0):
    """Clear distance from x0 in `direction` (+1 right / -1 left) before an edge or a wall."""
    x, foot, d = x0, foot_y, 0.0
    while d < limit:
        x += direction * STEP
        d += STEP
        lead = x + direction * PLAYER_R
        for s in solids:
            l, t, r, b = s['box']
            if l <= lead <= r and t < foot - STEP_UP and b > foot - 2 * PLAYER_R:
                return {'clear_px': round(d - STEP, 1), 'end': 'wall', 'at_x': round(x, 1), 'by': s.get('obj')}
        sup = [s for s in solids if s['box'][0] - 1 <= x <= s['box'][2] + 1 and abs(s['box'][1] - foot) <= STEP_UP]
        if not sup:
            return {'clear_px': round(d - STEP, 1), 'end': 'edge', 'at_x': round(x, 1)}
        foot = min(sup, key=lambda s: abs(s['box'][1] - foot))['box'][1]
    return {'clear_px': round(limit, 1), 'end': 'limit', 'at_x': round(x, 1)}


def ground_profile(solids, body_pos, ground_id):
    """{'confidence': 'estimate' | 'unavailable', 'right': {...}, 'left': {...}, ...}."""
    gx, gy = body_pos
    foot = gy + PLAYER_R
    own = [s for s in solids if s.get('id') == ground_id]
    out = {'solids': len(solids), 'ground_id': ground_id, 'player': [round(gx, 1), round(gy, 1)],
           'assumptions': 'fixture = sprite rectangle; image_yscale at +0xA8 (unverified); rotated pieces boxed'}
    if not own:
        out.update(confidence='unavailable', reason='the spider\'s ground instance (%s) is not among the scanned '
                   'solids (not an oSolid/oPlatform physics object, or not standing on solid ground)' % ground_id)
        return out
    g = own[0]
    if abs(g['box'][1] - foot) > 2 * STEP_UP:
        out.update(confidence='unavailable', reason='the ground instance top (%.1f) is not at the spider\'s feet '
                   '(%.1f): its fixture is not its sprite rectangle' % (g['box'][1], foot))
        return out
    if g.get('angle', 0) % 90:
        out['note'] = 'standing on a rotated piece (%.1f deg): box approximation is coarse' % g['angle']
    out.update(confidence='estimate', ground_obj=g.get('obj'),
               right=scan_direction(solids, gx, g['box'][1], +1), left=scan_direction(solids, gx, g['box'][1], -1))
    return out


def live_solids(game, probe, obj_names, objs=None, sprites=None):
    """Read-only: every active solid instance's box (one instance walk + one core read per solid)."""
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
        spr = sprites.get(objs[name]['sprite'])
        if not spr or not (math.isfinite(xs) and math.isfinite(ys)):
            continue
        out.append({'id': c['id'], 'obj': name, 'angle': c['image_angle'],
                    'box': instance_box(c['x'], c['y'], xs, ys, c['image_angle'], spr)})
    return out
