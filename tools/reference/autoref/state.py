"""Classification of probe rows (webbed_probe.py v3 schema) for the automated runner.

Everything here reads the game's OWN variables as captured by the read-only probe. Predicates marked
INFERRED encode an interpretation of a variable that has not been confirmed against the binary yet;
the runner logs raw state transitions next to them so a wrong interpretation is visible in the data.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'tests'))
import capture_lib as cap   # noqa: E402

NOONE = -4                  # GameMaker `noone`
MOVE_MAX_PXPS = 1.0         # "settled": |v| below this ...
SETTLE_FRAMES = 30          # ... for this many consecutive frames
TELEPORT_PX = 64.0          # player moved further than this in one frame: respawn / room transition
OBJ_WEB = 107               # oWeb (webbed_probe.OBJ)
OBJ_WEBSHOT = 106           # oWebshotNode

# discrete player variables whose changes are logged as state transitions (W* scenarios)
TRANSITION_VARS = ('ground', 'webwalk', 'attached', 'grapple', 'weave', 'retract', 'webshot_node',
                   'jump_allowed', 'landed', 'freefall', 'slipping', 'foot_mode', 'locked', 'pull_target',
                   'weave_base', 'platform_target')
TRANSITION_IO = ('hm', 'vm', 'jmp', 'jmp_held', 'grapple', 'grapple_pressed', 'grapple_released', 'weave',
                 'weave_pressed', 'weave_released', 'retract')


def player(row):
    return row.get('player') or {}


def pvars(row):
    return player(row).get('vars') or {}


def io(row):
    return row.get('io') or {}


def body(row):
    return player(row).get('body')


def is_set(v):
    """GameMaker instance reference that points at something (not noone / undefined)."""
    return v not in (NOONE, None) and not isinstance(v, str)


def support(row):
    return cap.support(row)


def speed_pxps(row):
    b = body(row)
    return math.hypot(*b['vel_pxps']) if b else float('nan')


def live_shot(row):
    """A web-shot projectile exists (captured shot body, or the player's webshot_node is set)."""
    return bool(row.get('shots')) or is_set(pvars(row).get('webshot_node'))


def new_webs(row):
    """Number of oWeb instances created this frame (probe tail tracking)."""
    return sum(1 for e in row.get('new_instances') or [] if e.get('obj') == OBJ_WEB)


def grapple_engaged(row):
    """INFERRED: the player hangs on a grapple when no shot is in flight and its `attached` or `grapple` state
    is set. Independent of the input, so it also tells whether releasing the input detached the player.
    Confirmed only once live W1 captures show the transition (the runner logs the raw variables)."""
    v = pvars(row)
    return not live_shot(row) and (is_set(v.get('attached')) or bool(v.get('grapple')))


def grapple_attached(row):
    """INFERRED: grapple input held and the player engaged on the grapple."""
    return bool(io(row).get('grapple')) and grapple_engaged(row)


def anchor(row):
    """Grapple anchor (gx, gy): the shot position, which stops where the shot attached. INFERRED."""
    v = pvars(row)
    if v.get('gx') is None or v.get('gy') is None:
        return None
    return v['gx'], v['gy']


def readiness(row, need_support='ground'):
    """Reasons the spider is NOT controllable/ready for a scenario start (empty list = ready).
    `need_support`: 'ground', 'web', 'any' (supported by something) or None (no requirement)."""
    r = []
    if row is None:
        return ['no frame captured']
    w = row.get('world') or {}
    if w.get('speed') is None:
        r.append('no physics world (menu / loading?)')
    p = player(row)
    if not p:
        return r + ['no oPlayer (title screen / menu / loading?): enter a level']
    if not p.get('body'):
        return r + ['oPlayer found but its Box2D body is not readable']
    if not row.get('io'):
        r.append('oIO (game input object) not found')
    v = pvars(row)
    if v.get('locked'):
        r.append('player is locked (cutscene / dialogue?)')
    if v.get('in_water'):
        r.append('player is in water')
    if v.get('riding_skateboard'):
        r.append('player is riding a skateboard')
    if v.get('parachute'):
        r.append('parachute is open')
    if live_shot(row) or v.get('grapple') or v.get('weave') or v.get('retract') or is_set(v.get('attached')):
        r.append('a web shot / grapple / weave is active')
    s = support(row)
    if need_support == 'any' and s is None:
        r.append('spider is not standing on anything')
    elif need_support in ('ground', 'web') and s != need_support:
        r.append('support=%s, scenario needs support=%s' % (s, need_support))
    return r


class SettleTracker:
    """Counts consecutive frames with the player present and |v| < MOVE_MAX_PXPS."""
    def __init__(self):
        self.frames = 0

    def update(self, row):
        sp = speed_pxps(row) if row and body(row) else float('nan')
        self.frames = self.frames + 1 if sp == sp and sp < MOVE_MAX_PXPS else 0
        return self.frames

    @property
    def settled(self):
        return self.frames >= SETTLE_FRAMES


def consistency_issues(prev, row):
    """Frame-to-frame problems that make a recording invalid or suspicious (beyond torn/skipped rows):
    counter not increasing, room change, player respawn (id change / teleport), world settings change."""
    out = []
    if prev is None or row is None:
        return out
    if row['frame'] <= prev['frame']:
        out.append('frame counter went from %d to %d' % (prev['frame'], row['frame']))
    if row.get('room') != prev.get('room'):
        out.append('room changed %s -> %s' % (prev.get('room'), row.get('room')))
    pp, pr = player(prev), player(row)
    if pp and pr and pp.get('id') != pr.get('id'):
        out.append('player instance changed (death / respawn?) %s -> %s' % (pp.get('id'), pr.get('id')))
    if bool(pp.get('body')) != bool(pr.get('body')):
        out.append('player body %s' % ('appeared' if pr.get('body') else 'disappeared'))
    bp, br = body(prev), body(row)
    if bp and br and row['frame'] == prev['frame'] + 1:
        d = math.hypot(br['pos_px'][0] - bp['pos_px'][0], br['pos_px'][1] - bp['pos_px'][1])
        if d > TELEPORT_PX:
            out.append('player moved %.0f px in one frame (respawn / teleport?)' % d)
    wp, wr = prev.get('world') or {}, row.get('world') or {}
    if wp.get('speed') is not None and wr.get('speed') is not None and wp != wr:
        out.append('physics world changed %s -> %s' % (wp, wr))
    return out


def transitions(prev, row):
    """Changes of discrete player / input variables between two frames: [(scope, name, old, new)]."""
    out = []
    if prev is None:
        return out
    for scope, names, get in (('player', TRANSITION_VARS, pvars), ('io', TRANSITION_IO, io)):
        a, b = get(prev), get(row)
        for n in names:
            if a.get(n) != b.get(n):
                out.append((scope, n, a.get(n), b.get(n)))
    return out
