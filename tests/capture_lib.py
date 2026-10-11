"""Discover, validate and load reference captures recorded by tools/reference/webbed_probe.py (v3).

Captures are measurements (numbers only) of the original game. They live OUTSIDE the repository:
default C:\\Users\\profe\\DesktopPlaygroundLocal\\captures, override with env DP_CAPTURES.
Each session directory is '<SCENARIO-ID>_<name>_<timestamp>', e.g. M3_jump_in_place_20261011-022345,
containing segment_NN.jsonl (one fast-lane row per game frame) and segment_NN_webs.jsonl (web records).

Quality gate: a session is usable only if it has player physics, valid rows, >= MIN_COVERAGE of the game
frames in its span, and <= MAX_TORN torn rows. Low-quality captures make the tests FAIL, not skip.
"""
import glob
import json
import math
import os

CAPTURE_ROOT = os.environ.get('DP_CAPTURES', r'C:\Users\profe\DesktopPlaygroundLocal\captures')
MIN_COVERAGE = 0.90
MAX_TORN = 0.05

ROW_KEYS = {'frame', 'skipped', 'torn', 't', 'marker', 'room', 'world', 'player', 'io', 'skateboards'}
WORLD_KEYS = {'speed', 'iterations', 'm_per_px'}
PLAYER_KEYS = {'id', 'xy', 'image_angle', 'body', 'vars'}
BODY_KEYS = {'pos_m', 'vel_mps', 'angle_rad', 'angvel_rps', 'pos_px', 'vel_pxps'}
WEB_KEYS = {'lane', 'id', 'vars', 'o_pose', 'a_pose', 'frame', 'frame_end', 'torn'}


def schema_errors(row):
    err = []
    missing = ROW_KEYS - set(row)
    if missing:
        return ['row missing %s' % sorted(missing)]
    if not isinstance(row['frame'], int):
        err.append('frame is not an int')
    if not isinstance(row['world'], dict) or WORLD_KEYS - set(row['world']):
        err.append('world block incomplete')
    p = row['player']
    if p is not None:
        if PLAYER_KEYS - set(p):
            err.append('player missing %s' % sorted(PLAYER_KEYS - set(p)))
        elif p['body'] is not None and BODY_KEYS - set(p['body']):
            err.append('player.body missing %s' % sorted(BODY_KEYS - set(p['body'])))
    return err


class Session:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.frames, self.webs, self.problems = [], [], []
        self.bad_rows = 0
        seg_files = sorted(f for f in glob.glob(os.path.join(path, 'segment_*.jsonl')) if not f.endswith('_webs.jsonl'))
        if not seg_files:
            self.problems.append('no segment_*.jsonl files')
        self.segments = []
        for seg in seg_files:
            rows = []
            if os.path.getsize(seg) == 0:
                self.problems.append('%s is empty (0 bytes)' % os.path.basename(seg))
            with open(seg) as f:
                for i, line in enumerate(f):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        self.bad_rows += 1
                        continue
                    errs = schema_errors(row)
                    if errs:
                        self.bad_rows += 1
                        if len(self.problems) < 5:
                            self.problems.append('%s line %d: %s' % (os.path.basename(seg), i + 1, '; '.join(errs)))
                        continue
                    rows.append(row)
            self.segments.append(rows)
            self.frames.extend(rows)
            wf = seg[:-len('.jsonl')] + '_webs.jsonl'
            if os.path.exists(wf):
                with open(wf) as f:
                    for line in f:
                        if line.strip():
                            r = json.loads(line)
                            if not (WEB_KEYS - set(r)):
                                self.webs.append(r)
        # quality metrics (per segment, then summed)
        self.player_frames = sum(1 for r in self.frames if r['player'] and r['player'].get('body'))
        self.torn = sum(r['torn'] for r in self.frames)
        self.span = sum(s[-1]['frame'] - s[0]['frame'] + 1 for s in self.segments if s)
        self.duration = sum(s[-1]['t'] - s[0]['t'] for s in self.segments if len(s) > 1)
        self.skipped = sum(r['skipped'] for s in self.segments for r in s[1:])
        self.coverage = len(self.frames) / self.span if self.span else 0.0
        self.torn_frac = self.torn / len(self.frames) if self.frames else 0.0
        self.longest_run = 0
        for s in self.segments:
            run = 1
            for a, b in zip(s, s[1:]):
                run = run + 1 if b['frame'] == a['frame'] + 1 and not a['torn'] and not b['torn'] else 1
                self.longest_run = max(self.longest_run, run)
        if self.frames and self.player_frames == 0:
            self.problems.append('%d rows but none contain player physics (recorded outside a level?)' % len(self.frames))
        if self.frames and self.coverage < MIN_COVERAGE:
            self.problems.append('only %.1f%% of game frames captured (need >= %.0f%%)' % (100 * self.coverage, 100 * MIN_COVERAGE))
        if self.frames and self.torn_frac > MAX_TORN:
            self.problems.append('%.1f%% torn rows (max %.0f%%)' % (100 * self.torn_frac, 100 * MAX_TORN))
        if seg_files and not self.frames and not any('empty' in p for p in self.problems):
            self.problems.append('no valid rows')

    @property
    def usable(self):
        return not self.problems and self.player_frames > 0 and self.bad_rows == 0

    def summary(self):
        hz = (len(self.frames) - len(self.segments)) / self.duration if self.duration else 0.0
        return ('%-42s %5.1fs rows=%-5d player=%-5d %5.1fHz coverage=%5.1f%% skipped=%-4d(%4.1f%%) torn=%-4d(%4.1f%%) '
                'run=%-5d webrec=%-6d %s') % (
            self.name, self.duration, len(self.frames), self.player_frames, hz, 100 * self.coverage, self.skipped,
            100.0 * self.skipped / self.span if self.span else 0, self.torn, 100 * self.torn_frac, self.longest_run,
            len(self.webs), 'OK' if self.usable else 'UNUSABLE: ' + ' | '.join(self.problems))


def discover(prefix=''):
    if not os.path.isdir(CAPTURE_ROOT):
        return []
    return [Session(d) for d in sorted(glob.glob(os.path.join(CAPTURE_ROOT, prefix + '*'))) if os.path.isdir(d)]


def consecutive(frames):
    """Pairs of adjacent, untorn frames with no frame missing in between."""
    for a, b in zip(frames, frames[1:]):
        if b['frame'] == a['frame'] + 1 and not a['torn'] and not b['torn']:
            yield a, b


# ------------------------------------------------------------------ state classification (selection only)
def _v(fr):
    return (fr.get('player') or {}).get('vars') or {}


def support(fr):
    """'ground', 'web' or None, from the game's own variables (ground / webwalk instance ids)."""
    v = _v(fr)
    if v.get('ground') not in (-4, None):
        return 'ground'
    if v.get('webwalk') not in (-4, None):
        return 'web'
    return None


def web_segments(webs):
    """Latest endpoints per web id: {id: (ox, oy, ax, ay)}."""
    seg = {}
    for r in sorted(webs, key=lambda r: r['frame']):
        v = r['vars']
        if None not in (v.get('ox'), v.get('oy'), v.get('ax'), v.get('ay')):
            seg[r['id']] = (v['ox'], v['oy'], v['ax'], v['ay'])
    return seg


def dist_point_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def free_flight(fr, segments, clearance=20.0):
    """Player body moving under gravity only: no support, not attached, no web/grapple/weave, not in water or
    on a skateboard/parachute, and not within `clearance` px of any known web line."""
    p = fr.get('player') or {}
    v = p.get('vars') or {}
    if not p.get('body') or support(fr):
        return False
    if v.get('attached') not in (-4, None) or v.get('grapple') or v.get('weave') or v.get('retract'):
        return False
    if v.get('in_water') or v.get('riding_skateboard') or v.get('parachute') or v.get('locked'):
        return False
    x, y = p['body']['pos_px']
    return all(dist_point_segment(x, y, *s) > clearance for s in segments.values())


def jump_pressed(fr):
    io = fr.get('io') or {}
    return bool(io.get('jmp'))
