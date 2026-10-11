"""LiveProvider: per-frame game state from the existing read-only probe (tools/reference/webbed_probe.py).

The probe module is reused, not modified or copied: attach (`find_pid`, read-only `OpenProcess`, build hash
check, ASLR rebase), `diagnose`, `Game` and `Recorder` (fast lane and slow lane) come from it. This class
only re-creates the probe's frame loop so the runner can act on each frame between captures:

  poll stamp -> new frame c0 -> Recorder.fast(...) -> stamp again -> torn = (stamp changed)
  -> runner decides inputs -> slow lane in the frame's idle time (budget SLOW_BUDGET_S, stops on new frame)

Rows are identical to the probe's (`torn`, `frame_start`, `fast_ms` added the same way).
"""
import ctypes
import hashlib
import json
import os
import struct
import sys
import time

from . import groundscan

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN_S = 2.0       # no new game frame for this long: game paused by the OS / minimised / closed


class GameLost(Exception):
    """The game stopped producing frames or the process went away."""


def import_probe():
    sys.path.insert(0, os.path.join(HERE, '..'))
    import webbed_probe            # Windows only (ctypes.WinDLL at import)
    return webbed_probe


class LiveProvider:
    def __init__(self, probe=None, slow_lane=True):
        self.p = probe or import_probe()
        p = self.p
        inv_dir = os.path.join(HERE, '..', '..', '..', 'docs', 'inventory')
        with open(os.path.join(inv_dir, 'names.json')) as f:
            inv = json.load(f)
        self.room_names, self.obj_names = inv.get('ROOM', []), inv.get('OBJT', [])
        self.pid = p.find_pid()
        if not self.pid:
            raise GameLost('webbed.exe is not running: start Webbed from Steam and enter a level')
        self.h = p.k32.OpenProcess(0x0010 | 0x1000, False, self.pid)   # VM_READ | QUERY_LIMITED_INFORMATION
        if not self.h:
            raise GameLost('OpenProcess (read-only) failed: %d' % ctypes.get_last_error())
        with open(p.exe_path(self.h), 'rb') as f:
            self.digest = hashlib.sha256(f.read()).hexdigest()
        if self.digest != p.EXPECTED_SHA256:
            raise GameLost('webbed.exe build mismatch (%s); the probe only supports %s' % (self.digest, p.BUILD))
        base = p.module_base(self.pid)
        if not base:
            raise GameLost('could not find the webbed.exe module base')
        with open(os.path.join(inv_dir, 'yyc_vars.json')) as f:
            var_ids = json.load(f)
        self.game = p.Game(p.Mem(self.h), var_ids, base)
        self.id2name = {v: k for k, v in var_ids.items() if v >= 0}
        self.base = base
        self.rec = p.Recorder(self.game)
        self.rec.rediscover()
        self.slow = self.rec.slow_lane() if slow_lane else None
        self.t0 = time.perf_counter()
        st = self.game.stamp()
        self.last_counter = st[1] if st else None
        self.st0 = None
        self.tf = None
        p.ctypes.WinDLL('winmm').timeBeginPeriod(1)

    def meta(self):
        return {'build': self.p.BUILD, 'sha256': self.digest, 'module_base': '0x%08X' % self.base}

    def diagnose(self):
        return self.p.diagnose(self.game, self.room_names, self.obj_names)

    def room_name(self, idx):
        return self.room_names[idx] if idx is not None and 0 <= idx < len(self.room_names) else '?'

    def next_frame(self, last_rec_frame, marker, tick=None):
        """Block until the next game frame; return (row, focus_web_records). `tick` runs every poll (safety)."""
        g = self.game
        t_wait = time.perf_counter()
        while True:
            if tick:
                tick()
            st0 = g.stamp()
            if st0 is None:
                raise GameLost('cannot read the game any more (process closed?)')
            if st0[1] != self.last_counter:
                break
            if time.perf_counter() - t_wait > FROZEN_S:
                raise GameLost('no new game frame for %.1f s (game minimised, paused by Windows, or closed)' % FROZEN_S)
            time.sleep(0.0005)
        c0 = st0[1]
        self.last_counter = c0
        tf = time.perf_counter()
        row, focus = self.rec.fast(c0, last_rec_frame, tf - self.t0, marker)
        st1 = g.stamp()
        row['torn'] = int(st1 != st0)
        row['frame_start'] = st0[0]
        row['fast_ms'] = round((time.perf_counter() - tf) * 1000, 3)
        for r in focus:
            r.update(frame=c0, frame_end=st1[1] if st1 else None, torn=row['torn'])
        self.st0, self.tf = st0, tf
        return row, focus

    def idle(self, writer=None):
        """Slow lane in this frame's idle time (exactly the probe's budget and stop rule). Web sweep records are
        written only while a segment is recording; the sweep itself always runs, because it also refreshes the
        player / input pointers (title screen and hub share room_Hub)."""
        if self.slow is None or self.st0 is None:
            return
        g = self.game
        while time.perf_counter() - self.tf < self.p.SLOW_BUDGET_S:
            if g.stamp() != self.st0:
                break
            item = next(self.slow)
            if item == 'idle':
                break
            if item is None:
                continue
            if item[0] == 'record':
                if writer is None or not writer.recording:
                    continue
                s0 = g.stamp()
                r = self.rec.web_record(item[1], 'sweep')
                s1 = g.stamp()
                r.update(frame=s0[1], frame_end=s1[1], torn=int(s1 != s0 or s0 != self.st0))
                writer.write_sweep(r)
            elif item[0] == 'sweep_done' and writer is not None:
                writer.sweep_done()

    def all_vars(self, which):
        """EVERY numeric instance variable of the player ('player') or the input object ('io'), by name
        (read-only hashmap walk, same layout as webbed_probe.Game.vars but without a name filter)."""
        inst = self.rec.player if which == 'player' else self.rec.io
        m = self.game.m
        vmap = m.u32(inst + self.p.INST_VARMAP) if inst else None
        hdr = m.raw(vmap, 0x14) if vmap else None
        if not hdr:
            return {}
        size, _, _, _, elems = struct.unpack('<5I', hdr)
        if not elems or not (0 < size <= 1 << 16):
            return {}
        blob = m.raw(elems, size * 12)
        out = {}
        for vptr, key, hsh in struct.iter_unpack('<IiI', blob or b''):
            if hsh and vptr:
                v = m.rvalue(vptr)
                if isinstance(v, (int, float)):
                    out[self.id2name.get(key, 'var%d' % key)] = v
        return out

    def ground_profile(self, row):
        """groundscan estimate around the player in `row` (read-only; one instance walk)."""
        b = (row.get('player') or {}).get('body')
        if not b:
            return {'confidence': 'unavailable', 'reason': 'no player body'}
        if not hasattr(self, '_inv'):
            self._inv = groundscan.load_inventory()
        solids = groundscan.live_solids(self.game, self.p, self.obj_names, *self._inv)
        return groundscan.ground_profile(solids, b['pos_px'], ((row['player'].get('vars') or {}).get('ground')))

    def close(self):
        try:
            self.p.ctypes.WinDLL('winmm').timeEndPeriod(1)
        finally:
            self.p.k32.CloseHandle(self.h)
