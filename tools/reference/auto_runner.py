"""Automated, state-aware reference recordings of the ORIGINAL Webbed (Windows; read-only probe + OS input).

    python tools/reference/auto_runner.py --all                     # M2, W1, W2, W3 suites, then the tests
    python tools/reference/auto_runner.py --suite M2 W1
    python tools/reference/auto_runner.py --scenario W1_attach_hang
    python tools/reference/auto_runner.py --check                   # attach + readiness + bindings, no inputs
    python tools/reference/auto_runner.py --calibrate               # find the key for each action via oIO
    python tools/reference/auto_runner.py --mock --all              # runner self-test against a FAKE game

Launcher: powershell -ExecutionPolicy Bypass -File tools\\reference\\auto_record.ps1 -All
Emergency stop: F12 or Pause/Break (also: Ctrl+C, or move the mouse / switch window during a run).
See docs/REFERENCE_TEST_PROCEDURE.md, section "Automated recording".
"""
import argparse
import datetime
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
    ap.add_argument('--calibrate', action='store_true', help='(re)calibrate bindings against oIO')
    ap.add_argument('--no-calibrate', action='store_true', help='never calibrate automatically')
    ap.add_argument('--capture-root', default=None)
    ap.add_argument('--retries', type=int, default=1, help='extra attempts for transient failures (default 1)')
    ap.add_argument('--no-prompt', action='store_true', help='do not pause for manual positioning between suites')
    ap.add_argument('--no-tests', action='store_true', help='skip the regression test run')
    ap.add_argument('--no-mouse-failsafe', action='store_true',
                    help='do not abort when the cursor moves unexpectedly (use if the game warps the cursor)')
    ap.add_argument('--mock', action='store_true', help='use the FAKE game (runner self-test, no Windows needed)')
    ap.add_argument('--mock-fault', action='append', default=[],
                    help='mock fault injection, e.g. drop_keys=LMB, torn_every=5, io_latency=3, freeze_at=200')
    return ap.parse_args(argv)


GAME_FAULTS = {'drop_keys', 'release_mode', 'io_latency', 'torn_every', 'skip_every', 'no_player_frames',
               'freeze_at', 'respawn_at'}
DRIVER_FAULTS = {'emergency_at', 'focus_lost_at'}


def mock_kwargs(faults):
    """--mock-fault name=value -> (MockGame kwargs, MockInputDriver attributes)."""
    game, drv = {}, {}
    for f in faults:
        k, _, v = f.partition('=')
        if k == 'drop_keys':
            game[k] = tuple(v.split(','))
        elif k == 'release_mode':
            game[k] = v
        elif k in GAME_FAULTS:
            game[k] = int(v)
        elif k in DRIVER_FAULTS:
            drv[k] = int(v)
        else:
            raise SystemExit('unknown --mock-fault %r; known: %s' % (k, ', '.join(sorted(GAME_FAULTS | DRIVER_FAULTS))))
    return game, drv


def main(argv=None):
    a = parse(argv)
    suites = SUITE_ORDER if a.all else list(a.suite)
    only = set(a.scenario) or None
    if only:
        suites = suites or [s for s in SUITE_ORDER if any(i in only for i in SUITES[s])]
    if not (suites or a.check or a.calibrate):
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
    cfg = RN.Config(root, retries=a.retries, prompt=not a.no_prompt and not a.mock, run_tests=not (a.no_tests or a.mock),
                    mouse_failsafe=not a.no_mouse_failsafe, mode='mock' if a.mock else 'live')
    run_info = {'stamp': RN.stamp(), 'mode': cfg.mode, 'started': datetime.datetime.now().isoformat(),
                'argv': sys.argv[1:] if argv is None else argv}
    print('Desktop Playground automated reference run %s (%s)' % (run_info['stamp'], cfg.mode))
    print('captures: %s   failed recordings: %s' % (cfg.capture_root, cfg.failed_root))
    if not a.mock:
        print('EMERGENCY STOP: F12 or Pause/Break. Switching window or moving the mouse also stops the run.')

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
        elif os.path.exists(cfg.bindings_file) and not a.calibrate:
            bnd = B.Bindings.load(cfg.bindings_file)
        else:
            bnd = B.Bindings.first_candidates(B.load_defaults())
        runner = RN.Runner(provider, driver, bnd, cfg, watchdog=watchdog)
        runner.guard.require_focus = not a.check
        print('Attaching (read-only)...')
        run_info['init'] = runner.initialise(focus=not a.check)['report']
        run_info['bindings_source'] = bnd.source
        reasons = runner.wait_ready('any', timeout=180)
        print('readiness: %s' % ('READY' if not reasons else 'NOT READY: ' + '; '.join(reasons)))
        if a.check:
            print('bindings (%s): %s' % (bnd.source, bnd.keys))
            code = 0 if not reasons else 2
            cfg.run_tests = False
        else:
            needs_cal = a.calibrate or (not a.mock and not a.no_calibrate and not os.path.exists(cfg.bindings_file))
            if needs_cal:
                bnd = runner.calibrate(B.load_defaults())
                bnd.save(cfg.bindings_file)
                print('bindings saved to %s: %s' % (cfg.bindings_file, bnd.keys))
                missing = [k for k, v in bnd.keys.items() if not v]
                if missing:
                    print('WARNING: no working key found for %s; scenarios needing them will fail with binding_missing'
                          % missing)
            run_info['bindings'] = bnd.to_json()
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
    if cfg.run_tests and not a.check:
        print('Running regression tests against %s ...' % cfg.capture_root)
        os.makedirs(cfg.runs_root, exist_ok=True)
        tests = RN.run_tests(cfg.capture_root, os.path.join(cfg.runs_root, 'tests_%s.json' % run_info['stamp']))
    if a.check:
        return code
    d, rep = RN.write_report(cfg, run_info, results, tests, suites)
    print('')
    print(rep['verdict'])
    passed = {r['scenario'] for r in results if r['status'] == 'pass'}
    unpassed = sorted({r['scenario'] for r in results if r['status'] == 'fail'} - passed)
    if unpassed:
        print('scenarios without a passing attempt: %s' % ', '.join(unpassed))
    if tests and tests.get('counts'):
        for t in tests['tests']:
            if t['outcome'] in ('fail', 'error'):
                print('  %s %s' % (t['outcome'].upper(), t['id']))
    print('report: %s' % os.path.join(d, 'report.md'))
    if code == 0 and (unpassed or (tests and (not tests.get('counts') or tests['counts']['fail'] or tests['counts']['error']))):
        code = 1
    return code


if __name__ == '__main__':
    sys.exit(main())
