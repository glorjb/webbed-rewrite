"""MockGame / MockInputDriver: a fake game-state provider for unit tests and `auto_runner.py --mock`.

!!! The mock's dynamics are NOT the recovered Webbed physics and its output is NOT a reference capture. !!!
It produces rows in the probe's schema with plausible state so the runner's control flow, acknowledgement
logic, quality gate, failure preservation and safety paths can be tested without the game. Only free flight
reuses sim/webbed_model (so the mock behaves sanely in the air); walking, grapple and weave are crude stand-ins.

Fault injection (constructor keyword arguments) lets tests produce: dropped inputs, input latency, torn rows,
skipped frames, a missing player, a frozen game, a respawn, focus loss and an emergency stop.
"""
import math
import os
import sys

from . import groundscan as G
from .driver import InputDriver
from .inputhook import EMERGENCY_VKS, WM_MOUSEMOVE, TakeoverMonitor
from .provider import GameLost

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'sim'))
import webbed_model as wm   # noqa: E402

R = wm.PLAYER_RADIUS_PX
H = 1.0 / wm.ROOM_SPEED
NOONE = -4
OBJ_WEBSHOT, OBJ_WEB = 106, 107


class MockInputDriver(InputDriver):
    def __init__(self, game, client=(1280, 720)):
        super().__init__()
        self.game = game
        game.driver = self
        self.client = client
        self.events = []
        self.focus_lost_at = None
        self.emergency_at = None
        self.refocus_ok = True              # focus_game() succeeds (False: Windows refused and nobody clicked)
        self.monitor = TakeoverMonitor()    # same logic as the live low-level hooks
        self.physical_at = []               # [(frame index, 'move' | 'key' | 'button' | 'f12')]: simulated user input
        self.cursor = (client[0] / 2.0, client[1] / 2.0)   # where Windows reports the pointer
        self.aim_cursor = self.cursor                        # where the fake game last sampled it

    def tick(self, index):
        """Feed scheduled simulated input to the takeover monitor (what the live hooks would report)."""
        for at, kind in [e for e in self.physical_at if e[0] == index]:
            if kind == 'move':
                x, y = self.cursor
                self.monitor.on_mouse(WM_MOUSEMOVE, x, y, False, False)
                self.monitor.on_mouse(WM_MOUSEMOVE, x + 20, y, False, False)
            elif kind == 'button':
                self.monitor.on_mouse(0x0201, 0, 0, False, False)
            elif kind == 'key':
                self.monitor.on_key(0x41, True, False, False)
            elif kind == 'esc':
                self.monitor.on_key(0x1B, True, False, False)
            elif kind == 'f12':
                self.monitor.on_key(EMERGENCY_VKS[0], True, False, False)

    def arm(self):
        self.mouse = None
        self.monitor.arm()

    def disarm(self):
        self.monitor.disarm()

    def takeover(self):
        return self.monitor.takeover()

    def cursor_client(self):
        return self.cursor

    def cursor_state(self):
        return {'visible': not self.game.cursor_recenters, 'clip_screen': [0, 0, 0, 0], 'client_size': list(self.client)}

    def move_mouse_relative(self, dx, dy):
        if self.game.mouse_relative:
            self.game.pending_rel = (self.game.pending_rel[0] + dx, self.game.pending_rel[1] + dy)
        else:
            self.move_mouse(self.cursor[0] + dx, self.cursor[1] + dy)

    def press(self, key):
        self.held.add(key.upper())
        self.events.append(('down', key.upper(), self.game.index))

    def release(self, key):
        self.held.discard(key.upper())
        self.events.append(('up', key.upper(), self.game.index))

    def move_mouse(self, x, y):
        self.cursor = self.mouse = self.aim_cursor = (max(2, min(self.client[0] - 3, x)),
                                                      max(2, min(self.client[1] - 3, y)))

    def client_size(self):
        return self.client

    def foreground_ok(self):
        return self.focus_lost_at is None or self.game.index < self.focus_lost_at

    def emergency_pressed(self):
        return self.monitor.emergency or (self.emergency_at is not None and self.game.index >= self.emergency_at)

    def focus_game(self, timeout_s=15.0):
        if self.emergency_pressed():
            from .driver import AbortRun
            raise AbortRun('EMERGENCY STOP (F12 / Pause pressed) while waiting for the game window')
        if self.refocus_ok and not self.foreground_ok():   # repairs a loss that happened, not a future one
            self.focus_lost_at = None
        self.mouse = None
        return self.foreground_ok()


class MockGame:
    ROOM = 18

    def __init__(self, bindings_keys=None, ground_y=600.0, ground_x=(-1e9, 1e9), ceiling_y=420.0, wall_x=None,
                 start_x=400.0, io_latency=1, drop_keys=(), torn_every=0, skip_every=0, no_player_frames=0,
                 freeze_at=None, respawn_at=None, release_mode='button-up', view_scale=0.75, start_frame=50000,
                 io_absent=(), weave_reels_in=False, jitter_pxps=0.0, cursor_recenters=False,
                 crosshair_static=False, mouse_relative=False, floor_attaches=True, scan_unavailable=False,
                 gh_reset_on_attach=True, camera_offset=(0.0, 0.0), extra_solids=(), menu_frames=0):
        self.keys = dict(bindings_keys or {'right': 'D', 'left': 'A', 'jump': 'SPACE', 'grapple': 'LMB',
                                           'weave': 'RMB', 'retract': 'SHIFT'})
        self.ground_y, self.ground_x, self.ceiling_y, self.wall_x = ground_y, ground_x, ceiling_y, wall_x
        self.io_latency, self.drop_keys = io_latency, {k.upper() for k in drop_keys}
        self.torn_every, self.skip_every = torn_every, skip_every
        self.no_player_frames, self.freeze_at, self.respawn_at = no_player_frames, freeze_at, respawn_at
        self.release_mode = release_mode
        self.io_absent = set(io_absent)         # oIO variables that do not exist (probe omits absent variables)
        self.weave_reels_in = weave_reels_in    # mock stand-in: weave held on a grapple shortens the rope
        self.jitter_pxps = jitter_pxps          # body velocity noise (never settles when > MOVE_MAX_PXPS)
        self.cursor_recenters = cursor_recenters  # the game warps the cursor to the window centre every frame
        self.crosshair_static = crosshair_static  # crosshair_tx/ty never follow the cursor (live finding)
        self.mouse_relative = mouse_relative    # aim moves only with relative mouse motion (absolute ignored)
        # relative mode (live 2026-10-11: cursor locked at the centre, aim from deltas): a virtual cursor in
        # client px, moved by abs-move deltas (position - lock point) and relative moves, clamped to the window
        self.vcur = (640.0 + 133.0, 360.0 - 80.0)
        self.pending_rel = (0.0, 0.0)
        self.floor_attaches = floor_attaches    # a shot reaching the floor attaches there
        self.scan_unavailable = scan_unavailable
        self.gh_reset_on_attach = gh_reset_on_attach   # gh/gv read 0 once the shot is gone (unknown in the game)
        self.camera_offset = camera_offset      # player's screen position minus the window centre (client px)
        self.extra_solids = list(extra_solids)  # groundscan solids added to the floor (tests: unknown shapes etc.)
        self.menu_frames = menu_frames          # a pause menu is open for the first N frames
        self.shot_vars = {'gh': 0.0, 'gv': 0.0, 'sx': None, 'sy': None, 'gx': None, 'gy': None}
        self.cross0 = None
        self.view_scale = view_scale
        self.driver = None
        self.index = 0                      # frames produced
        self.counter = start_frame          # game frame counter
        self.x, self.y = start_x, ground_y - R
        self.vx = self.vy = 0.0
        self.pid = 100001
        self.cam = (start_x - 640 * view_scale, self.y - 360 * view_scale)
        self.history = []                   # held-key snapshots per frame
        self.prev_io = {}
        self.shot = None                    # dict(id, x, y, gh, gv, travelled, kind)
        self.anchor = None                  # (x, y, rope_len)
        self.toggle_attached = False
        self.webs = []                      # permanent webs: dict(id, ox, oy, ax, ay, length)
        self.next_id = 200000
        self.new_events = []
        self.t = 0.0

    # ---------------------------------------------------------------- provider interface
    def meta(self):
        return {'build': 'MOCK (not a game capture)', 'sha256': None, 'mock': True}

    def diagnose(self):
        row, _ = self._row(None, 0)
        return {'report': ['MOCK game (no real process)'], 'counter_advancing': True,
                'ok_for_recording': row['player'] is not None, 'row': row}

    def room_name(self, idx):
        return 'room_Hub(mock)'

    def all_vars(self, which):
        pending = self.new_events                     # _row consumes pending new-instance events: keep them
        row, _ = self._row(None, 0)
        self.new_events = pending
        if which == 'io':
            return {k: v for k, v in (row['io'] or {}).items() if isinstance(v, (int, float))}
        v = dict(((row['player'] or {}).get('vars')) or {})
        v.update(mock_unrelated_counter=self.index % 7, mock_constant=3.0)
        return {k: x for k, x in v.items() if isinstance(x, (int, float))}

    SPR_GROUND = {'name': 'sGround', 'w': 32, 'h': 32, 'ox': 16, 'oy': 16}

    def ground_solids(self):
        x0, x1 = max(self.ground_x[0], self.x - 5000), min(self.ground_x[1], self.x + 5000)
        floor = G.make_solid(9001, 'oGround', (x0 + x1) / 2.0, self.ground_y + 16, (x1 - x0) / 32.0, 1.0, 0.0,
                             self.SPR_GROUND, 1)
        return [floor] + self.extra_solids

    def ground_profile(self, row):
        if self.scan_unavailable:
            return {'confidence': 'unavailable', 'reason': 'mock: scan unavailable'}
        return G.ground_profile(self.ground_solids(), (self.x, self.y), 9001)

    def pause_state(self):
        return {'menus': ['oSettingsMenu'] if self.index <= self.menu_frames else [], 'paused': {}}

    def idle(self, writer=None):
        pass

    def close(self):
        pass

    def next_frame(self, last_rec_frame, marker, tick=None):
        if tick:
            tick()
        if self.freeze_at is not None and self.index >= self.freeze_at:
            raise GameLost('no new game frame for 2.0 s (mock freeze)')
        self.index += 1
        if self.driver:
            self.driver.tick(self.index)
        self.counter += 2 if (self.skip_every and self.index % self.skip_every == 0) else 1
        self.t += H
        self._step()
        return self._row(last_rec_frame, marker)

    # ---------------------------------------------------------------- simulation (mock, not Webbed physics)
    def _io(self):
        k = self.history[-1 - self.io_latency] if len(self.history) > self.io_latency else set()
        k = {x for x in k if x not in self.drop_keys}
        on = lambda a: self.keys.get(a, '').upper() in k   # noqa: E731
        io = {'hm': float(on('right')) - float(on('left')), 'vm': 0.0, 'jmp': 0, 'jmp_held': int(on('jump')),
              'grapple': int(on('grapple')), 'weave': int(on('weave')), 'retract': int(on('retract')),
              'mvdir': 0.0, 'mvmag': 0.0}
        for a in ('grapple', 'weave'):
            io[a + '_pressed'] = int(io[a] and not self.prev_io.get(a))
            io[a + '_released'] = int(not io[a] and bool(self.prev_io.get(a)))
        io['jmp'] = int(io['jmp_held'] and not self.prev_io.get('jmp_held'))
        mx, my = self.driver.aim_cursor if self.driver else (640, 360)
        io['aimx'], io['aimy'] = self._room(mx, my)
        if self.mouse_relative:
            d = self.driver
            cx, cy = (d.client[0] / 2.0, d.client[1] / 2.0) if d else (640.0, 360.0)
            ax, ay = d.aim_cursor if d else (cx, cy)
            vx = self.vcur[0] + (ax - cx) + self.pending_rel[0]
            vy = self.vcur[1] + (ay - cy) + self.pending_rel[1]
            self.vcur = (max(0.0, min(2 * cx, vx)), max(0.0, min(2 * cy, vy)))
            self.pending_rel = (0.0, 0.0)
            if d:
                d.aim_cursor = d.cursor = (cx, cy)          # the game recentres the cursor after reading it
            rx, ry = self._room(*self.vcur)
            io.pop('aimx', None)
            io.pop('aimy', None)
            io.update(mx=rx, my=ry, gmx=self.vcur[0], gmy=self.vcur[1],
                      aimdir=wm.point_direction(self.x, self.y, rx, ry))
            self.aim_point = (rx, ry)
        else:
            self.aim_point = (io['aimx'], io['aimy'])
        for k in self.io_absent:
            io.pop(k, None)
        return io

    def _room(self, mx, my):
        return self.cam[0] + mx * self.view_scale, self.cam[1] + my * self.view_scale

    def _grounded(self):
        return self.ground_x[0] <= self.x <= self.ground_x[1] and self.y >= self.ground_y - R - 0.01 and self.vy >= 0

    def _new(self, obj):
        self.next_id += 1
        self.new_events.append({'frame': self.counter, 'id': self.next_id, 'obj': obj})
        return self.next_id

    def _step(self):
        self.history.append(set(self.driver.held) if self.driver else set())
        io = self.io = self._io()
        if self.respawn_at is not None and self.index == self.respawn_at:
            self.pid += 1
            self.x += 300
        # web shot spawn / cancel
        for kind in ('grapple', 'weave'):
            if io.get(kind + '_pressed') and self.shot is None and self.anchor is None:
                tx, ty = self.aim_point
                d = math.hypot(tx - self.x, ty - self.y) or 1.0
                self.shot = {'id': self._new(OBJ_WEBSHOT), 'x': self.x, 'y': self.y, 'gh': 35 * (tx - self.x) / d,
                             'gv': 35 * (ty - self.y) / d, 'travelled': 0.0, 'kind': kind}
                self.shot_vars.update(gh=self.shot['gh'], gv=self.shot['gv'], sx=self.x, sy=self.y)
        if self.shot and not io.get(self.shot['kind']):
            self.shot = None
        if self.shot:
            s = self.shot
            x0, y0 = s['x'], s['y']
            s['x'] += s['gh']
            s['y'] += s['gv']
            s['travelled'] += 35
            self.shot_vars.update(gx=s['x'], gy=s['y'])

            def on_ray_y(yy):                      # the hit point stays on the shot's straight line
                t = (yy - y0) / s['gv'] if s['gv'] else 0.0
                return (x0 + t * s['gh'], yy)
            hit = None
            if self.ceiling_y is not None and s['y'] <= self.ceiling_y:
                hit = on_ray_y(self.ceiling_y)
            elif self.floor_attaches and s['y'] >= self.ground_y and self.ground_x[0] <= s['x'] <= self.ground_x[1]:
                hit = on_ray_y(self.ground_y)
            elif self.wall_x is not None and s['x'] >= self.wall_x:
                t = (self.wall_x - x0) / s['gh'] if s['gh'] else 0.0
                hit = (self.wall_x, y0 + t * s['gv'])
            if s['travelled'] > wm.MAX_GDIST:
                self.shot = None
            elif hit:
                self.shot_vars.update(gx=hit[0], gy=hit[1])
                if self.gh_reset_on_attach:
                    self.shot_vars.update(gh=0.0, gv=0.0)
                if s['kind'] == 'grapple':
                    self.anchor = (hit[0], hit[1], math.hypot(hit[0] - self.x, hit[1] - self.y))
                else:
                    self.webs.append({'id': self._new(OBJ_WEB), 'ox': self.x, 'oy': self.y + R, 'ax': hit[0],
                                      'ay': hit[1], 'length': math.hypot(hit[0] - self.x, hit[1] - self.y - R)})
                self.shot = None
        # detach
        if self.anchor:
            if self.release_mode == 'button-up' and not io['grapple']:
                self.anchor = None
            elif self.release_mode == 'toggle':
                if io['grapple_released'] and self.toggle_attached:
                    self.anchor = None
                    self.toggle_attached = False
                elif io['grapple_released']:
                    self.toggle_attached = True
        # motion
        if self.anchor:
            ax, ay, L = self.anchor
            if io.get('retract') or (self.weave_reels_in and io.get('weave')):
                L = max(20.0, L - 2.0)
                self.anchor = (ax, ay, L)
            self.vy += wm.gravity_dv_pxps_per_step()
            self.x += self.vx * H
            self.y += self.vy * H
            dx, dy = self.x - ax, self.y - ay
            d = math.hypot(dx, dy) or 1.0
            if d > L:
                nx, ny = dx / d, dy / d
                self.x, self.y = ax + nx * L, ay + ny * L
                vr = self.vx * nx + self.vy * ny
                if vr > 0:
                    self.vx -= vr * nx
                    self.vy -= vr * ny
        elif self._grounded():
            target = io['hm'] * wm.MAX_WALKSPD * wm.ROOM_SPEED
            acc = 900.0 * H
            self.vx += max(-acc, min(acc, target - self.vx))
            self.vy = 0.0
            if io['jmp']:
                self.vy = -wm.JUMP_POWER * wm.ROOM_SPEED
            self.x += self.vx * H
            self.y += self.vy * H
        else:
            dvx, dvy = wm.player_free_flight_dv(self.vx, self.vy)
            self.vx += dvx
            self.vy += dvy
            self.x += self.vx * H
            self.y += self.vy * H
        if self.ceiling_y is not None and self.y - R < self.ceiling_y:
            self.y = self.ceiling_y + R
            self.vy = -wm.PLAYER_RESTITUTION * self.vy
        if self.ground_x[0] <= self.x <= self.ground_x[1] and self.y > self.ground_y - R:
            self.y, self.vy = self.ground_y - R, 0.0
        if self.y > 5000:                    # fell out of the world: respawn
            self.pid += 1
            self.x, self.y, self.vx, self.vy = 400.0, self.ground_y - R, 0.0, 0.0
        self.prev_io = io
        self.cam = (self.x - (640 + self.camera_offset[0]) * self.view_scale,
                    self.y - (360 + self.camera_offset[1]) * self.view_scale)   # camera follows the player
        if self.cursor_recenters and self.driver:
            self.driver.cursor = (self.driver.client[0] / 2.0, self.driver.client[1] / 2.0)   # SetCursorPos-like

    def _jit(self):
        return self.jitter_pxps * (1 if self.index % 2 else -1)

    # ---------------------------------------------------------------- row in the probe schema
    def _row(self, last_rec_frame, marker):
        has_player = self.index > self.no_player_frames
        sc = wm.M_PER_PX
        row = {'frame': self.counter,
               'skipped': max(0, self.counter - last_rec_frame - 1) if last_rec_frame is not None else 0,
               't': round(self.t, 6), 'marker': marker, 'room': self.ROOM,
               'world': {'speed': 60, 'iterations': 10, 'm_per_px': sc}, 'player': None,
               'io': getattr(self, 'io', None) or {'hm': 0.0, 'grapple': 0, 'weave': 0, 'retract': 0, 'jmp': 0,
                                                   'jmp_held': 0},
               'skateboards': [], 'shots': [], 'nodes_recent': [], 'new_instances': self.new_events,
               'shot_missing': 0, 'torn': int(bool(self.torn_every) and self.index % self.torn_every == 0),
               'frame_start': self.counter * 16667, 'fast_ms': 0.4}
        self.new_events = []
        if has_player:
            ctx, cty = getattr(self, 'aim_point', self._room(640, 360))
            if self.mouse_relative:
                ad = math.radians(wm.point_direction(self.x, self.y, ctx, cty))
                v_rel = {'aimx': ctx, 'aimy': cty, 'abx': self.x - 6 * math.cos(ad), 'aby': self.y + 6 * math.sin(ad)}
            else:
                v_rel = {}
            if self.crosshair_static:
                self.cross0 = self.cross0 or (ctx, cty)
                ctx, cty = self.cross0
            g = self._grounded() and self.anchor is None
            s = self.shot
            v = {'ground': 9001 if g else NOONE, 'webwalk': NOONE,
                 'attached': 7001 if self.anchor else NOONE, 'grapple': 1 if self.anchor else 0,
                 'weave': (row['io'] or {}).get('weave', 0), 'retract': 0,
                 'in_water': 0, 'riding_skateboard': 0, 'parachute': 0, 'locked': 0, 'ground_angle': 0,
                 'webshot_node': s['id'] if s else NOONE, 'crosshair_tx': ctx, 'crosshair_ty': cty,
                 'gx': self.shot_vars['gx'], 'gy': self.shot_vars['gy'], 'sx': self.shot_vars['sx'],
                 'sy': self.shot_vars['sy'], 'gh': self.shot_vars['gh'], 'gv': self.shot_vars['gv']}
            v.update(v_rel)
            row['player'] = {'id': self.pid, 'xy': [self.x, self.y], 'image_angle': 0.0, 'vars': v,
                             'body': {'pos_m': [self.x * sc, self.y * sc], 'vel_mps': [self.vx * sc, self.vy * sc],
                                      'angle_rad': 0.0, 'angvel_rps': 0.0, 'pos_px': [self.x, self.y],
                                      'vel_pxps': [self.vx + self._jit(), self.vy]}}
            if s:
                row['shots'].append({'id': s['id'], 'obj': OBJ_WEBSHOT, 'xy': [s['x'], s['y']], 'image_angle': 0.0,
                                     'body': {'pos_px': [s['x'], s['y']], 'vel_pxps': [s['gh'] * 60, s['gv'] * 60]}})
        focus = []
        for w in self.webs:
            cl = math.hypot(w['ax'] - w['ox'], w['ay'] - w['oy'])
            st = wm.web_stretch_for(w['length'])
            focus.append({'lane': 'focus', 'id': w['id'], 'o_pose': None, 'a_pose': None, 'frame': self.counter,
                          'frame_end': self.counter, 'torn': row['torn'],
                          'vars': {'ox': w['ox'], 'oy': w['oy'], 'ax': w['ax'], 'ay': w['ay'], 'length': w['length'],
                                   'current_length': cl, 'dir': wm.point_direction(w['ox'], w['oy'], w['ax'], w['ay']),
                                   'stretch': st, 'snap_threshold': wm.web_snap_threshold_for(st), 'breakable': 1}})
        return row, focus
