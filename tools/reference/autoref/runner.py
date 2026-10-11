"""Orchestration of automated reference recordings.

Per scenario:
  1. wait until the spider is controllable and at rest (state.readiness + SettleTracker), else `not_ready`;
  2. create a NEW session directory (never reuses or touches existing captures) and start recording;
  3. drive the scenario generator one game frame at a time; safety is checked on every poll;
  4. release all inputs, record a short tail, stop recording;
  5. judge: scenario result + frame consistency + the SAME quality gate as the tests (capture_lib.Session);
  6. failed attempts are MOVED to <captures>/../captures_failed/ with auto_run.json (reason, measurements),
     so they are preserved for investigation without making the regression suite fail; passing sessions
     stay in the capture root where the tests read them;
  7. retry transient failures (missed input, invalid capture, inconsistent frames, timeout, aim) up to N times.
Then the unittest suite runs against the capture root and everything goes into one report.
"""
import datetime
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

from . import aim as A
from . import bindings as B
from . import groundscan as G
from . import state as S
from .capture import SessionWriter
from .driver import AbortRun, SafetyGuard
from .provider import GameLost
from .scenarios import (ACK_FRAMES, SCENARIOS, SIGHT_ANGLES, SUITES, Ctx, RelativeAim, ScenarioFailure,
                        sight)

HERE = os.path.dirname(os.path.abspath(__file__))
RETRYABLE = {'missed_input', 'capture_invalid', 'inconsistent', 'timeout', 'aim_failed', 'not_controllable'}
# 'blocked' is NOT retried: the same spot blocks again; the failed recording is kept for analyze_walk.py
HANDS_OFF_S = 3.0                    # the user must not have touched mouse/keyboard for this long before arming
SETUP_WINDOW_FRAMES = 15 * 60        # after a setup announcement: time to start moving the spider into place
PREPARE_TIMEOUT_FRAMES = 120 * 60    # setup + readiness (closing menus, unpausing, positioning) may take a while
REFOCUS_EVERY_S = 5.0
TAIL_FRAMES = 30
READY_TIMEOUT_FRAMES = 1200          # 20 s to become controllable and settle
RELEVANT_TESTS = {
    'M2': ['test_walk_plateau_speed', 'test_free_fall_gravity', 'test_world_settings_every_frame',
           'test_every_capture_is_usable'],
    'W1': ['WebShot', 'test_free_fall_gravity', 'test_every_capture_is_usable'],
    'W2': ['WebShot', 'test_free_fall_gravity', 'WebGeometry', 'test_every_capture_is_usable'],
    'W3': ['WebGeometry', 'test_every_capture_is_usable'],
    'W4': ['WebShot', 'test_every_capture_is_usable'],
}


def stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


class Config:
    def __init__(self, capture_root, retries=1, prompt=True, run_tests=True, mouse_failsafe=True,
                 bindings_file=None, mode='live', trust_ground=False):
        self.capture_root = capture_root
        parent = os.path.dirname(os.path.normpath(capture_root))
        self.failed_root = os.path.join(parent, 'captures_failed')
        self.runs_root = os.path.join(parent, 'auto_runs')
        self.bindings_file = bindings_file or os.path.join(parent, 'auto_bindings.local.json')
        self.aim_profile_file = os.path.join(parent, 'aim_profile.local.json')
        self.state_profile_file = os.path.join(parent, 'game_state_profile.local.json')
        self.retries, self.prompt, self.run_tests = retries, prompt, run_tests
        self.mouse_failsafe, self.mode = mouse_failsafe, mode
        self.trust_ground = trust_ground    # DIAGNOSTIC: walk although the ground scan is ambiguous / unavailable
        self.setup_window_frames = SETUP_WINDOW_FRAMES


class ConsoleUI:
    def __init__(self, interactive=True):
        self.interactive = interactive

    def say(self, msg=''):
        print(msg, flush=True)

    def beep(self, n=1):
        try:
            import winsound
            for _ in range(n):
                winsound.Beep(880, 120)
        except Exception:
            pass

    def ask(self, msg):
        """Returns '' (go), 's' (skip) or 'q' (quit). Non-interactive: always go."""
        if not self.interactive:
            return ''
        try:
            return input(msg + ' [Enter = go, s = skip, q = quit] ').strip().lower()[:1]
        except EOFError:
            return 'q'


class Runner:
    def __init__(self, provider, driver, bindings, cfg, ui=None, watchdog=None):
        self.p, self.d, self.b, self.cfg = provider, driver, bindings, cfg
        self.ui = ui or ConsoleUI(cfg.prompt)
        self.guard = SafetyGuard(driver, watchdog, mouse_failsafe=cfg.mouse_failsafe)
        self.results = []
        self.created = set()            # session dirs created by this run (the only ones it may move)
        self.last_row = None
        self.io_keys_seen = set()       # oIO variables that actually exist (the probe returns only present ones)
        self.calibration = None         # per-action calibration report (for the run report)
        self.state_profile = self._load_state_profile()
        self.last_game_state = None
        self.aim_profile = self._load_aim_profile()

    # ---------------------------------------------------------------- frame loop
    def frame(self, writer=None, marker=0):
        rec = writer is not None and writer.recording
        row, focus = self.p.next_frame(writer.last_rec_frame if rec else None, marker, tick=self.guard.check)
        if rec:
            writer.write_row(row, focus)
        self.p.idle(writer)
        self.last_row = row
        self.io_keys_seen.update(row.get('io') or {})
        return row

    def drive(self, gen, ctx, writer=None, max_frames=6000, issues=None):
        """Run generator `gen` (yield = next frame). Returns normally when it finishes."""
        prev = ctx.row
        try:
            next(gen)
            while True:
                row = self.frame(writer, ctx.marker)
                if issues is not None:
                    issues.extend('frame %d: %s' % (row['frame'], i) for i in S.consistency_issues(prev, row))
                prev = row
                ctx.observe(row)
                if ctx.frames > max_frames:
                    raise ScenarioFailure('timeout', 'scenario exceeded %d frames' % max_frames)
                gen.send(None)
        except StopIteration:
            return

    def pause_reasons(self, state):
        """Interpret game_state evidence. Menu instances alone are NOT 'open' (they can persist hidden); blocking
        evidence is: a `paused` variable that is set on some instance, or the measured -StateScan indicator."""
        out = []
        on = [o for o in state.get('paused_owners', []) if o.get('value') not in (None, 0, False, 0.0)]
        if on:
            out.append('the game is paused (%s)' % ', '.join('%s#%s.paused=%s' % (o['obj'], o['id'], o['value'])
                                                              for o in on))
        ind = self.state_profile
        if ind and hasattr(self.p, 'indicator_value'):
            v = self.p.indicator_value(ind)
            if v in ind['open_values']:
                out.append('a menu is open (%s.%s=%s, indicator measured by -StateScan)' % (ind['obj'], ind['var'], v))
        return out

    def wait_ready(self, need_support, timeout=READY_TIMEOUT_FRAMES, hands_off=False, refocus=False,
                   progress=False):
        """Wait until controllable, unpaused and settled. Returns [] when ready, else the last reasons.
        hands_off: also require no physical mouse/keyboard input for HANDS_OFF_S (setup phases, unarmed).
        refocus: when Webbed is not in front and the user is idle, bring it back (non-injecting) every few s.
        Focus loss itself only aborts when armed (SafetyGuard)."""
        tr = S.SettleTracker()
        reasons = ['no frame']
        best, speeds = 0, []
        pause_r, last_focus_try, last_say = [], 0.0, 0.0
        self.last_game_state = None
        for n in range(timeout):
            row = self.frame()
            reasons = S.readiness(row, need_support)
            if n % 30 == 0 and hasattr(self.p, 'game_state'):
                self.last_game_state = self.p.game_state()     # an instance walk, so not every frame
                pause_r = self.pause_reasons(self.last_game_state)
            if pause_r:
                reasons = reasons + pause_r + ['the runner waits; close menus / unpause in Webbed']
                tr.frames = 0
            if refocus and not self.d.foreground_ok():
                reasons = reasons + ['Webbed is not the foreground window']
                now = time.monotonic()
                if self.d.user_idle_seconds() >= HANDS_OFF_S and now - last_focus_try >= REFOCUS_EVERY_S:
                    last_focus_try = now
                    self.d.focus_game(timeout_s=2.0)
            if hands_off and self.d.user_idle_seconds() < HANDS_OFF_S:
                reasons = reasons + ['you touched mouse/keyboard %.1f s ago (hands off for %.0f s to start)'
                                     % (self.d.user_idle_seconds(), HANDS_OFF_S)]
            tr.update(row)
            best = max(best, tr.frames)
            if S.body(row):
                speeds.append(S.speed_pxps(row))
            if not reasons and tr.settled:
                return []
            if progress and time.monotonic() - last_say > 5.0 and n > 0:
                last_say = time.monotonic()
                self.ui.say('     waiting: %s' % ('; '.join(reasons) if reasons else 'spider settling'))
        if not reasons and not tr.settled:
            recent = speeds[-60:] or [float('nan')]
            reasons = ['the spider did not come to rest: needs |v| < %.1f px/s for %d consecutive frames; longest '
                       'still run %d frames; |v| over the last second %.2f..%.2f px/s (support=%s)'
                       % (S.MOVE_MAX_PXPS, S.SETTLE_FRAMES, best, min(recent), max(recent),
                          S.support(self.last_row))]
        return reasons

    def prepare(self, phase, need_support, setup_text=None, gate=None):
        """The one start-up path before the runner sends ANY input:
          1. disarmed (your input is allowed: walk the spider, close menus, unpause);
          2. optional setup window: the setup is announced; you get time to move the spider (no Enter needed);
          3. Webbed brought to the front automatically (non-injecting), re-tried while you are idle;
          4. wait: controllable, not paused / no measured menu open, at rest, and hands off for HANDS_OFF_S;
          5. optional gate (e.g. the ground scan) on the ready state;
          6. armed (takeover + focus-loss protection), right before the first input.
        F12 / Pause stop at every step. Returns a list of reasons when not ready (nothing was sent)."""
        self.d.disarm()
        self.d.release_all()
        if setup_text:
            self.ui.say('')
            self.ui.say('>>> %s - setup: %s' % (phase, setup_text))
            self.ui.say('    Move the spider there in Webbed now (no need to come back here). The run starts by itself '
                        'once the spider is at rest and you have let go of mouse and keyboard for %.0f s.' % HANDS_OFF_S)
            self.ui.beep(2)
            t0 = self.d.user_idle_seconds()
            for _ in range(self.cfg.setup_window_frames):   # time to start moving; ends early once you have moved
                self.frame()
                idle = self.d.user_idle_seconds()
                if idle < t0 or idle < 1.0:                 # you started moving the spider
                    break
                t0 = idle
        if not self.d.foreground_ok():
            self.d.focus_game(timeout_s=5.0)
        reasons = self.wait_ready(need_support, timeout=PREPARE_TIMEOUT_FRAMES, hands_off=True, refocus=True,
                                  progress=True)
        if reasons:
            return reasons
        if gate:
            r = gate()
            if r:
                return r
        if not self.d.foreground_ok():                      # last chance, still unarmed
            if not self.d.focus_game():
                return ['could not bring Webbed to the front: click its TITLE BAR (not inside the game)']
            reasons = self.wait_ready(need_support, timeout=READY_TIMEOUT_FRAMES, hands_off=True)
            if reasons:
                return reasons
        self.arm(phase)
        self.ui.beep(1)
        return []

    def ensure_ready(self, need_support, purpose, timeout=READY_TIMEOUT_FRAMES):
        """Gate before inputs (prepare + raise): never continues on a failed check; arms on success."""
        self.ui.say('  getting ready (%s): bringing Webbed to the front, waiting until the spider is controllable, '
                    'unpaused and at rest...' % purpose)
        reasons = self.prepare(purpose, need_support)
        if reasons:
            raise ScenarioFailure('not_ready', '%s: %s' % (purpose, '; '.join(reasons)))

    # ---------------------------------------------------------------- initialisation
    def initialise(self, focus=True):
        diag = self.p.diagnose()
        for line in diag['report']:
            self.ui.say('  ' + line)
        if not diag['counter_advancing']:
            raise GameLost('the game frame counter is not advancing (game paused by Windows / loading?)')
        self.ui.say('  (attach diagnostics are a snapshot; the runner keeps refreshing the instance list)')
        if focus:
            self.focus()
        return diag

    def focus(self):
        """Bring the game to the foreground at a phase boundary (start, after a prompt). Never mid-scenario:
        a focus change during a scenario is treated as a user takeover and aborts. Nothing is held while
        focusing, and the emergency stop is polled throughout (driver.focus_game)."""
        self.d.disarm()
        self.d.release_all()
        if not self.d.focus_game():
            raise AbortRun('could not bring the Webbed window to the foreground; click its TITLE BAR (not inside '
                           'the window, which would fire a web) and run again')

    def arm(self, phase):
        """Takeover detection ON. Called only after focus, readiness (incl. unpaused / no menu) and setup are
        complete, right before the runner's own inputs. F12 / Pause work in every phase regardless."""
        self.d.arm()
        self.ui.say('     [takeover detection armed: %s; hands off - F12 / Pause stops]' % phase)

    def _load_state_profile(self):
        """Menu-open indicator measured by -StateScan (instance variable of an object), or None."""
        try:
            with open(self.cfg.state_profile_file) as f:
                prof = json.load(f)
        except (OSError, ValueError):
            return None
        return prof if prof.get('kind') == 'instance_var' and prof.get('open_values') else None

    def _load_aim_profile(self):
        """Verified aim profile from -AimScan: 'relative' (locked cursor, delta impulses) or absolute feedback."""
        try:
            with open(self.cfg.aim_profile_file) as f:
                prof = json.load(f)
        except (OSError, ValueError):
            return None
        return prof if (prof.get('mode') == 'relative' and prof.get('pair')) or prof.get('feedback_pair') else None

    def cursor_locked(self):
        """Does the game pull the cursor back to a fixed point (relative-mouse aiming)? One small move."""
        c0 = self.d.cursor_client()
        if c0 is None:
            return False
        w, h = self.d.client_size()
        self.d.move_mouse(min(w - 5, c0[0] + 60), c0[1])
        for _ in range(4):
            self.frame()
        c1 = self.d.cursor_client()
        return c1 is not None and abs(c1[0] - c0[0]) + abs(c1[1] - c0[1]) < 10

    # ---------------------------------------------------------------- calibration
    CAL_ORDER = ('right', 'left', 'jump', 'retract', 'weave', 'grapple')

    def calibrate(self, candidates, actions=None, base=None):
        """Find, per action, the first candidate key that oIO reacts to. Fires a web or two (grapple/weave).
        `actions`: subset to (re)calibrate; others keep their key from `base` (previously confirmed bindings).
        An action whose oIO variable does not exist is reported as such: no key can be confirmed through it.
        If the spider will not come to rest between actions, calibration stops and returns what it found."""
        self.ui.say('Calibrating bindings (watch the game: the spider will move, jump and shoot)...')
        base = base or B.Bindings({})
        actions = [a for a in self.CAL_ORDER if not actions or a in actions]
        keys = {a: base.key(a) for a in self.CAL_ORDER}
        cal = dict(base.calibrated)
        report = {}
        self.ensure_ready('any', 'calibration')
        used = {k.upper() for a, k in keys.items() if k and a not in actions}
        for i, action in enumerate(actions):
            if i:
                reasons = self.wait_ready('any')     # armed: the user should not touch anything now
                if reasons:
                    for rest in actions[i:]:
                        report[rest] = {'status': 'not attempted', 'reason': 'spider not at rest: ' + '; '.join(reasons)}
                    self.ui.say('  calibration stopped: the spider did not come to rest (%s)' % '; '.join(reasons))
                    break
            found, tried = None, []
            for key in candidates.get(action, []):
                if key.upper() in used:
                    continue
                ctx = Ctx(self.d, self.b)
                ctx.row = self.last_row
                res = {}
                self.drive(self._try_key(ctx, action, key, res), ctx, max_frames=200)
                tried.append(key.upper())
                self.ui.say('  %-8s %-6s %s' % (action, key, 'OK (latency %d frames)' % res['latency']
                                                if res.get('ok') else 'no reaction in oIO'))
                if res.get('ok'):
                    found = key.upper()
                    cal[action] = {'key': found, 'latency_frames': res['latency'], 'side_effects': res['also'],
                                   'confirmed': stamp()}
                    used.add(found)
                    break
            vars_needed = [v for v, _ in B.SIGNALS[action]['any']]
            present = [v for v in vars_needed if v in self.io_keys_seen]
            if found:
                keys[action] = found
                report[action] = {'status': 'confirmed', 'key': found, 'latency_frames': cal[action]['latency_frames']}
            else:
                keys[action] = None
                cal.pop(action, None)
                why = ('oIO has no variable %s: no key can be confirmed through it, so this action is probably not '
                       'an oIO input (see W4_retract_probe)' % '/'.join(vars_needed) if not present else
                       'oIO variable(s) %s exist but no candidate key changed them' % '/'.join(present))
                report[action] = {'status': 'unresolved', 'tried': tried, 'reason': why}
                self.ui.say('  %-8s UNRESOLVED: %s' % (action, why))
            ctx = Ctx(self.d, self.b)
            ctx.row = self.last_row
            self.drive(self._release_webs(ctx), ctx, max_frames=900)
        for a in self.CAL_ORDER:
            if a not in actions:
                report[a] = {'status': 'kept', 'key': keys[a]}
        missing_io = sorted(v for v in ('hm', 'jmp', 'jmp_held', 'grapple', 'weave', 'retract')
                            if v not in self.io_keys_seen)
        self.calibration = {'actions': report, 'oio_variables_seen': sorted(self.io_keys_seen),
                            'oio_variables_missing': missing_io}
        self.d.disarm()
        self.b = B.Bindings(keys, source='calibrated %s' % stamp(), calibrated=cal)
        return self.b

    def _try_key(self, ctx, action, key, res):
        self.d.press(key)
        n = yield from ctx.wait_until(lambda r: B.signal(S.io(r), action), ACK_FRAMES, kind=None)
        also = sorted(a for a in B.SIGNALS if a != action and B.signal(S.io(ctx.row), a))
        yield from ctx.wait(2)
        self.d.release(key)
        yield from ctx.wait_until(lambda r: not B.signal(S.io(r), action), 30, 'missed_input',
                                  '%s still active in oIO after releasing %s' % (action, key))
        res.update(ok=n is not None, latency=n, also=also)

    def _release_webs(self, ctx):
        """After calibration presses: let the spider settle; detach from a grapple if one caught."""
        yield from ctx.wait(10)
        if S.grapple_engaged(ctx.row) and self.b.key('grapple'):
            self.d.press(self.b.key('grapple'))
            yield from ctx.wait(3)
            self.d.release(self.b.key('grapple'))
        yield from ctx.settle(600)

    # ---------------------------------------------------------------- scenarios
    def run_scenario(self, sc, attempt):
        res = {'scenario': sc.id, 'suite': sc.suite, 'attempt': attempt, 'status': 'fail', 'failure': None,
               'started': datetime.datetime.now().isoformat(), 'mode': self.cfg.mode}
        missing = [a for a in sc.needs if not self.b.key(a)]
        if missing:
            res['status'] = 'skipped'
            res['failure'] = {'kind': 'binding_unresolved',
                              'msg': 'no confirmed key for %s; nothing was recorded (see -Calibrate%s)' % (
                                  missing, ' and W4_retract_probe' if 'retract' in missing else '')}
            return res
        gate_box = {}

        def gate():                     # setup validation BEFORE any input: is there enough walkable ground?
            g = self._ground_gate(sc, res) if sc.space else None
            if g:
                gate_box['res'] = g
                return [g['failure']['msg']]
            return []
        reasons = self.prepare(sc.id, sc.support, setup_text=getattr(sc, '_announce', None), gate=gate)
        sc._announce = None
        if gate_box.get('res'):
            return gate_box['res']
        if reasons:
            res['failure'] = {'kind': 'not_ready', 'msg': '; '.join(reasons)}
            if self.last_game_state:
                res['game_state'] = self.last_game_state
            return res
        if self.last_game_state:
            res['game_state'] = self.last_game_state
        aimer, aim_sources = None, None
        if sc.aim:
            prof = self.aim_profile
            if prof and prof.get('mode') == 'relative':
                aimer = RelativeAim(prof)
                res['aim_method'] = 'relative (%s via %s impulses)' % ('/'.join(prof['pair']), prof['method'])
            elif prof and prof.get('feedback_pair'):
                pair = prof['feedback_pair']
                (sx, vx), (sy, vy) = (pair[0].split('.', 1), pair[1].split('.', 1))
                aim_sources = [(sx, vx, vy)] if sx == sy else None
                res['aim_method'] = 'absolute feedback (%s)' % '/'.join(pair)
            elif self.cursor_locked():
                res['failure'] = {'kind': 'aim_failed', 'msg': 'the game locks the cursor (relative-mouse aiming) and '
                                  'no relative aim profile exists yet: run -AimScan first (nothing was fired)'}
                self.d.disarm()
                return res
            if aimer is None and not aim_sources:
                res['aim_method'] = 'sighting'
                sres = self._sight(res)
                if sres is None:
                    return res
                aimer = sres
                reasons = self.wait_ready(sc.support)
                if reasons:
                    res['failure'] = {'kind': 'not_ready', 'msg': 'after sighting: ' + '; '.join(reasons)}
                    self.d.disarm()
                    return res
        name = self._new_session_name(sc.id)
        path = os.path.join(self.cfg.capture_root, name)
        meta = self.p.meta()
        meta['auto'] = {'scenario': sc.id, 'attempt': attempt, 'bindings': self.b.to_json(), 'mode': self.cfg.mode}
        writer = SessionWriter(path, meta)
        self.created.add(path)
        res['session'] = name
        writer.start_segment()
        ctx = Ctx(self.d, self.b, log=writer.log)
        ctx.row = self.last_row
        ctx.aimer, ctx.aim_sources = aimer, aim_sources
        ctx.ground_profile = res.get('ground_profile')
        if getattr(aimer, 'sighting', None):
            writer.log(dict(event='sighting_used', P=aimer.P, **aimer.sighting))
        issues = []
        fatal = None
        t0 = time.time()
        try:
            self.drive(sc.fn(ctx), ctx, writer, sc.max_frames, issues)
            self.d.release_all()
            for _ in range(TAIL_FRAMES):
                ctx.observe(self.frame(writer, ctx.marker))
            res['status'] = 'pass'
        except ScenarioFailure as e:
            res['failure'] = {'kind': e.kind, 'msg': e.msg, 'frame': ctx.frame}
        except AbortRun as e:
            res['failure'] = {'kind': 'aborted', 'msg': str(e), 'frame': ctx.frame}
            fatal = e
        except GameLost as e:
            res['failure'] = {'kind': 'game_lost', 'msg': str(e), 'frame': ctx.frame}
            fatal = e
        finally:
            self.d.release_all()
            self.d.disarm()
            writer.stop_segment()
        res['duration_s'] = round(time.time() - t0, 2)
        res['frames'] = ctx.frames
        res['inputs'] = summarise_inputs(ctx)
        res['observations'] = ctx.obs
        res['transitions'] = [list(t) for t in ctx.transitions[:400]]
        res['consistency_issues'] = issues[:50]
        sess = self._quality(path)
        res['quality'] = sess
        if res['status'] == 'pass' and issues:
            res['status'], res['failure'] = 'fail', {'kind': 'inconsistent', 'msg': issues[0]}
        if res['status'] == 'pass' and not sess['usable']:
            res['status'], res['failure'] = 'fail', {'kind': 'capture_invalid', 'msg': sess['summary']}
        if sc.id.startswith('M2') and sess['usable']:
            res['observations']['walk_metrics'] = walk_metrics(path)
        if sc.space and res.get('ground_profile'):
            res['ground_scan_check'] = ground_scan_check(res['ground_profile'], path)
        writer.close(res)
        if res['status'] != 'pass':
            res['preserved_at'] = self._preserve_failed(path)
            writer.path = res['preserved_at']
            writer.write_result(res)
        else:
            res['path'] = path
        if fatal:
            self.results.append(res)
            raise fatal
        return res

    def _ground_gate(self, sc, res):
        """Setup safety gate before a walk. Returns a finished result (skipped) or None to go ahead.
        'estimate' is enforced; 'ambiguous' / 'unavailable' skip unless -TrustGround (diagnostic mode)."""
        prof = self.p.ground_profile(self.last_row)
        res['ground_profile'] = prof
        conf = prof.get('confidence')
        if conf == 'estimate':
            if 'either' in sc.space:
                best = max((prof.get(d) or {}).get('clear_px', 0) for d in ('right', 'left'))
                short = {} if best >= sc.space['either'] else {'either': (best, sc.space['either'], 'both sides')}
            else:
                short = {d: (prof[d]['clear_px'], need, prof[d].get('end')) for d, need in sc.space.items()
                         if prof.get(d) and prof[d]['clear_px'] < need}
            if short:
                res['status'] = 'skipped'
                res['failure'] = {'kind': 'insufficient_space', 'msg': 'nothing sent: estimated clear ground %s '
                                  '(have px, need px, end); move to a longer flat area, or check the estimate with '
                                  '-GroundScan' % short}
                return res
            return None
        if self.cfg.trust_ground:
            res['ground_gate'] = 'OVERRIDDEN by -TrustGround (diagnostic): scan %s: %s' % (conf, prof.get('reason'))
            self.ui.say('     DIAGNOSTIC MODE: ground scan %s (%s); walking anyway, leaving the ground stops the '
                        'scenario' % (conf, prof.get('reason')))
            return None
        res['status'] = 'skipped'
        res['failure'] = {'kind': 'ground_%s' % conf, 'msg': 'nothing sent: the ground scan is %s (%s). Run '
                          '-GroundScan here to see why, or -TrustGround to walk anyway (diagnostic mode)'
                          % (conf, prof.get('reason'))}
        return res

    def state_snapshot(self, frames=60):
        """READ-ONLY snapshot for -Check: is the simulation advancing (fps over `frames`), foreground window,
        player control state, menu instances with all their variables, every `paused` owner, physics-world bytes,
        and the runner's verdict (blocking reasons) from that evidence."""
        t0 = time.monotonic()
        rows = [self.frame() for _ in range(frames)]
        dt = time.monotonic() - t0
        row = rows[-1]
        b = S.body(row)
        moved = [S.speed_pxps(r) for r in rows if S.body(r)]
        st = self.p.game_state(refresh=True) if hasattr(self.p, 'game_state') else {}
        menus = []
        if hasattr(self.p, 'menu_instances'):
            for ptr, iid, obj in self.p.menu_instances():
                menus.append({'id': iid, 'obj': obj, 'vars': self.p.instance_vars(ptr)})
        return {'game_fps': round((rows[-1]['frame'] - rows[0]['frame']) / dt, 1) if dt > 0 else None,
                'webbed_in_front': bool(self.d.foreground_ok()),
                'player': {'present': bool(b), 'locked': S.pvars(row).get('locked'), 'support': S.support(row),
                           'max_speed_pxps': round(max(moved), 2) if moved else None,
                           'readiness_reasons': S.readiness(row, 'any')},
                'menus': menus, 'paused_owners': st.get('paused_owners'),
                'state_profile': self.state_profile,
                'blocking_reasons': self.pause_reasons(st) if st else [],
                'world_raw': self.p.world_raw() if hasattr(self.p, 'world_raw') else None}

    def state_scan(self, out_dir, phase_frames=300, lead_frames=300):
        """Diagnostic, READ-ONLY (no input, no focus change, never armed): find what really signals an open menu.
        Phases (beeps mark them): A menu closed, B open the pause menu (Esc), C close it again. Every frame: frame
        counter, foreground window, player presence / locked / speed; every 5th frame also: all variables of every
        menu instance, every instance's `paused` variable, raw physics-world bytes. Saves the indicator (if one is
        found) to game_state_profile.local.json, which readiness then uses."""
        from . import statescan as SS
        os.makedirs(out_dir, exist_ok=True)
        self.ui.say('State scan: switch to Webbed now and stay in gameplay. In %d s: ONE beep = open the pause menu '
                    '(Esc) and leave it open; TWO beeps = close it; THREE beeps = done.' % (lead_frames // 60))
        for _ in range(lead_frames):
            self.frame()
        samples = []
        t0 = time.monotonic()
        with open(os.path.join(out_dir, 'statescan.jsonl'), 'w') as f:
            for i, phase in enumerate(SS.PHASES):
                if i:
                    self.ui.beep(i)
                    self.ui.say('  phase %s' % phase)
                if hasattr(self.p, 'on_phase'):        # mock only: scripted "user" for the self-test
                    self.p.on_phase(phase)
                for n in range(phase_frames):
                    row = self.frame()
                    b = S.body(row)
                    smp = {'phase': phase, 'frame': row['frame'], 't': round(time.monotonic() - t0, 4),
                           'fg': bool(self.d.foreground_ok()),
                           'player': {'present': bool(b), 'locked': S.pvars(row).get('locked'),
                                      'speed': S.speed_pxps(row) if b else None}}
                    if n % 5 == 0:
                        smp['menus'] = {str(iid): {'obj': obj, 'vars': self.p.instance_vars(ptr)}
                                        for ptr, iid, obj in self.p.menu_instances()}
                        smp['paused'] = {'%s#%s' % (obj, iid): v for _, iid, obj, v in
                                         self.p.find_var_owners('paused', refresh=(n == 0 and i == 0))}
                        smp['world_raw'] = self.p.world_raw()
                    samples.append(smp)
                    f.write(json.dumps(smp, default=str) + '\n')
        self.ui.beep(3)
        an = SS.analyze_statescan(samples)
        with open(os.path.join(out_dir, 'statescan_report.json'), 'w') as f:
            json.dump(an, f, indent=1, default=str)
        if an.get('indicator'):
            prof = dict(an['indicator'], measured=stamp(), note='from -StateScan; readiness waits while it shows open')
            if os.path.exists(self.cfg.state_profile_file):
                os.replace(self.cfg.state_profile_file, self.cfg.state_profile_file + '.%s.bak' % stamp())
            with open(self.cfg.state_profile_file, 'w') as f:
                json.dump(prof, f, indent=1)
            self.state_profile = prof
        return an

    def ground_scan(self, compare_session=None):
        """Diagnostic, sends nothing: the ground model around the spider, the corridor solids, and optionally a
        comparison with a capture made at the same spot (where the spider really stood)."""
        reasons = self.wait_ready(None, timeout=120)
        row = self.last_row
        if not S.body(row):
            return {'error': 'no player body: ' + '; '.join(reasons)}
        solids = self.p.ground_solids()
        out = {'profile': G.ground_profile(solids, S.body(row)['pos_px'], S.pvars(row).get('ground')),
               'corridor': G.corridor_table(solids, S.body(row)['pos_px'])}
        if compare_session:
            sys.path.insert(0, os.path.join(HERE, '..', '..', '..', 'tests'))
            import capture_lib as cap
            sess = cap.Session(compare_session)
            out['compare'] = {'session': sess.name, 'rooms': sorted({f.get('room') for f in sess.frames}),
                              'room_now': row.get('room'),
                              'instances': G.compare_with_capture(solids, sess.frames)}
        return out

    def _sight(self, res):
        """Pre-recording sighting shots (unrecorded; logged in the result). Returns a SightedAim or None."""
        self.ui.say('     sighting: %d short grapple taps into the floor to locate the aim origin on screen'
                    % len(SIGHT_ANGLES))
        ctx = Ctx(self.d, self.b)
        ctx.row = self.last_row
        box = {}

        def gen():
            box['aimer'] = yield from sight(ctx)
        try:
            self.drive(gen(), ctx, max_frames=2400)
        except ScenarioFailure as e:
            self.d.release_all()
            self.d.disarm()
            res['failure'] = {'kind': e.kind, 'msg': e.msg}
            res['sighting'] = (ctx.obs.get('sighting') or [None])[-1]
            return None
        finally:
            self.d.release_all()
        res['sighting'] = box['aimer'].sighting
        self.ui.say('     sighting OK: aim origin at client %s (ray rms %.2f px)' % (
            [round(v) for v in box['aimer'].P], box['aimer'].sighting['fit']['rms_px']))
        return box['aimer']

    # ---------------------------------------------------------------- aim diagnostics
    def aim_scan(self, out_dir, hold=12):
        """Diagnostic, fires nothing: move the cursor over a grid (absolute) and in small steps (relative); at the
        end of every hold record the requested and actual cursor, Windows' cursor state and EVERY numeric instance
        variable of oIO and oPlayer. Writes aimscan.jsonl + aimscan_report.json and returns the analysis."""
        self.ensure_ready('any', 'aim scan (cursor moves only)')
        w, h = self.d.client_size()
        # small symmetric grid around the current cursor: classifies the cursor (free / locked) and finds
        # absolute followers, while summing to zero motion if the game reads it as deltas (locked cursor)
        c0 = self.d.cursor_client() or (w / 2.0, h / 2.0)
        c0 = (max(60, min(w - 60, c0[0])), max(60, min(h - 60, c0[1])))
        pts = [(c0[0] + dx, c0[1] + dy) for dy in (-40, 0, 40) for dx in (-40, 0, 40)]
        rel = [(40, 0), (0, 40), (-40, 0), (0, -40)] * 2
        os.makedirs(out_dir, exist_ok=True)
        samples = []
        with open(os.path.join(out_dir, 'aimscan.jsonl'), 'w') as f:
            def record(req, phase):
                for _ in range(hold):
                    row = self.frame()
                sample = {'phase': phase, 'req': req, 'cur': self.d.cursor_client(), 'frame': row['frame'],
                          'body': (S.body(row) or {}).get('pos_px'),
                          'vars': dict([('player.' + k, v) for k, v in self.p.all_vars('player').items()] +
                                       [('io.' + k, v) for k, v in self.p.all_vars('io').items()])}
                samples.append(sample)
                f.write(json.dumps(dict(sample, cursor_state=self.d.cursor_state()), default=str) + '\n')
            for p in pts:
                self.d.move_mouse(*p)
                record(list(p), 'absolute')
            self.d.move_mouse(*c0)
            record(list(c0), 'absolute')
            for dx, dy in rel:
                self.d.move_mouse_relative(dx, dy)
                record(None, 'relative')
            # delta impulses: one event, then every frame recorded (gain, integration, lag)
            impulses = []
            deltas = [(20, 0), (-20, 0), (60, 0), (-60, 0), (0, 20), (0, -20), (0, 60), (0, -60)]
            for method in ('abs', 'rel'):
                for dx, dy in deltas:
                    for _ in range(4):
                        self.frame()
                    series = [self._aim_vars()]
                    c = self.d.cursor_client() or (w / 2.0, h / 2.0)
                    if method == 'abs':
                        self.d.move_mouse(c[0] + dx, c[1] + dy)
                    else:
                        self.d.move_mouse_relative(dx, dy)
                    for _ in range(8):
                        self.frame()
                        series.append(self._aim_vars())
                    impulses.append({'method': method, 'd': (dx, dy), 'series': series,
                                     'body': (S.body(self.last_row) or {}).get('pos_px')})
                    f.write(json.dumps({'phase': 'impulse', 'method': method, 'd': [dx, dy], 'series': series},
                                       default=str) + '\n')
        analysis = A.analyze_aimscan(samples)
        resp = A.analyze_impulses(impulses)
        rel_checks = A.relations(impulses[-1]['series'][-1], impulses[-1]['body']) if impulses else {}
        relative = A.choose_relative_profile(resp, rel_checks)
        analysis['impulse_response'] = {m: {k: v for k, v in r.items() if k != 'rejected'} for m, r in resp.items()
                                        if m != 'rejected'}
        analysis['impulse_rejected_pairs'] = resp.get('rejected')
        analysis['aimdir_relations'] = rel_checks
        analysis['relative_profile'] = relative
        analysis['cursor_state_at_end'] = self.d.cursor_state()
        analysis['hooks'] = getattr(self.d, 'hook_ok', None)
        analysis['takeover_monitor_counts'] = getattr(getattr(self.d, 'monitor', None), 'counts', None)
        with open(os.path.join(out_dir, 'aimscan_report.json'), 'w') as f:
            json.dump(analysis, f, indent=1, default=str)
        prof = None
        if relative and analysis.get('cursor', {}).get('mode') == 'recentred_or_locked':
            prof = dict(relative, measured=stamp(), lock_point=analysis['cursor'].get('fixed_point'))
            analysis['recommendation'] = ('relative aim: %s via %s impulses (gain %s, kind %s%s); saved as the aim '
                                          'profile; shots are still direction-checked'
                                          % ('/'.join(prof['pair']), prof['method'], prof['gain'], prof['kind'],
                                             ', verified against io.aimdir' if prof['kind_verified_by_aimdir'] else ''))
        elif analysis.get('feedback_pair'):
            prof = {'feedback_pair': analysis['feedback_pair'], 'measured': stamp(),
                    'followers': analysis['followers'][:6]}
        elif analysis.get('cursor', {}).get('mode') == 'recentred_or_locked':
            analysis['recommendation'] = ('the cursor is locked but no variable pair responded linearly to delta '
                                          'impulses: see impulse_response / impulse_rejected_pairs in the report')
        if prof:
            prof['note'] = 'from -AimScan; every real shot is still direction-checked'
            if os.path.exists(self.cfg.aim_profile_file):
                os.replace(self.cfg.aim_profile_file, self.cfg.aim_profile_file + '.%s.bak' % stamp())
            with open(self.cfg.aim_profile_file, 'w') as f:
                json.dump(prof, f, indent=1)
            self.aim_profile = prof
        self.d.disarm()
        return analysis

    def _aim_vars(self):
        return dict([('player.' + k, v) for k, v in self.p.all_vars('player').items()] +
                    [('io.' + k, v) for k, v in self.p.all_vars('io').items()])

    def aim_sight(self):
        """Diagnostic: only the sighting (3 grapple taps into the floor). Returns the sighting result."""
        self.ensure_ready('ground', 'aim sighting')
        if self.cursor_locked():
            self.d.disarm()
            return {'ok': False, 'P': None, 'sighting': None, 'failure': {
                'kind': 'aim_failed', 'msg': 'the game locks the cursor: sighting needs absolute aiming; use the '
                                             'relative aim profile from -AimScan (nothing was fired)'}}
        res = {}
        aimer = self._sight(res)
        self.d.disarm()
        return {'ok': aimer is not None, 'P': aimer.P if aimer else None, 'sighting': res.get('sighting'),
                'failure': res.get('failure')}

    def _quality(self, path):
        sys.path.insert(0, os.path.join(HERE, '..', '..', '..', 'tests'))
        import capture_lib as cap
        try:
            s = cap.Session(path)
        except Exception as e:          # malformed files: preserve and report
            return {'usable': False, 'summary': 'capture could not be loaded: %r' % e}
        return {'usable': s.usable, 'summary': s.summary(), 'rows': len(s.frames), 'player_rows': s.player_frames,
                'coverage': round(s.coverage, 4), 'torn': s.torn, 'skipped': s.skipped, 'web_records': len(s.webs),
                'problems': s.problems}

    def _new_session_name(self, sid):
        """'<scenario>_<timestamp>' as record.ps1 names sessions; unique across captures and captures_failed."""
        base = '%s_%s' % (sid, stamp())
        name, n = base, 1
        while any(os.path.exists(os.path.join(r, name)) for r in (self.cfg.capture_root, self.cfg.failed_root)):
            n += 1
            name = '%s-%d' % (base, n)
        return name

    def _preserve_failed(self, path):
        """Move a failed session created by THIS run out of the capture root (never deletes anything)."""
        if path not in self.created:
            raise RuntimeError('refusing to move a session this run did not create: %s' % path)
        os.makedirs(self.cfg.failed_root, exist_ok=True)
        dst = os.path.join(self.cfg.failed_root, os.path.basename(path))
        if os.path.exists(dst):
            raise RuntimeError('failed-recording destination already exists: %s' % dst)
        shutil.move(path, dst)
        return dst

    def run_suites(self, suites, only=None):
        stop = False
        for suite in suites:
            ids = [i for i in SUITES[suite] if not only or i in only]
            if not ids or stop:
                continue
            last_setup = None
            for sid in ids:
                sc = SCENARIOS[sid]
                if sc.setup != last_setup and 'Same spot' not in sc.setup:
                    self.d.disarm()            # the user is about to use mouse and keyboard
                    if self.cfg.prompt:        # optional console prompt (-Prompt); default: no Alt+Tab needed
                        self.ui.say('')
                        self.ui.say('[%s] %s  setup: %s' % (suite, sid, sc.setup))
                        ans = self.ui.ask('Position the spider (standing still), then switch back here.')
                        if ans == 'q':
                            stop = True
                            break
                        if ans == 's':
                            self.results.append({'scenario': sid, 'suite': suite, 'status': 'skipped',
                                                 'failure': {'kind': 'skipped', 'msg': 'skipped by user'}})
                            continue
                    else:
                        sc._announce = sc.setup  # announced in prepare(); the user sets up in the game
                last_setup = sc.setup if 'Same spot' not in sc.setup else last_setup
                for attempt in range(1, self.cfg.retries + 2):
                    self.ui.say('  -> %s attempt %d ...' % (sid, attempt))
                    res = self.run_scenario(sc, attempt)
                    self.results.append(res)
                    self.ui.say('     %s%s' % (res['status'].upper(), '' if res['status'] == 'pass' else
                                               ' [%s] %s' % (res['failure']['kind'], res['failure']['msg'])))
                    if res.get('preserved_at'):
                        self.ui.say('     failed recording preserved: %s' % res['preserved_at'])
                    if res['status'] != 'fail' or res['failure']['kind'] not in RETRYABLE:
                        break
        return self.results


# ------------------------------------------------------------------------------------------ measurements
def summarise_inputs(ctx):
    lat = [a['latency_frames'] for a in ctx.acks]
    out = {'acks': len(lat), 'holds': ctx.holds}
    if lat:
        out['ack_latency_frames'] = {'min': min(lat), 'median': statistics.median(lat), 'max': max(lat),
                                     'histogram': {str(k): lat.count(k) for k in sorted(set(lat))}}
    if ctx.holds:
        dev = [h['observed_frames'] - h['requested_frames'] for h in ctx.holds]
        out['hold_deviation_frames'] = {'min': min(dev), 'max': max(dev)}
    return out


def ground_scan_check(profile, path):
    """After a walk: predicted clear distance vs. how far the spider really went on support (validates the
    ground scan over time). Distances in px from the start position."""
    sys.path.insert(0, os.path.join(HERE, '..', '..', '..', 'tests'))
    import capture_lib as cap
    try:
        s = cap.Session(path)
    except Exception as e:
        return {'error': repr(e)}
    fr = [r for r in s.frames if (r.get('player') or {}).get('body')]
    if not fr:
        return {'error': 'no player frames'}
    x0 = fr[0]['player']['body']['pos_px'][0]
    on = [r['player']['body']['pos_px'][0] - x0 for r in fr if cap.support(r) == 'ground']
    lost = next((r['player']['body']['pos_px'][0] - x0 for a, r in zip(fr, fr[1:])
                 if cap.support(a) == 'ground' and cap.support(r) is None), None)
    out = {'confidence': profile.get('confidence'), 'actual_max_right_px': round(max(on), 1) if on else None,
           'actual_max_left_px': round(-min(on), 1) if on else None, 'first_support_loss_at_px':
           None if lost is None else round(lost, 1)}
    for d in ('right', 'left'):
        if profile.get(d):
            out['predicted_%s_px' % d] = profile[d]['clear_px']
            out['predicted_%s_end' % d] = profile[d].get('end')
    return out


def walk_metrics(path):
    """M2 (measured, not asserted): per walking phase, plateau speed, frames to 90 % of plateau after the game
    saw the input, and frames to stop after the game saw the release. Speeds in px/step."""
    sys.path.insert(0, os.path.join(HERE, '..', '..', '..', 'tests'))
    import capture_lib as cap
    s = cap.Session(path)
    fr = [r for r in s.frames if r['player'] and r['player'].get('body') and r.get('io') is not None]
    phases, cur = [], None
    for i, r in enumerate(fr):
        hm = (r['io'] or {}).get('hm') or 0
        if hm and cur is None:
            cur = {'dir': 1 if hm > 0 else -1, 'start': i}
        elif not hm and cur is not None:
            cur['end'] = i
            phases.append(cur)
            cur = None
    out = []
    for ph in phases:
        vx = [abs(fr[k]['player']['body']['vel_pxps'][0]) / 60.0 for k in range(ph['start'], ph['end'])]
        if len(vx) < 30:
            continue
        plateau = statistics.median(vx[-60:])
        t90 = next((k for k, v in enumerate(vx) if v >= 0.9 * plateau), None)
        after = [abs(fr[k]['player']['body']['vel_pxps'][0]) / 60.0 for k in range(ph['end'], min(len(fr), ph['end'] + 180))]
        stop = next((k for k, v in enumerate(after) if v < 0.05), None)
        out.append({'direction': ph['dir'], 'held_frames': len(vx), 'plateau_px_per_step': round(plateau, 4),
                    'frames_to_90pct': t90, 'initial_dv_px_per_step2': round(vx[1] - vx[0], 4) if len(vx) > 1 else None,
                    'frames_to_stop_after_release': stop,
                    'ground_frames': sum(1 for k in range(ph['start'], ph['end']) if cap.support(fr[k]) == 'ground')})
    return out


# ------------------------------------------------------------------------------------------ tests + report
TESTS_TIMEOUT_S = 900


def run_tests(capture_root, out_json, pattern='test*.py', timeout=TESTS_TIMEOUT_S):
    env = dict(os.environ, DP_CAPTURES=capture_root)
    script = os.path.join(HERE, 'testrun.py')
    try:
        proc = subprocess.run([sys.executable, '-I', script, '--json', out_json, '--pattern', pattern], env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'counts': None, 'tests': [], 'error': 'regression tests timed out after %d s' % timeout,
                'exit_code': None}
    try:
        with open(out_json) as f:
            res = json.load(f)
    except (OSError, ValueError):
        res = {'counts': None, 'tests': [], 'error': (proc.stdout + proc.stderr)[-4000:]}
    res['exit_code'] = proc.returncode
    return res


def relevant(tests, suites):
    keys = [k for s in suites for k in RELEVANT_TESTS.get(s, [])]
    return [t for t in tests if any(k in t['id'] for k in keys)]


def verdict(results, run_info):
    """One honest line: what was recorded, never 'all passed' when nothing ran."""
    ok = sum(1 for r in results if r['status'] == 'pass')
    failed = sum(1 for r in results if r['status'] == 'fail')
    skipped = sum(1 for r in results if r['status'] == 'skipped')
    parts = []
    cal = run_info.get('calibration')
    if cal:
        acts = cal['actions']
        parts.append('calibration: %d confirmed/kept, %d unresolved (%s)' % (
            sum(1 for a in acts.values() if a['status'] in ('confirmed', 'kept') and a.get('key')),
            sum(1 for a in acts.values() if a['status'] in ('unresolved', 'not attempted') or not a.get('key')),
            ', '.join(sorted(k for k, a in acts.items() if not a.get('key'))) or 'none'))
    if run_info.get('aim_scan'):
        parts.append('aim scan: %s' % run_info['aim_scan'].get('recommendation'))
    if run_info.get('aim_sight'):
        r = run_info['aim_sight']
        parts.append('aim sighting %s' % ('OK, origin %s' % [round(v) for v in r['P']] if r['ok'] else
                                          'FAILED: %s' % (r.get('failure') or {}).get('msg')))
    if not results:
        parts.append('NO gameplay scenarios were run or recorded')
    elif ok == 0 and failed == 0:
        parts.append('NO gameplay scenarios were recorded (%d skipped)' % skipped)
    else:
        parts.append('%d scenario attempt(s): %d pass, %d fail, %d skipped' % (len(results), ok, failed, skipped))
    return '; '.join(parts)


def run_outcome(results, tests, run_info):
    """Exit code: 0 = >= 1 scenario recorded, none without a passing attempt, tests clean;
    1 = failures; 4 = nothing recorded (calibration / check only, or everything skipped) and nothing failed."""
    passed = {r['scenario'] for r in results if r['status'] == 'pass'}
    unpassed = {r['scenario'] for r in results if r['status'] == 'fail'} - passed
    tests_bad = bool(tests) and not tests.get('pending') and (not tests.get('counts') or tests['counts']['fail']
                                                              or tests['counts']['error'])
    if unpassed or tests_bad:
        return 1, sorted(unpassed)
    return (0 if passed else 4), []


def write_report(cfg, run_info, results, tests, suites):
    d = os.path.join(cfg.runs_root, run_info['stamp'])
    os.makedirs(d, exist_ok=True)
    verdict_parts = [verdict(results, run_info)]
    if tests and tests.get('pending'):
        verdict_parts.append('regression tests: running')
    elif tests and tests.get('counts'):
        verdict_parts.append('tests: %(pass)d pass, %(fail)d fail, %(error)d error, %(skip)d skip' % tests['counts'])
    elif tests and tests.get('error'):
        verdict_parts.append('regression tests not completed (%s)' % tests['error'][:120])
    report = dict(run_info, finished=datetime.datetime.now().isoformat(), suites=suites, capture_root=cfg.capture_root,
                  failed_root=cfg.failed_root, results=results, tests=tests,
                  relevant_tests=relevant(tests.get('tests', []), suites) if tests else [],
                  verdict='; '.join(verdict_parts))
    with open(os.path.join(d, 'report.json'), 'w') as f:
        json.dump(report, f, indent=1, default=str)
    with open(os.path.join(d, 'report.md'), 'w') as f:
        f.write(render_md(report))
    return d, report


def render_md(rep):
    L = ['# Automated reference run %s (%s)' % (rep['stamp'], rep['mode']), '',
         '**%s**' % rep['verdict'], '']
    if rep['mode'] != 'live':
        L += ['> MOCK run: these sessions come from the fake game in autoref/mock.py. They are NOT reference '
              'captures and validate only the runner itself.', '']
    if rep.get('init_error'):
        L += ['Initialisation failed: `%s`' % rep['init_error'], '']
    if rep.get('aborted'):
        L += ['Run aborted: `%s` (all inputs released)' % rep['aborted'], '']
    cal = rep.get('calibration')
    if cal:
        L += ['## Calibration', '', '| Action | Status | Key | Latency (frames) | Note |', '|---|---|---|---|---|']
        for a, c in cal['actions'].items():
            L.append('| %s | %s | %s | %s | %s |' % (a, c['status'], c.get('key') or '', c.get('latency_frames', ''),
                                                    (c.get('reason') or '').replace('|', '/')))
        L += ['', 'oIO variables present: `%s`  ' % ', '.join(cal['oio_variables_seen']),
              'expected but absent: `%s`' % (', '.join(cal['oio_variables_missing']) or 'none'), '',
              'Calibration confirms only that the game receives the keys; it validates no physics.', '']
    L += ['Capture root: `%s`  ' % rep['capture_root'], 'Failed recordings: `%s`' % rep['failed_root'], '',
          '| Scenario | Attempt | Status | Failure | Frames | Ack latency (frames) | Session |', '|---|---|---|---|---|---|---|']
    for r in rep['results']:
        lat = (r.get('inputs') or {}).get('ack_latency_frames')
        L.append('| %s | %s | %s | %s | %s | %s | %s |' % (
            r['scenario'], r.get('attempt', ''), r['status'].upper(),
            ('%s: %s' % (r['failure']['kind'], r['failure']['msg'])).replace('|', '/') if r.get('failure') else '',
            r.get('frames', ''), '%(min)s / %(median)s / %(max)s' % lat if lat else '',
            r.get('preserved_at') or r.get('path') or ''))
    for r in rep['results']:
        obs = r.get('observations')
        if obs:
            L += ['', '### %s (attempt %s) observations (measured, not asserted)' % (r['scenario'], r.get('attempt')),
                  '```', json.dumps(obs, indent=1, default=str)[:4000], '```']
    t = rep.get('tests')
    if t and t.get('pending'):
        L += ['', '## Regression tests', '', 'running (this report is updated when they finish)']
        t = None
    if t and t.get('counts'):
        L += ['', '## Regression tests', '', 'Totals: %(pass)d pass, %(fail)d fail, %(error)d error, %(skip)d skip' % t['counts'],
              '', '| Test | Outcome | Detail |', '|---|---|---|']
        for x in rep['relevant_tests'] + [x for x in t['tests'] if x['outcome'] in ('fail', 'error')
                                          and x not in rep['relevant_tests']]:
            det = (x.get('first_failures') or [x.get('detail') or ''])[0]
            L.append('| %s | %s | %s |' % (x['id'], x['outcome'].upper(),
                                          (det.strip().splitlines() or [''])[-1][:200].replace('|', '/')))
    elif t:
        L += ['', '## Regression tests', '', 'Test run failed to produce results:', '```', t.get('error', ''), '```']
    return '\n'.join(L) + '\n'
