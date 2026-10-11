"""READ-ONLY reference recorder for the ORIGINAL Webbed (Steam build 7679547, 32-bit GameMaker YYC).

Guarantees
  * Attaches only if webbed.exe's SHA-256 matches the analysed build.
  * Opens the process with PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION only:
    no debugger, no breakpoints, no thread suspension, no memory writes, no injected code.
    The game is never paused or slowed: the probe only reads while the game keeps running.
  * Reads only physics / web / input-state values listed below.

Frame sync and consistency
  The runner increments a frame counter (global, rebased for ASLR) once per game step, after End Step and
  before Draw. The probe polls it (~0.5 ms) and, right after each increment, captures the FAST LANE:
    counter c0 -> player + input + world + focus webs + skateboards -> counter c1
  `torn` = 1 when c1 != c0, i.e. the next frame's update started while the row was being read, so the row may
  mix two frames. With the fast lane typically < 1 ms of reads inside a ~16.7 ms frame, torn rows should be rare.
  `skipped` = game frames between this row and the previous one that have no row (should be 0).

  The SLOW LANE (all other webs and their endpoint nodes) runs in the idle time between frames on a time
  budget, in small chunks, and stops as soon as a new frame starts, so it can never cost fast-lane frames.
  Each slow record carries its own counter stamps and `torn` flag.

Files per segment N
  segment_NN.jsonl       one fast-lane row per game frame
  segment_NN_webs.jsonl  web records (lane = "focus" every frame, "sweep" from the slow lane)

Hotkeys (global key state is polled; keys still reach the game)
  F9  start / stop recording      F10  add a step marker      Ctrl+F12  quit

Usage
  python webbed_probe.py --out <session_dir> [--selftest] [--record-seconds N]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import hashlib
import json
import math
import os
import struct
import sys
import time

EXPECTED_SHA256 = 'c63cecb63ed471bc68b2e5a0f90c4a0cf15a57266b7af1e2d80a0b04d0116dde'
BUILD = 'steam-7679547'

# ---- runtime layout (recovered from the runner's own code; see docs/P1_REVERSE_ENGINEERING.md)
# webbed.exe is DYNAMICBASE (ASLR): globals are for the preferred base 0x400000 and are rebased at attach.
PREFERRED_BASE = 0x00400000
G_RUN_ROOM = 0x01679C68       # CRoom*
G_ROOM_INDEX = 0x01679C90     # current room index
G_FRAME_COUNTER = 0x01679CB8  # ++ once per DoAStep, at the END of the update (after End Step)
G_FRAME_START = 0x01679BF8    # int64 timestamp written at the START of DoAStep (before Begin Step)
STAMP_LEN = G_FRAME_COUNTER + 4 - G_FRAME_START   # one read covers both
ROOM_FIRST_ACTIVE = 0x80      # CRoom active list: +0x80 first, +0x84 last, +0x88 count (verified live)
ROOM_LAST_ACTIVE = 0x84
INST_PREV = 0x14C             # CInstance -> previous active (verified live)
ROOM_PHYS_WORLD = 0xB4
WORLD_SPEED = 0x58            # int speed, int iterations, float m/px at +0x58..+0x63
# CInstance fields, read as one block starting at +0x70
CORE_AT, CORE_LEN = 0x70, 0xDC  # covers +0x70 .. +0x14B (phys, flags, id, object_index, image_angle, x, y, next)
C_PHYS, C_FLAGS, C_ID, C_OBJ = 0x74 - CORE_AT, 0x80 - CORE_AT, 0x84 - CORE_AT, 0x88 - CORE_AT
C_ANGLE, C_X, C_Y, C_NEXT = 0xAC - CORE_AT, 0xB8 - CORE_AT, 0xBC - CORE_AT, 0x148 - CORE_AT
INST_VARMAP = 0x2C            # CHashMap<int, RValue*>: [0]=size ... [4]=elements(12 B: value*, key, hash)
BODY_LEN = 0x5C               # b2Body: pos +0x0C, angle +0x48, linvel +0x50, angvel +0x58

OBJ = {'oPlayer': 94, 'oIO': 68, 'oWeb': 107, 'oWebNode': 101, 'oWebNode_phantom': 105,
       'oWebshotNode': 106, 'oSkateboard': 311, 'oGiantSkateboard': 23}
NODE_OBJS = (OBJ['oWebNode'], OBJ['oWebNode_phantom'], OBJ['oWebshotNode'])
SKATE_OBJS = (OBJ['oSkateboard'], OBJ['oGiantSkateboard'])
# fast lane: state needed to classify every frame (support, webs, water, skateboard, jump)
PLAYER_VARS = ['ground', 'webwalk', 'attached', 'grapple', 'weave', 'retract', 'in_water', 'riding_skateboard',
               'jump_allowed', 'jump_buffered', 'landed', 'freefall', 'slipping', 'ground_angle', 'foot_mode',
               'locked', 'parachute', 'webshot_node', 'pull_target', 'tilt',
               # web shot / grapple state (oPlayer_Step_0 L183-L260, L624-L729, L912-L916)
               'gx', 'gy', 'gh', 'gv', 'sx', 'sy', 'ox', 'oy', 'odir', 'odist', 'adir', 'adist', 'slack',
               'webshot_timer', 'weave_base', 'crosshair_tx', 'crosshair_ty', 'platform_target']
IO_VARS = ['hm', 'vm', 'mvdir', 'jmp', 'jmp_held', 'grapple', 'grapple_pressed', 'grapple_released', 'weave',
           'weave_pressed', 'weave_released', 'retract', 'mouse_1', 'mouse_1_pressed', 'mouse_1_released',
           'interact', 'interact_held', 'aimx', 'aimy', 'mvmag']
WEB_VARS = ['o', 'a', 'ox', 'oy', 'ax', 'ay', 'odist', 'odir', 'adist', 'adir', 'dir', 'length', 'current_length',
            'last_length', 'length_buffer_1', 'length_buffer_2', 'length_buffer_3', 'slack', 'slack_v',
            'avg_slack_v', 'stretch', 'pull_power', 'breakable', 'anchored', 'snap_threshold',
            'pluck_string', 'playing_note']
NODE_VARS = ['attached_webs', 'buoyancy']
RECENT_FRAMES = 300           # newly created webs/nodes are captured every frame for 5 s
FOCUS_RADIUS_PX = 300         # webs with an endpoint this close to the player are captured every frame
FOCUS_MAX = 8
SWEEP_RADIUS_PX = 1500        # slow lane records webs with an endpoint within this distance
SLOW_BUDGET_S = 0.006         # max slow-lane work per frame

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
u32 = ctypes.WinDLL('user32')
k32.OpenProcess.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
VK_F9, VK_F10, VK_F12, VK_CONTROL = 0x78, 0x79, 0x7B, 0x11


# ------------------------------------------------------------------------------------------ process
def find_pid(name='webbed.exe'):
    class PE32(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('cntUsage', wt.DWORD), ('th32ProcessID', wt.DWORD),
                    ('th32DefaultHeapID', ctypes.c_void_p), ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
                    ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wt.DWORD),
                    ('szExeFile', wt.WCHAR * 260)]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    e = PE32()
    e.dwSize = ctypes.sizeof(e)
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    pid = None
    while ok:
        if e.szExeFile.lower() == name:
            pid = e.th32ProcessID
            break
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return pid


def module_base(pid, name='webbed.exe'):
    """Load address of the game's main module (read-only Toolhelp module snapshot)."""
    class ME32(ctypes.Structure):
        _fields_ = [('dwSize', wt.DWORD), ('th32ModuleID', wt.DWORD), ('th32ProcessID', wt.DWORD),
                    ('GlblcntUsage', wt.DWORD), ('ProccntUsage', wt.DWORD), ('modBaseAddr', ctypes.c_void_p),
                    ('modBaseSize', wt.DWORD), ('hModule', wt.HMODULE), ('szModule', wt.WCHAR * 256),
                    ('szExePath', wt.WCHAR * 260)]
    for _ in range(5):
        snap = k32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)   # SNAPMODULE | SNAPMODULE32
        if snap and snap != wt.HANDLE(-1).value:
            break
        time.sleep(0.05)
    else:
        return None
    e = ME32()
    e.dwSize = ctypes.sizeof(e)
    ok = k32.Module32FirstW(snap, ctypes.byref(e))
    base = None
    while ok:
        if e.szModule.lower() == name:
            base = e.modBaseAddr
            break
        ok = k32.Module32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return base


def exe_path(h):
    buf = ctypes.create_unicode_buffer(1024)
    n = wt.DWORD(1024)
    k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
    return buf.value


class Mem:
    """ReadProcessMemory only. Reuses buffers to keep per-read overhead low."""
    def __init__(self, h):
        self.h = h
        self._got = ctypes.c_size_t()
        self._bufs = {}
        self.reads = 0

    def raw(self, a, n):
        if not a:
            return None
        b = self._bufs.get(n)
        if b is None:
            b = self._bufs[n] = ctypes.create_string_buffer(n)
        self.reads += 1
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(a), b, n, ctypes.byref(self._got)) or self._got.value != n:
            return None
        return b.raw

    def u32(self, a):
        b = self.raw(a, 4)
        return struct.unpack('<I', b)[0] if b else None

    def i32(self, a):
        b = self.raw(a, 4)
        return struct.unpack('<i', b)[0] if b else None

    def rvalue(self, a):
        b = self.raw(a, 16)
        if not b:
            return None
        kind = struct.unpack_from('<I', b, 12)[0] & 0xFFFFFF
        if kind == 0:
            return struct.unpack_from('<d', b, 0)[0]
        if kind == 13:
            return bool(struct.unpack_from('<d', b, 0)[0])
        if kind == 7:
            return struct.unpack_from('<i', b, 0)[0]
        if kind == 10:
            return struct.unpack_from('<q', b, 0)[0]
        if kind == 5:
            return None
        return '<kind%d>' % kind


# ------------------------------------------------------------------------------------------ game reads
class Game:
    def __init__(self, mem, var_ids, base):
        self.m = mem
        self.var_ids = var_ids
        d = base - PREFERRED_BASE
        self.g_room, self.g_room_index, self.g_frame = G_RUN_ROOM + d, G_ROOM_INDEX + d, G_FRAME_COUNTER + d
        self.g_start = G_FRAME_START + d

    def frame_counter(self):
        return self.m.u32(self.g_frame)

    def stamp(self):
        """(frame_start_timestamp, frame_counter) in one read. Frame n's state is stable from the counter
        reaching n until the start timestamp changes (next update begins)."""
        b = self.m.raw(self.g_start, STAMP_LEN)
        if not b:
            return None
        return struct.unpack_from('<q', b, 0)[0], struct.unpack_from('<I', b, STAMP_LEN - 4)[0]

    def room_ptr(self):
        return self.m.u32(self.g_room)

    def core(self, inst):
        """flags, id, object_index, phys, image_angle, x, y, next  (one read)."""
        b = self.m.raw(inst + CORE_AT, CORE_LEN)
        if not b:
            return None
        return {'phys': struct.unpack_from('<I', b, C_PHYS)[0], 'flags': struct.unpack_from('<I', b, C_FLAGS)[0],
                'id': struct.unpack_from('<i', b, C_ID)[0], 'obj': struct.unpack_from('<I', b, C_OBJ)[0],
                'image_angle': struct.unpack_from('<f', b, C_ANGLE)[0], 'x': struct.unpack_from('<f', b, C_X)[0],
                'y': struct.unpack_from('<f', b, C_Y)[0], 'next': struct.unpack_from('<I', b, C_NEXT)[0]}

    def world(self):
        room = self.room_ptr()
        w = self.m.u32(room + ROOM_PHYS_WORLD) if room else None
        b = self.m.raw(w + WORLD_SPEED, 12) if w else None
        if not b:
            return {'speed': None, 'iterations': None, 'm_per_px': None}
        s, it, sc = struct.unpack('<iif', b)
        return {'speed': s, 'iterations': it, 'm_per_px': sc}

    def body(self, phys, scale):
        bptr = self.m.u32(phys) if phys else None
        b = self.m.raw(bptr, BODY_LEN) if bptr else None
        if not b:
            return None
        px, py = struct.unpack_from('<ff', b, 0x0C)
        vx, vy = struct.unpack_from('<ff', b, 0x50)
        d = {'pos_m': [px, py], 'vel_mps': [vx, vy], 'angle_rad': struct.unpack_from('<f', b, 0x48)[0],
             'angvel_rps': struct.unpack_from('<f', b, 0x58)[0]}
        if scale:
            d['pos_px'] = [px / scale, py / scale]
            d['vel_pxps'] = [vx / scale, vy / scale]
        return d

    def vars(self, inst, names):
        res = {}
        vmap = self.m.u32(inst + INST_VARMAP)
        hdr = self.m.raw(vmap, 0x14) if vmap else None
        if not hdr:
            return res
        size, _, _, _, elems = struct.unpack('<5I', hdr)
        if not elems or not (0 < size <= 1 << 16):
            return res
        blob = self.m.raw(elems, size * 12)
        if not blob:
            return res
        want = {self.var_ids[n]: n for n in names if n in self.var_ids}
        for vptr, key, hsh in struct.iter_unpack('<IiI', blob):
            if hsh and key in want and vptr:
                res[want[key]] = self.m.rvalue(vptr)
        return res

    def walk(self):
        """Generator over active instances: (ptr, core). One read per instance."""
        room = self.room_ptr()
        inst = self.m.u32(room + ROOM_FIRST_ACTIVE) if room else None
        guard = 0
        while inst and guard < 100000:
            c = self.core(inst)
            if c is None:
                return
            if (c['flags'] & 3) == 0:
                yield inst, c
            inst = c['next']
            guard += 1

    def node_pose(self, inst, scale):
        c = self.core(inst)
        if not c:
            return None
        return {'id': c['id'], 'obj': c['obj'], 'xy': [c['x'], c['y']], 'image_angle': c['image_angle'],
                'body': self.body(c['phys'], scale)}


def dist_to_web(px, py, v):
    try:
        return min(math.hypot(v['ox'] - px, v['oy'] - py), math.hypot(v['ax'] - px, v['ay'] - py))
    except (KeyError, TypeError):
        return float('inf')


# ------------------------------------------------------------------------------------------ recorder
class Recorder:
    def __init__(self, game):
        self.g = game
        self.room = None
        self.player = None        # instance ptr
        self.io = None
        self.skates = []
        self.focus = []           # web instance ptrs captured every frame
        self.webs = {}            # id -> ptr (from last full sweep)
        self.nodes = {}           # id -> ptr
        self.sweep = None         # slow-lane generator
        self.scale = None
        self.player_xy = (0.0, 0.0)
        self.tail = None           # last active instance seen
        self.count = None
        self.recent = {}           # ptr -> expiry frame (new webs / nodes / web shots)
        self.new_events = []

    # -- discovery (full walk; also run when cached pointers become invalid)
    def rediscover(self):
        self.player, self.io, self.skates = None, None, []
        webs, nodes = {}, {}
        for p, c in self.g.walk():
            o = c['obj']
            if o == OBJ['oPlayer']:
                self.player = p
            elif o == OBJ['oIO']:
                self.io = p
            elif o in SKATE_OBJS:
                self.skates.append(p)
            elif o == OBJ['oWeb']:
                webs[c['id']] = p
            elif o in NODE_OBJS:
                nodes[c['id']] = p
        self.webs, self.nodes = webs, nodes
        self.room = self.g.room_ptr()
        self.focus = []

    def poll_new_instances(self, frame):
        """New instances are appended at the tail of the room's active list: read (last, count) and walk
        back via prev only when they changed. Typically 1 read per frame."""
        room = self.g.room_ptr()
        b = self.g.m.raw(room + ROOM_LAST_ACTIVE, 8) if room else None
        if not b:
            return
        last, count = struct.unpack('<Ii', b)
        if self.tail is None:
            self.tail, self.count = last, count
            return
        if last == self.tail and count == self.count:
            return
        ptr, steps = last, 0
        while ptr and ptr != self.tail and steps < 64:
            c = self.g.core(ptr)
            if not c:
                break
            o = c['obj']
            if o in (OBJ['oWeb'],) + NODE_OBJS + SKATE_OBJS:
                self.recent[ptr] = frame + RECENT_FRAMES
                if o == OBJ['oWeb']:
                    self.webs[c['id']] = ptr
                elif o in NODE_OBJS:
                    self.nodes[c['id']] = ptr
                else:
                    self.skates.append(ptr)
                self.new_events.append({'frame': frame, 'id': c['id'], 'obj': o})
            ptr = self.g.m.u32(ptr + INST_PREV)
            steps += 1
        self.tail, self.count = last, count

    def valid(self, ptr, obj):
        c = self.g.core(ptr) if ptr else None
        return c if c and c['obj'] == obj and (c['flags'] & 3) == 0 else None

    # -- fast lane
    def fast(self, frame, last_frame, t, marker):
        g = self.g
        if g.room_ptr() != self.room or (self.player and not self.valid(self.player, OBJ['oPlayer'])):
            self.rediscover()
        w = g.world()
        self.scale = w['m_per_px']
        row = {'frame': frame, 'skipped': max(0, frame - last_frame - 1) if last_frame is not None else 0,
               't': round(t, 6), 'marker': marker, 'room': g.m.i32(g.g_room_index), 'world': w,
               'player': None, 'io': None, 'skateboards': []}
        if self.player:
            c = g.core(self.player)
            if c and c['obj'] == OBJ['oPlayer']:
                row['player'] = {'id': c['id'], 'xy': [c['x'], c['y']], 'image_angle': c['image_angle'],
                                 'body': g.body(c['phys'], self.scale), 'vars': g.vars(self.player, PLAYER_VARS)}
                self.player_xy = (c['x'], c['y'])
        if self.io:
            row['io'] = g.vars(self.io, IO_VARS)
        for s in self.skates:
            c = g.core(s)
            if c and c['obj'] in SKATE_OBJS:
                row['skateboards'].append({'id': c['id'], 'obj': c['obj'], 'xy': [c['x'], c['y']],
                                           'image_angle': c['image_angle'], 'body': g.body(c['phys'], self.scale)})
        self.poll_new_instances(frame)
        row['shots'], row['nodes_recent'], row['new_instances'] = [], [], self.new_events
        self.new_events = []
        recent_webs = []
        for ptr, exp in list(self.recent.items()):
            c = g.core(ptr)
            if exp < frame or not c or (c['flags'] & 3) or c['obj'] not in (OBJ['oWeb'],) + NODE_OBJS + SKATE_OBJS:
                del self.recent[ptr]
                continue
            if c['obj'] == OBJ['oWeb']:
                if ptr not in self.focus:
                    recent_webs.append(ptr)
            elif c['obj'] in NODE_OBJS:
                pose = {'id': c['id'], 'obj': c['obj'], 'xy': [c['x'], c['y']], 'image_angle': c['image_angle'],
                        'body': g.body(c['phys'], self.scale)}
                (row['shots'] if c['obj'] == OBJ['oWebshotNode'] else row['nodes_recent']).append(pose)
        focus_recs = [self.web_record(wp, 'recent') for wp in recent_webs]
        # diagnostic: the player has a live web shot that the tail tracking did not capture
        ws = (row['player'] or {}).get('vars', {}).get('webshot_node')
        row['shot_missing'] = int(isinstance(ws, (int, float)) and ws not in (-4, None)
                                  and not any(sh['id'] == int(ws) for sh in row['shots']))
        for wp in list(self.focus):
            if not self.valid(wp, OBJ['oWeb']):
                self.focus.remove(wp)
                continue
            focus_recs.append(self.web_record(wp, 'focus'))
        return row, focus_recs

    def web_record(self, wp, lane):
        g = self.g
        c = g.core(wp)
        v = g.vars(wp, WEB_VARS)
        rec = {'lane': lane, 'id': c['id'] if c else None, 'vars': v, 'o_pose': None, 'a_pose': None}
        for end in ('o', 'a'):
            iid = v.get(end)
            ptr = self.nodes.get(int(iid)) if isinstance(iid, (int, float)) and iid not in (None, -4) else None
            if ptr:
                rec[end + '_pose'] = g.node_pose(ptr, self.scale)
        return rec

    # -- slow lane (generator; yields after small units of work)
    def slow_lane(self):
        while True:
            t_start = time.perf_counter()
            webs, nodes, n = {}, {}, 0
            player, io, skates = None, None, []
            for p, c in self.g.walk():
                if c['obj'] == OBJ['oWeb']:
                    webs[c['id']] = p
                elif c['obj'] in NODE_OBJS:
                    nodes[c['id']] = p
                elif c['obj'] == OBJ['oPlayer']:
                    player = p
                elif c['obj'] == OBJ['oIO']:
                    io = p
                elif c['obj'] in SKATE_OBJS:
                    skates.append(p)
                n += 1
                if n % 32 == 0:
                    yield None
            self.webs, self.nodes = webs, nodes
            # the player can appear without a room change (title screen and hub share room_Hub)
            if player and player != self.player:
                self.player = player
                print('  player found (instance list refreshed)')
            if io:
                self.io = io
            self.skates = skates
            px, py = self.player_xy
            focus = []
            for wid, wp in list(webs.items()):
                v = self.g.vars(wp, ['ox', 'oy', 'ax', 'ay'])
                d = dist_to_web(px, py, v)
                if d <= FOCUS_RADIUS_PX and len(focus) < FOCUS_MAX:
                    focus.append(wp)
                if d <= SWEEP_RADIUS_PX:
                    yield ('record', wp)
                else:
                    yield None
            self.focus = focus
            yield ('sweep_done', len(webs))
            while time.perf_counter() - t_start < 0.25:     # at most ~4 sweeps per second
                yield 'idle'


def diagnose(game, room_names, obj_names):
    rep = []
    c1 = game.frame_counter()
    time.sleep(0.25)
    c2 = game.frame_counter()
    adv = c1 is not None and c2 is not None and c2 != c1
    rep.append('frame counter: %s -> %s over 0.25 s (%s)' % (c1, c2, 'advancing' if adv else 'NOT advancing'))
    ri = game.m.i32(game.g_room_index)
    rep.append('room: %s (%s)' % (ri, room_names[ri] if ri is not None and 0 <= ri < len(room_names) else '?'))
    counts, t0 = {}, time.perf_counter()
    for _, c in game.walk():
        nm = obj_names[c['obj']] if c['obj'] < len(obj_names) else 'obj%d' % c['obj']
        counts[nm] = counts.get(nm, 0) + 1
    walk_ms = (time.perf_counter() - t0) * 1000
    rep.append('active instances: %d (walk %.1f ms); oPlayer: %d; oIO: %d; oWeb: %d; web nodes: %d' % (
        sum(counts.values()), walk_ms, counts.get('oPlayer', 0), counts.get('oIO', 0), counts.get('oWeb', 0),
        sum(counts.get(n, 0) for n in ('oWebNode', 'oWebNode_phantom', 'oWebshotNode'))))
    rec = Recorder(game)
    rec.rediscover()
    t0 = time.perf_counter()
    reads0 = game.m.reads
    row, _ = rec.fast(game.frame_counter(), None, 0.0, 0)
    fast_ms = (time.perf_counter() - t0) * 1000
    rep.append('fast lane: %.2f ms, %d reads per frame' % (fast_ms, game.m.reads - reads0))
    p = row['player']
    body_ok = bool(p and p.get('body'))
    if not p:
        rep.append('WARNING: no oPlayer in this room (title screen / menu / loading?). Enter a level before recording.')
    elif not body_ok:
        rep.append('WARNING: oPlayer found but its Box2D body could not be read.')
    else:
        v = p['vars']
        support = 'ground' if v.get('ground') not in (-4, None) else ('web' if v.get('webwalk') not in (-4, None) else 'none')
        rep.append('player body: pos_px=%s vel_pxps=%s; support=%s' % (
            [round(x, 2) for x in p['body']['pos_px']], [round(x, 2) for x in p['body']['vel_pxps']], support))
        if support == 'web':
            rep.append('NOTE: the spider is standing on a web. Movement scenarios M1-M6 need solid ground.')
    w = row['world']
    rep.append('physics world: speed=%s iterations=%s m_per_px=%s' % (w['speed'], w['iterations'], w['m_per_px']))
    ok = adv and counts.get('oPlayer', 0) == 1 and body_ok and fast_ms < 8
    if body_ok and fast_ms >= 8:
        rep.append('WARNING: fast lane too slow (%.1f ms); frames may be torn.' % fast_ms)
    return {'report': rep, 'counter_advancing': adv, 'ok_for_recording': ok, 'row': row}


def key_down(vk):
    return bool(u32.GetAsyncKeyState(vk) & 0x8000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--selftest', action='store_true', help='diagnose, print one fast-lane row and exit')
    ap.add_argument('--record-seconds', type=float, default=0, help='record one segment immediately for N s')
    ap.add_argument('--no-slow-lane', action='store_true', help='fast lane only (no web sweep)')
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument('--vars', default=os.path.join(here, '..', '..', 'docs', 'inventory', 'yyc_vars.json'))
    ap.add_argument('--names', default=os.path.join(here, '..', '..', 'docs', 'inventory', 'names.json'))
    a = ap.parse_args()
    try:
        inv = json.load(open(a.names))
        room_names, obj_names = inv.get('ROOM', []), inv.get('OBJT', [])
    except OSError:
        room_names, obj_names = [], []

    pid = find_pid()
    if not pid:
        sys.exit('webbed.exe is not running')
    h = k32.OpenProcess(0x0010 | 0x1000, False, pid)        # PROCESS_VM_READ | PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        sys.exit('OpenProcess failed: %d' % ctypes.get_last_error())
    digest = hashlib.sha256(open(exe_path(h), 'rb').read()).hexdigest()
    if digest != EXPECTED_SHA256:
        sys.exit('webbed.exe build mismatch (%s). This probe only supports %s.' % (digest, BUILD))
    base = module_base(pid)
    if not base:
        sys.exit('could not find the webbed.exe module base')
    game = Game(Mem(h), json.load(open(a.vars)), base)
    print('webbed.exe pid %d loaded at 0x%08X (preferred 0x%08X)' % (pid, base, PREFERRED_BASE))
    diag = diagnose(game, room_names, obj_names)
    for line in diag['report']:
        print(line)
    if a.selftest:
        print(json.dumps(diag['row'], indent=1, default=str)[:3000])
        sys.exit(0 if diag['ok_for_recording'] else 2)
    if not diag['counter_advancing']:
        sys.exit('ABORT: the frame counter is not advancing; nothing could be recorded.')

    winmm = ctypes.WinDLL('winmm')
    winmm.timeBeginPeriod(1)
    os.makedirs(a.out, exist_ok=True)
    meta = {'build': BUILD, 'sha256': digest, 'started': datetime.datetime.now().isoformat(),
            'probe': 'webbed_probe.py v3 (read-only polling, fast/slow lanes)', 'segments': [],
            'attach_diagnostics': diag['report']}
    rec = Recorder(game)
    rec.rediscover()
    slow = None if a.no_slow_lane else rec.slow_lane()
    state = {'recording': False, 'seg': 0, 'f': None, 'fw': None, 'marker': 0, 'last_rec_frame': None}
    t0 = time.perf_counter()

    def start_segment():
        state['seg'] += 1
        state['marker'] = 0
        base_name = os.path.join(a.out, 'segment_%02d' % state['seg'])
        state['f'] = open(base_name + '.jsonl', 'w')
        state['fw'] = open(base_name + '_webs.jsonl', 'w')
        state['last_rec_frame'] = None
        meta['segments'].append({'file': os.path.basename(base_name) + '.jsonl', 'webs_file': os.path.basename(base_name) + '_webs.jsonl',
                                 'rows': 0, 'rows_with_player': 0, 'skipped_frames': 0, 'torn_rows': 0,
                                 'web_records': 0, 'web_sweeps': 0, 'first_frame': None, 'last_frame': None,
                                 'fast_ms_max': 0.0, 'started_t': time.perf_counter() - t0})
        state['recording'] = True
        print('\a[REC] segment %d' % state['seg'])
        if not rec.player:
            print('  WARNING: no oPlayer right now (menu/loading?). Rows will have player=null.')

    def stop_segment():
        state['recording'] = False
        for k in ('f', 'fw'):
            if state[k]:
                state[k].close()
                state[k] = None
        sg = meta['segments'][-1]
        sg['duration_s'] = round(time.perf_counter() - t0 - sg['started_t'], 3)
        span = (sg['last_frame'] - sg['first_frame'] + 1) if sg['rows'] else 0
        sg['frame_span'] = span
        sg['coverage'] = round(sg['rows'] / span, 4) if span else 0.0
        sg['effective_hz'] = round(sg['rows'] / sg['duration_s'], 2) if sg['duration_s'] else 0.0
        print('[STOP] segment %d: %.1f s, %d rows (%.1f Hz), %d with player, skipped %d (%.2f%%), torn %d, web records %d' % (
            state['seg'], sg['duration_s'], sg['rows'], sg['effective_hz'], sg['rows_with_player'], sg['skipped_frames'],
            100.0 * sg['skipped_frames'] / span if span else 0, sg['torn_rows'], sg['web_records']))
        print('  web shots: %d frames with a shot, %d frames with a shot NOT captured, %d new instances' % (
            sg.get('shot_frames', 0), sg.get('shot_missing_frames', 0), sg.get('new_instances', 0)))

    auto_stop = None
    if a.record_seconds > 0:
        start_segment()
        auto_stop = time.perf_counter() + a.record_seconds
    prev_keys = {VK_F9: False, VK_F10: False, VK_F12: False}
    st = game.stamp()
    last_counter = st[1] if st else None
    last_status = time.perf_counter()
    try:
        while True:
            if auto_stop and time.perf_counter() >= auto_stop:
                break
            for vk in prev_keys:
                down = key_down(vk)
                if down and not prev_keys[vk]:
                    if vk == VK_F9:
                        stop_segment() if state['recording'] else start_segment()
                    elif vk == VK_F10 and state['recording']:
                        state['marker'] += 1
                        print('  marker %d' % state['marker'])
                    elif vk == VK_F12 and key_down(VK_CONTROL):
                        raise KeyboardInterrupt
                prev_keys[vk] = down
            st0 = game.stamp()
            if st0 is None:
                break
            c0 = st0[1]
            if c0 != last_counter:
                last_counter = c0
                tf = time.perf_counter()
                if state['recording']:
                    row, focus = rec.fast(c0, state['last_rec_frame'], tf - t0, state['marker'])
                    st1 = game.stamp()
                    # torn: the next update started (start timestamp changed) or finished (counter changed)
                    row['torn'] = int(st1 != st0)
                    row['frame_start'] = st0[0]
                    fast_ms = (time.perf_counter() - tf) * 1000
                    row['fast_ms'] = round(fast_ms, 3)
                    state['f'].write(json.dumps(row, separators=(',', ':')) + '\n')
                    for r in focus:
                        r.update(frame=c0, frame_end=st1[1], torn=row['torn'])
                        state['fw'].write(json.dumps(r, separators=(',', ':')) + '\n')
                    sg = meta['segments'][-1]
                    sg['rows'] += 1
                    sg['rows_with_player'] += 1 if row['player'] and row['player'].get('body') else 0
                    sg['skipped_frames'] += row['skipped']
                    sg['torn_rows'] += row['torn']
                    sg['web_records'] += len(focus)
                    sg['shot_frames'] = sg.get('shot_frames', 0) + (1 if row['shots'] else 0)
                    sg['shot_missing_frames'] = sg.get('shot_missing_frames', 0) + row['shot_missing']
                    sg['new_instances'] = sg.get('new_instances', 0) + len(row['new_instances'])
                    sg['first_frame'] = sg['first_frame'] if sg['first_frame'] is not None else c0
                    sg['last_frame'] = c0
                    sg['fast_ms_max'] = max(sg['fast_ms_max'], round(fast_ms, 3))
                    state['last_rec_frame'] = c0
                    if time.perf_counter() - last_status > 2.0:
                        last_status = time.perf_counter()
                        print('  rec: %d rows, skipped %d, torn %d, web records %d' % (
                            sg['rows'], sg['skipped_frames'], sg['torn_rows'], sg['web_records']))
                # slow lane in the idle time of this frame
                if slow is not None and state['recording']:
                    while time.perf_counter() - tf < SLOW_BUDGET_S:
                        if game.stamp() != st0:
                            break
                        item = next(slow)
                        if item == 'idle':
                            break
                        if item is None:
                            continue
                        if item[0] == 'record':
                            s0 = game.stamp()
                            r = rec.web_record(item[1], 'sweep')
                            s1 = game.stamp()
                            r.update(frame=s0[1], frame_end=s1[1], torn=int(s1 != s0 or s0 != st0))
                            state['fw'].write(json.dumps(r, separators=(',', ':')) + '\n')
                            meta['segments'][-1]['web_records'] += 1
                        elif item[0] == 'sweep_done':
                            meta['segments'][-1]['web_sweeps'] += 1
            else:
                time.sleep(0.0005)
    except KeyboardInterrupt:
        pass
    finally:
        if state['recording']:
            stop_segment()
        winmm.timeEndPeriod(1)
        k32.CloseHandle(h)
        meta['total_rows_with_player'] = sum(s['rows_with_player'] for s in meta['segments'])
        with open(os.path.join(a.out, 'session.json'), 'w') as mf:
            json.dump(meta, mf, indent=1)
        print('Detached. Session saved to', a.out)


if __name__ == '__main__':
    main()
