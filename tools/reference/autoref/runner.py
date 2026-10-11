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

from . import bindings as B
from . import state as S
from .capture import SessionWriter
from .driver import AbortRun, SafetyGuard
from .provider import GameLost
from .scenarios import ACK_FRAMES, SCENARIOS, SUITES, Ctx, ScenarioFailure

HERE = os.path.dirname(os.path.abspath(__file__))
RETRYABLE = {'missed_input', 'capture_invalid', 'inconsistent', 'timeout', 'aim_failed'}
TAIL_FRAMES = 30
READY_TIMEOUT_FRAMES = 1200          # 20 s to become controllable and settle
RELEVANT_TESTS = {
    'M2': ['test_walk_plateau_speed', 'test_free_fall_gravity', 'test_world_settings_every_frame',
           'test_every_capture_is_usable'],
    'W1': ['WebShot', 'test_free_fall_gravity', 'test_every_capture_is_usable'],
    'W2': ['WebShot', 'test_free_fall_gravity', 'WebGeometry', 'test_every_capture_is_usable'],
    'W3': ['WebGeometry', 'test_every_capture_is_usable'],
}


def stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


class Config:
    def __init__(self, capture_root, retries=1, prompt=True, run_tests=True, mouse_failsafe=True,
                 bindings_file=None, mode='live'):
        self.capture_root = capture_root
        parent = os.path.dirname(os.path.normpath(capture_root))
        self.failed_root = os.path.join(parent, 'captures_failed')
        self.runs_root = os.path.join(parent, 'auto_runs')
        self.bindings_file = bindings_file or os.path.join(parent, 'auto_bindings.local.json')
        self.retries, self.prompt, self.run_tests = retries, prompt, run_tests
        self.mouse_failsafe, self.mode = mouse_failsafe, mode


class ConsoleUI:
    def __init__(self, interactive=True):
        self.interactive = interactive

    def say(self, msg=''):
        print(msg, flush=True)

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

    # ---------------------------------------------------------------- frame loop
    def frame(self, writer=None, marker=0):
        rec = writer is not None and writer.recording
        row, focus = self.p.next_frame(writer.last_rec_frame if rec else None, marker, tick=self.guard.check)
        if rec:
            writer.write_row(row, focus)
        self.p.idle(writer)
        self.last_row = row
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

    def wait_ready(self, need_support, timeout=READY_TIMEOUT_FRAMES):
        """Wait until controllable and settled. Returns [] when ready, else the last reasons."""
        tr = S.SettleTracker()
        reasons = ['no frame']
        for _ in range(timeout):
            row = self.frame()
            reasons = S.readiness(row, need_support)
            tr.update(row)
            if not reasons and tr.settled:
                return []
        if not reasons and not tr.settled:
            reasons = ['the spider did not come to rest (|v| < %.1f px/s for %d frames)'
                       % (S.MOVE_MAX_PXPS, S.SETTLE_FRAMES)]
        return reasons

    # ---------------------------------------------------------------- initialisation
    def initialise(self, focus=True):
        diag = self.p.diagnose()
        for line in diag['report']:
            self.ui.say('  ' + line)
        if not diag['counter_advancing']:
            raise GameLost('the game frame counter is not advancing (game paused by Windows / loading?)')
        if focus and not self.d.focus_game():
            raise AbortRun('could not bring the Webbed window to the foreground')
        return diag

    # ---------------------------------------------------------------- calibration
    def calibrate(self, candidates):
        """Find, per action, the first candidate key that oIO reacts to. Fires a web or two (grapple/weave)."""
        self.ui.say('Calibrating bindings (watch the game: the spider will move, jump and shoot)...')
        reasons = self.wait_ready('any')
        if reasons:
            raise ScenarioFailure('not_ready', '; '.join(reasons))
        keys, cal, used = {}, {}, set()
        for action in ('right', 'left', 'jump', 'retract', 'weave', 'grapple'):
            keys[action] = None
            for key in candidates.get(action, []):
                if key.upper() in used:
                    continue
                ctx = Ctx(self.d, self.b)
                ctx.row = self.last_row
                res = {}
                self.drive(self._try_key(ctx, action, key, res), ctx, max_frames=200)
                self.ui.say('  %-8s %-6s %s' % (action, key, 'OK (latency %d frames)' % res['latency']
                                                if res.get('ok') else 'no reaction in oIO'))
                if res.get('ok'):
                    keys[action] = key.upper()
                    cal[action] = {'key': key.upper(), 'latency_frames': res['latency'], 'side_effects': res['also']}
                    used.add(key.upper())
                    break
            ctx = Ctx(self.d, self.b)
            ctx.row = self.last_row
            self.drive(self._release_webs(ctx), ctx, max_frames=900)
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
            res['failure'] = {'kind': 'binding_missing', 'msg': 'no key bound for %s (run -Calibrate)' % missing}
            return res
        reasons = self.wait_ready(sc.support)
        if reasons:
            res['failure'] = {'kind': 'not_ready', 'msg': '; '.join(reasons)}
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
                    self.d.mouse = None        # the user moved the mouse during setup: new takeover baseline
                    if not self.d.focus_game():
                        raise AbortRun('could not bring the Webbed window to the foreground')
                last_setup = sc.setup if 'Same spot' not in sc.setup else last_setup
                for attempt in range(1, self.cfg.retries + 2):
                    self.ui.say('  -> %s attempt %d ...' % (sid, attempt))
                    res = self.run_scenario(sc, attempt)
                    self.results.append(res)
                    self.ui.say('     %s%s' % (res['status'].upper(), '' if res['status'] == 'pass' else
                                               ' [%s] %s' % (res['failure']['kind'], res['failure']['msg'])))
                    if res.get('preserved_at'):
                        self.ui.say('     failed recording preserved: %s' % res['preserved_at'])
                    if res['status'] == 'pass' or res['failure']['kind'] not in RETRYABLE:
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
def run_tests(capture_root, out_json, pattern='test*.py'):
    env = dict(os.environ, DP_CAPTURES=capture_root)
    script = os.path.join(HERE, 'testrun.py')
    proc = subprocess.run([sys.executable, '-I', script, '--json', out_json, '--pattern', pattern], env=env,
                          capture_output=True, text=True)
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


def write_report(cfg, run_info, results, tests, suites):
    d = os.path.join(cfg.runs_root, run_info['stamp'])
    os.makedirs(d, exist_ok=True)
    ok = [r for r in results if r['status'] == 'pass']
    failed = [r for r in results if r['status'] == 'fail']
    verdict_parts = ['%d scenario attempt(s): %d pass, %d fail, %d skipped' % (
        len(results), len(ok), len(failed), sum(1 for r in results if r['status'] == 'skipped'))]
    if tests and tests.get('counts'):
        verdict_parts.append('tests: %(pass)d pass, %(fail)d fail, %(error)d error, %(skip)d skip' % tests['counts'])
    report = dict(run_info, finished=datetime.datetime.now().isoformat(), suites=suites, capture_root=cfg.capture_root,
                  failed_root=cfg.failed_root, results=results, tests=tests,
                  relevant_tests=relevant(tests['tests'], suites) if tests else [], verdict='; '.join(verdict_parts))
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
