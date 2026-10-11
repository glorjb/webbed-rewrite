"""Automated, state-aware reference recordings of the ORIGINAL Webbed (Windows; read-only probe + OS input).

    python tools/reference/auto_runner.py --all                     # M2, W1, W2, W3 suites, then the tests
    python tools/reference/auto_runner.py --suite M2 W1
    python tools/reference/auto_runner.py --scenario W1_attach_hang
    python tools/reference/auto_runner.py --check                   # attach + readiness + bindings, no inputs
    python tools/reference/auto_runner.py --calibrate               # find the key for each action via oIO
    python tools/reference/auto_runner.py --calibrate retract       # only those actions; others are kept
    python tools/reference/auto_runner.py --list                    # suites, scenarios and the spot each needs
    python tools/reference/auto_runner.py --aim-scan                # cursor sweep, NO shots: what follows the mouse?
    python tools/reference/auto_runner.py --aim-sight               # 3 grapple taps into the floor: aim origin
    python tools/reference/auto_runner.py --ground-scan [--compare-capture DIR]   # ground model, sends nothing
    python tools/reference/auto_runner.py --mock --all              # runner self-test against a FAKE game

Launcher: powershell -ExecutionPolicy Bypass -File tools\\reference\\auto_record.ps1 -All
Emergency stop: F12 or Pause/Break (also: Ctrl+C, or move the mouse / switch window during a run).
See docs/REFERENCE_TEST_PROCEDURE.md, section "Automated recording".
"""
import argparse
import datetime
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                      # needed under `python -I`

from autoref import bindings as B             # noqa: E402
from autoref import runner as RN              # noqa: E402
from autoref.driver import AbortRun, Watchdog  # noqa: E402
from autoref.provider import GameLost         # noqa: E402
from autoref.scenarios import SCENARIOS, SUITE_ORDER, SUITES, ScenarioFailure  # noqa: E402

DEFAULT_ROOT = os.environ.get('DP_CAPTURES', r'C:\Users\profe\DesktopPlaygroundLocal\captures')


def parse(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--all', action='store_true', help='run every suite (%s)' % ', '.join(SUITE_ORDER))
    ap.add_argument('--suite', nargs='+', choices=SUITE_ORDER, default=[])
    ap.add_argument('--scenario', nargs='+', choices=sorted(SCENARIOS), default=[])
    ap.add_argument('--check', action='store_true', help='attach, report readiness and bindings, send no input')
    ap.add_argument('--calibrate', nargs='*', choices=list(RN.Runner.CAL_ORDER), default=None, metavar='ACTION',
                    help='(re)calibrate bindings against oIO: all actions, or only the ones named (%s); confirmed '
                         'keys of other actions are kept and the previous file is backed up'
                         % ', '.join(RN.Runner.CAL_ORDER))
    ap.add_argument('--list', action='store_true', help='list suites and scenarios, then exit')
    ap.add_argument('--aim-scan', action='store_true',
                    help='diagnostic, fires nothing: sweep the cursor and record every oIO / oPlayer variable and the '
                         'actual cursor position; reports which variables follow the mouse and whether the game moves '
                         'the cursor')
    ap.add_argument('--ground-scan', action='store_true',
                    help='diagnostic, sends nothing: the ground model around the spider and the solids near it')
    ap.add_argument('--compare-capture', metavar='SESSION_DIR',
                    help='with --ground-scan: compare the model with where the spider really stood in that capture '
                         '(made in the same room)')
    ap.add_argument('--trust-ground', action='store_true',
                    help='DIAGNOSTIC MODE: walk although the ground scan is ambiguous / unavailable (leaving the ground '
                         'still stops the scenario; the result is marked OVERRIDDEN)')
    ap.add_argument('--aim-sight', action='store_true',
                    help='diagnostic: 3 short grapple taps into the floor to measure where the aim origin is on screen')
    ap.add_argument('--no-calibrate', action='store_true', help='never calibrate automatically')
    ap.add_argument('--capture-root', default=None)
    ap.add_argument('--retries', type=int, default=1, help='extra attempts for transient failures (default 1)')
    ap.add_argument('--no-prompt', action='store_true', help='do not pause for manual positioning between suites')
    ap.add_argument('--no-tests', action='store_true', help='skip the regression test run')
    ap.add_argument('--no-mouse-failsafe', action='store_true',
                    help='disable user-takeover detection (physical mouse/keyboard input while the runner owns '
                         'the game). F12 / Pause, focus-loss and the watchdog stay active. Not needed for cursor '
                         'warping by the game, which is not counted as a takeover')
    ap.add_argument('--mock', action='store_true', help='use the FAKE game (runner self-test, no Windows needed)')
    ap.add_argument('--mock-fault', action='append', default=[],
                    help='mock fault injection, e.g. drop_keys=LMB, torn_every=5, io_latency=3, freeze_at=200')
    return ap.parse_args(argv)


GAME_FAULTS = {'drop_keys', 'release_mode', 'io_latency', 'torn_every', 'skip_every', 'no_player_frames',
               'freeze_at', 'respawn_at', 'io_absent', 'weave_reels_in', 'jitter_pxps', 'cursor_recenters',
               'crosshair_static', 'mouse_relative', 'gh_reset_on_attach', 'scan_unavailable', 'floor_attaches',
               'camera_offset', 'ground_x'}
DRIVER_FAULTS = {'emergency_at', 'focus_lost_at'}


def mock_kwargs(faults):
    """--mock-fault name=value -> (MockGame kwargs, MockInputDriver attributes)."""
    game, drv = {}, {}
    for f in faults:
        k, _, v = f.partition('=')
        if k in ('drop_keys', 'io_absent'):
            game[k] = tuple(v.split(','))
        elif k == 'jitter_pxps':
            game[k] = float(v)
        elif k in ('camera_offset', 'ground_x'):
            game[k] = tuple(float(x) for x in v.split(','))
        elif k == 'release_mode':
            game[k] = v
        elif k in GAME_FAULTS:
            game[k] = int(v)
        elif k in DRIVER_FAULTS:
            drv[k] = int(v)
        else:
            raise SystemExit('unknown --mock-fault %r; known: %s' % (k, ', '.join(sorted(GAME_FAULTS | DRIVER_FAULTS))))
    return game, drv


def list_scenarios():
    for suite in SUITE_ORDER:
        print('suite %s' % suite)
        for sid in SUITES[suite]:
            sc = SCENARIOS[sid]
            print('  %-18s needs keys: %-28s spot: %s' % (sid, ', '.join(sc.needs), sc.setup))


def print_ground_scan(gs):
    if gs.get('error'):
        print('ground scan: %s' % gs['error'])
        return
    p = gs['profile']
    print('ground scan: confidence %s%s' % (p.get('confidence'), (' (%s)' % p['reason']) if p.get('reason') else ''))
    print('  standing on %s (%s) at %s, foot y %s; %d solids (%d with unknown shape)' % (
        p.get('ground_id'), p.get('ground_obj'), p.get('player'), p.get('foot_y'), p.get('solids', 0),
        p.get('inexact_solids', 0)))
    for d in ('right', 'left'):
        if p.get(d):
            q = p[d]
            print('  %-5s clear %7.1f px, ends: %s %s; pieces %s; unknown shapes nearby %s' % (
                d, q['clear_px'], q['end'], q.get('by') or q.get('level_change_px') or '',
                [x[0] for x in q['pieces']][:12], q['unknown_solids'][:6]))
    print('  solids near the spider (id obj exact shape sprite x y xscale yscale angle box surface@player):')
    for r in gs['corridor'][:40]:
        print('    %-7s %-18s %-5s %-4s %-12s %8.1f %8.1f %7.3f %7.3f %7.2f %s %s' % (
            r['id'], r['obj'], r['exact'], r['shape'], r['sprite'], r['x'], r['y'], r['xscale'], r['yscale'],
            r['angle'], r['box'], r['surface_at_player_x']))
    if gs.get('compare'):
        c = gs['compare']
        print('  compared with %s (rooms %s, now %s):' % (c['session'], c['rooms'], c['room_now']))
        for r in c['instances']:
            print('    %-7s %-18s frames %-5d x %s  %s' % (r['id'], r['obj'], r['frames'], r['x_range'], r['verdict']))


def main(argv=None):
    a = parse(argv)
    if a.list:
        list_scenarios()
        return 0
    calibrating = a.calibrate is not None
    suites = SUITE_ORDER if a.all else list(a.suite)
    only = set(a.scenario) or None
    if only:
        suites = suites or [s for s in SUITE_ORDER if any(i in only for i in SUITES[s])]
    diagnostic = a.aim_scan or a.aim_sight or a.ground_scan
    if not (suites or a.check or calibrating or diagnostic):
        print('nothing to do: pass --all, --suite, --scenario, --check or --calibrate')
        return 2
    if a.mock:
        root = a.capture_root or os.path.join(tempfile.mkdtemp(prefix='dp_mock_'), 'captures')
        if os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.abspath(DEFAULT_ROOT)) \
                or 'mock' not in root.lower():
            print('refusing: mock sessions must not go into a real capture root (path must contain "mock")')
            return 2
    else:
        root = a.capture_root or DEFAULT_ROOT
    os.makedirs(root, exist_ok=True)
    # mock sessions are fake: never run the physics regression suite against them
    # and calibration / check alone records nothing, so there is nothing new for the tests to judge
    cfg = RN.Config(root, retries=a.retries, prompt=not a.no_prompt and not a.mock,
                    run_tests=bool(suites) and not (a.no_tests or a.mock),
                    mouse_failsafe=not a.no_mouse_failsafe, mode='mock' if a.mock else 'live',
                    trust_ground=a.trust_ground)
    run_info = {'stamp': RN.stamp(), 'mode': cfg.mode, 'started': datetime.datetime.now().isoformat(),
                'argv': sys.argv[1:] if argv is None else argv}
    print('Desktop Playground automated reference run %s (%s)' % (run_info['stamp'], cfg.mode))
    print('captures: %s   failed recordings: %s' % (cfg.capture_root, cfg.failed_root))
    if not a.mock:
        print('EMERGENCY STOP: F12 or Pause/Break. Switching window, or touching the mouse/keyboard during a scenario, also stops the run.')

    provider = driver = watchdog = runner = None
    results = []
    code = 0
    try:
        if a.mock:
            from autoref.mock import MockGame, MockInputDriver
            game_kw, drv_kw = mock_kwargs(a.mock_fault)
            provider = MockGame(**game_kw)
            driver = MockInputDriver(provider)
            for k, v in drv_kw.items():
                setattr(driver, k, v)
        else:
            from autoref.driver import WinInputDriver
            from autoref.provider import LiveProvider
            provider = LiveProvider()
            driver = WinInputDriver(provider.pid)
            watchdog = Watchdog(driver).start()
        if a.mock:
            bnd = B.Bindings(provider.keys, source='mock')
        elif os.path.exists(cfg.bindings_file):
            bnd = B.Bindings.load(cfg.bindings_file)            # confirmed keys are kept unless recalibrated
        else:
            bnd = B.Bindings.first_candidates(B.load_defaults())
        runner = RN.Runner(provider, driver, bnd, cfg, watchdog=watchdog)
        runner.guard.require_focus = not a.check
        print('Attaching (read-only)...')
        run_info['init'] = runner.initialise(focus=not a.check)['report']
        run_info['bindings_source'] = bnd.source
        if a.ground_scan:
            gs = runner.ground_scan(a.compare_capture)
            run_info['ground_scan'] = gs
            out = os.path.join(cfg.runs_root, run_info['stamp'])
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, 'ground_scan.json'), 'w') as f:
                json.dump(gs, f, indent=1, default=str)
            print_ground_scan(gs)
            print('details: %s' % os.path.join(out, 'ground_scan.json'))
            if not (suites or calibrating or a.aim_scan or a.aim_sight or a.check):
                cfg.run_tests = False
        if a.check:
            reasons = runner.wait_ready('any')
            print('readiness: %s' % ('READY' if not reasons else 'NOT READY: ' + '; '.join(reasons)))
            print('bindings (%s): %s' % (bnd.source, bnd.keys))
            code = 0 if not reasons else 2
        else:
            runner.ensure_ready('any', 'run start')          # gate: a failed check stops here, never continues
            print('readiness: READY')
            needs_cal = calibrating or (not a.mock and not a.no_calibrate and not os.path.exists(cfg.bindings_file))
            if needs_cal:
                bnd = runner.calibrate(B.load_defaults(), actions=a.calibrate or None, base=bnd)
                runner.b = bnd
                run_info['calibration'] = runner.calibration
                if not a.mock:
                    backup = bnd.save(cfg.bindings_file)
                    print('bindings saved to %s: %s' % (cfg.bindings_file, bnd.keys))
                    if backup:
                        print('previous bindings kept in %s' % backup)
                missing = [k for k, v in bnd.keys.items() if not v]
                if missing:
                    print('NOTE: no confirmed key for %s; scenarios needing them are SKIPPED (nothing recorded)'
                          % missing)
                print('Calibration only confirms that the game receives the keys; it validates no physics.')
            run_info['bindings'] = bnd.to_json()
            if a.aim_scan:
                out = os.path.join(cfg.runs_root, run_info['stamp'], 'aimscan')
                print('Aim scan: moving the cursor over the game window (no clicks, no keys) ...')
                an = runner.aim_scan(out)
                run_info['aim_scan'] = {k: an.get(k) for k in ('cursor', 'followers', 'feedback_pair', 'recommendation',
                                                               'variables_that_changed',
                                                               'variables_that_changed_with_relative_motion',
                                                               'cursor_state_at_end', 'hooks',
                                                               'takeover_monitor_counts')}
                print('cursor: %s' % an.get('cursor'))
                print('variables following the cursor: %s' % ([(f['var'], f['r2']) for f in an['followers'][:6]] or 'none'))
                print('changed with relative motion: %s' % (an['variables_that_changed_with_relative_motion'][:12] or 'none'))
                print(an['recommendation'])
                print('details: %s' % out)
            if a.aim_sight:
                print('Aim sighting: 3 short grapple taps into the floor ...')
                r = runner.aim_sight()
                run_info['aim_sight'] = r
                print('sighting %s: %s' % ('OK' if r['ok'] else 'FAILED', r['P'] if r['ok'] else r['failure']))
            if suites:
                runner.run_suites(suites, only)
    except (GameLost, RuntimeError) as e:
        run_info['init_error'] = str(e)
        print('INITIALISATION FAILED: %s' % e)
        code = 2
    except AbortRun as e:
        run_info['aborted'] = str(e)
        print('RUN ABORTED: %s (all inputs released)' % e)
        code = 3
    except ScenarioFailure as e:
        run_info['init_error'] = str(e)
        print('FAILED: %s' % e)
        code = 2
    except KeyboardInterrupt:
        run_info['aborted'] = 'Ctrl+C'
        print('RUN ABORTED by Ctrl+C (all inputs released)')
        code = 3
    finally:
        if driver:
            driver.release_all()
        if watchdog:
            watchdog.stop()
        if provider:
            provider.close()
    results = runner.results if runner else []
    tests = None
    if cfg.run_tests:
        print('Running regression tests against %s ...' % cfg.capture_root)
        os.makedirs(cfg.runs_root, exist_ok=True)
        tests = RN.run_tests(cfg.capture_root, os.path.join(cfg.runs_root, 'tests_%s.json' % run_info['stamp']))
    if a.check:
        return code
    d, rep = RN.write_report(cfg, run_info, results, tests, suites)
    print('')
    print(rep['verdict'])
    outcome, unpassed = RN.run_outcome(results, tests, run_info)
    if unpassed:
        print('scenarios without a passing attempt: %s' % ', '.join(unpassed))
    if tests and tests.get('counts'):
        for t in tests['tests']:
            if t['outcome'] in ('fail', 'error'):
                print('  %s %s' % (t['outcome'].upper(), t['id']))
    print('report: %s' % os.path.join(d, 'report.md'))
    return code or outcome


if __name__ == '__main__':
    sys.exit(main())
