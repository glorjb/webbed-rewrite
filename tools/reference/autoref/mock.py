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

from .driver import InputDriver
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
        self.cursor = (client[0] / 2.0, client[1] / 2.0)   # where the fake game sees the pointer

    def press(self, key):
        self.held.add(key.upper())
        self.events.append(('down', key.upper(), self.game.index))

    def release(self, key):
        self.held.discard(key.upper())
        self.events.append(('up', key.upper(), self.game.index))

    def move_mouse(self, x, y):
        self.cursor = self.mouse = (max(2, min(self.client[0] - 3, x)), max(2, min(self.client[1] - 3, y)))

    def client_size(self):
        return self.client

    def foreground_ok(self):
        return self.focus_lost_at is None or self.game.index < self.focus_lost_at

    def emergency_pressed(self):
        return self.emergency_at is not None and self.game.index >= self.emergency_at


class MockGame:
    ROOM = 18

    def __init__(self, bindings_keys=None, ground_y=600.0, ground_x=(-1e9, 1e9), ceiling_y=420.0, wall_x=None,
                 start_x=400.0, io_latency=1, drop_keys=(), torn_every=0, skip_every=0, no_player_frames=0,
                 freeze_at=None, respawn_at=None, release_mode='button-up', view_scale=0.75, start_frame=50000):
        self.keys = dict(bindings_keys or {'right': 'D', 'left': 'A', 'jump': 'SPACE', 'grapple': 'LMB',
                                           'weave': 'RMB', 'retract': 'SHIFT'})
        self.ground_y, self.ground_x, self.ceiling_y, self.wall_x = ground_y, ground_x, ceiling_y, wall_x
        self.io_latency, self.drop_keys = io_latency, {k.upper() for k in drop_keys}
        self.torn_every, self.skip_every = torn_every, skip_every
        self.no_player_frames, self.freeze_at, self.respawn_at = no_player_frames, freeze_at, respawn_at
        self.release_mode = release_mode
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
        mx, my = self.driver.cursor if self.driver else (640, 360)
        io['aimx'], io['aimy'] = self._room(mx, my)
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
            if io[kind + '_pressed'] and self.shot is None and self.anchor is None:
                tx, ty = io['aimx'], io['aimy']
                d = math.hypot(tx - self.x, ty - self.y) or 1.0
                self.shot = {'id': self._new(OBJ_WEBSHOT), 'x': self.x, 'y': self.y, 'gh': 35 * (tx - self.x) / d,
                             'gv': 35 * (ty - self.y) / d, 'travelled': 0.0, 'kind': kind}
        if self.shot and not io[self.shot['kind']]:
            self.shot = None
        if self.shot:
            s = self.shot
            s['x'] += s['gh']
            s['y'] += s['gv']
            s['travelled'] += 35
            hit = None
            if self.ceiling_y is not None and s['y'] <= self.ceiling_y:
                hit = (s['x'], self.ceiling_y)
            elif self.wall_x is not None and s['x'] >= self.wall_x:
                hit = (self.wall_x, s['y'])
            if s['travelled'] > wm.MAX_GDIST:
                self.shot = None
            elif hit:
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
            if io['retract']:
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
        self.cam = (self.x - 640 * self.view_scale, self.y - 360 * self.view_scale)   # camera follows the player

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
            mx, my = self.driver.cursor if self.driver else (640, 360)
            ctx, cty = self._room(mx, my)
            g = self._grounded() and self.anchor is None
            s = self.shot
            v = {'ground': 9001 if g else NOONE, 'webwalk': NOONE,
                 'attached': 7001 if self.anchor else NOONE, 'grapple': 1 if self.anchor else 0,
                 'weave': (row['io'] or {}).get('weave', 0), 'retract': (row['io'] or {}).get('retract', 0),
                 'in_water': 0, 'riding_skateboard': 0, 'parachute': 0, 'locked': 0, 'ground_angle': 0,
                 'webshot_node': s['id'] if s else NOONE, 'crosshair_tx': ctx, 'crosshair_ty': cty,
                 'gx': (s['x'] if s else (self.anchor[0] if self.anchor else None)),
                 'gy': (s['y'] if s else (self.anchor[1] if self.anchor else None)),
                 'gh': s['gh'] if s else 0.0, 'gv': s['gv'] if s else 0.0}
            row['player'] = {'id': self.pid, 'xy': [self.x, self.y], 'image_angle': 0.0, 'vars': v,
                             'body': {'pos_m': [self.x * sc, self.y * sc], 'vel_mps': [self.vx * sc, self.vy * sc],
                                      'angle_rad': 0.0, 'angvel_rps': 0.0, 'pos_px': [self.x, self.y],
                                      'vel_pxps': [self.vx, self.vy]}}
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
