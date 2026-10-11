"""Unit tests for the automated reference runner (tools/reference/autoref), using the MOCK game.

These test the runner's control flow, acknowledgement, quality gate, failure preservation and safety paths.
They say nothing about Webbed's physics: the mock's dynamics are deliberately not the recovered model, and no
mock output is ever written to the real capture root.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'tools', 'reference'))
import capture_lib as cap                                       # noqa: E402
from autoref import bindings as B                               # noqa: E402
from autoref import scenarios as SC                             # noqa: E402
from autoref import state as S                                  # noqa: E402
from autoref.capture import SessionWriter                       # noqa: E402
from autoref.driver import AbortRun, Watchdog                   # noqa: E402
from autoref.mock import MockGame, MockInputDriver              # noqa: E402
from autoref.provider import GameLost                           # noqa: E402
from autoref.runner import Config, ConsoleUI, Runner            # noqa: E402


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class QuietUI(ConsoleUI):
    def __init__(self):
        super().__init__(False)
        self.lines = []

    def say(self, msg=''):
        self.lines.append(msg)


class MockRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dp_mock_test_')
        self.root = os.path.join(self.tmp, 'mock', 'captures')
        os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, retries=1, keys=None, **kw):
        g = MockGame(**kw)
        d = MockInputDriver(g)
        cfg = Config(self.root, retries=retries, prompt=False, run_tests=False, mode='mock')
        r = Runner(g, d, B.Bindings(keys if keys is not None else g.keys, source='test'), cfg, ui=QuietUI())
        r.initialise()
        return g, d, r

    def run_ids(self, r, ids):
        suites = [s for s in SC.SUITE_ORDER if any(i in ids for i in SC.SUITES[s])]
        return r.run_suites(suites, set(ids))

    def sessions(self, root):
        return sorted(os.listdir(root)) if os.path.isdir(root) else []

    # ------------------------------------------------------------------ happy path
    def test_all_scenarios_pass_and_sessions_load_like_probe_captures(self):
        g, d, r = self.make()
        res = r.run_suites(SC.SUITE_ORDER)
        self.assertEqual([x['status'] for x in res], ['pass'] * len(SC.SCENARIOS), [x.get('failure') for x in res])
        self.assertEqual(self.sessions(r.cfg.failed_root), [])
        names = self.sessions(self.root)
        self.assertEqual(len(names), len(SC.SCENARIOS))
        for n in names:
            s = cap.Session(os.path.join(self.root, n))
            self.assertTrue(s.usable, s.summary())
            files = set(os.listdir(os.path.join(self.root, n)))
            self.assertEqual(files, {'segment_01.jsonl', 'segment_01_webs.jsonl', 'session.json', 'inputs.jsonl',
                                     'auto_run.json'})
            with open(os.path.join(self.root, n, 'session.json')) as f:
                meta = json.load(f)
            self.assertEqual(meta['segments'][0]['rows'], len(s.frames))
        self.assertEqual(d.held, set())

    def test_markers_and_acks_are_recorded(self):
        g, d, r = self.make(io_latency=3)
        res = self.run_ids(r, ['M2_walk_flat'])[0]
        s = cap.Session(os.path.join(self.root, res['session']))
        self.assertEqual(max(f['marker'] for f in s.frames), 2)
        lat = res['inputs']['ack_latency_frames']
        self.assertEqual((lat['min'], lat['max']), (4, 4))       # mock: press lands after frame n, latency 3
        self.assertEqual(len(res['inputs']['holds']), 2)
        for h in res['inputs']['holds']:
            self.assertEqual(h['requested_frames'], 240)
            self.assertEqual(h['observed_frames'], 240)
        wm = res['observations']['walk_metrics']
        self.assertEqual([p['direction'] for p in wm], [1, -1])

    # ------------------------------------------------------------------ failures are detected and preserved
    def assertPreservedFailure(self, r, res, kind):
        self.assertEqual(res['status'], 'fail')
        self.assertEqual(res['failure']['kind'], kind, res['failure'])
        self.assertTrue(os.path.isdir(res['preserved_at']), res)
        self.assertTrue(res['preserved_at'].startswith(r.cfg.failed_root))
        with open(os.path.join(res['preserved_at'], 'auto_run.json')) as f:
            saved = json.load(f)
        self.assertEqual(saved['failure']['kind'], kind)
        self.assertEqual(saved['preserved_at'], res['preserved_at'])

    def test_missed_input_is_retried_and_every_attempt_preserved(self):
        g, d, r = self.make(retries=1, drop_keys=('LMB',))
        res = self.run_ids(r, ['W1_attach_hang'])
        self.assertEqual(len(res), 2)
        for x in res:
            self.assertPreservedFailure(r, x, 'missed_input')
        self.assertEqual(len(self.sessions(r.cfg.failed_root)), 2)
        self.assertEqual(self.sessions(self.root), [])           # nothing unusable left where tests read
        self.assertEqual(d.held, set())

    def test_torn_frames_make_capture_invalid(self):
        g, d, r = self.make(retries=0, torn_every=5)              # 20 % torn > 5 % gate
        res = self.run_ids(r, ['M2_walk_flat'])[0]
        self.assertPreservedFailure(r, res, 'capture_invalid')
        self.assertIn('torn', res['failure']['msg'])

    def test_skipped_frames_make_capture_invalid(self):
        g, d, r = self.make(retries=0, skip_every=4)
        res = self.run_ids(r, ['M2_walk_flat'])[0]
        self.assertPreservedFailure(r, res, 'capture_invalid')

    def test_no_player_is_not_ready_and_records_nothing(self):
        g, d, r = self.make(retries=0, no_player_frames=10 ** 6)
        res = self.run_ids(r, ['M2_walk_flat'])[0]
        self.assertEqual(res['failure']['kind'], 'not_ready')
        self.assertIn('no oPlayer', res['failure']['msg'])
        self.assertEqual(self.sessions(self.root) + self.sessions(r.cfg.failed_root), [])

    def test_frozen_game_stops_the_run_and_preserves_the_recording(self):
        g, d, r = self.make(freeze_at=200)
        with self.assertRaises(GameLost):
            self.run_ids(r, ['M2_walk_flat', 'W1_attach_hang'])
        self.assertEqual(len(r.results), 1)
        self.assertPreservedFailure(r, r.results[0], 'game_lost')
        self.assertEqual(d.held, set())

    def test_respawn_mid_recording_is_inconsistent(self):
        g, d, r = self.make(retries=0, respawn_at=200)        # during the first walk
        res = self.run_ids(r, ['M2_walk_flat'])[0]
        self.assertPreservedFailure(r, res, 'inconsistent')

    def test_no_surface_is_setup_invalid_without_retry(self):
        g, d, r = self.make(retries=2, ceiling_y=None)
        res = self.run_ids(r, ['W1_attach_hang'])
        self.assertEqual(len(res), 1)
        self.assertPreservedFailure(r, res[0], 'setup_invalid')

    def test_walking_off_a_ledge_is_setup_invalid(self):
        g, d, r = self.make(retries=0, ground_x=(0, 600))
        res = self.run_ids(r, ['M2_walk_flat'])[0]
        self.assertPreservedFailure(r, res, 'setup_invalid')

    def test_missing_binding(self):
        g, d, r = self.make(keys={'right': 'D', 'left': 'A'})
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['failure']['kind'], 'binding_missing')

    def test_toggle_release_is_detected_and_recorded(self):
        g, d, r = self.make(release_mode='toggle')
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertEqual(res['observations']['release'][-1]['mode'], 'toggle')

    def test_state_transitions_logged_for_web_scenarios(self):
        g, d, r = self.make()
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        names = {(t[1], t[2]) for t in res['transitions']}
        self.assertIn(('player', 'attached'), names)
        self.assertIn(('io', 'grapple'), names)

    def test_aim_falls_back_to_io_aim_point(self):
        g, d, r = self.make()
        real_row = g._row

        def row_without_crosshair(*a):
            row, focus = real_row(*a)
            if row['player']:
                row['player']['vars']['crosshair_tx'] = row['player']['vars']['crosshair_ty'] = 0.0   # frozen
            return row, focus
        g._row = row_without_crosshair
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertEqual(res['observations']['aim'][0]['source'], 'io.aimx.aimy')

    def test_aim_fails_cleanly_without_feedback(self):
        g, d, r = self.make(retries=0)
        real_row = g._row

        def row_without_aim(*a):
            row, focus = real_row(*a)
            if row['player']:
                row['player']['vars'].pop('crosshair_tx')
            if row['io']:
                row['io'].pop('aimx', None)
            return row, focus
        g._row = row_without_aim
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertPreservedFailure(r, res, 'aim_failed')
        self.assertEqual(d.held, set())

    # ------------------------------------------------------------------ safety
    def test_observe_only_mode_does_not_require_focus(self):
        g, d, r = self.make()
        d.focus_lost_at = 0
        r.guard.require_focus = False
        self.assertEqual(r.wait_ready('any', timeout=100), [])
        r.guard.require_focus = True
        with self.assertRaises(AbortRun):
            r.wait_ready('any', timeout=100)

    def test_emergency_stop_aborts_releases_and_preserves(self):
        g, d, r = self.make()
        d.emergency_at = 150                                      # during the first walk
        with self.assertRaises(AbortRun):
            self.run_ids(r, ['M2_walk_flat'])
        self.assertEqual(d.held, set())
        self.assertPreservedFailure(r, r.results[0], 'aborted')
        self.assertIn('EMERGENCY', r.results[0]['failure']['msg'])

    def test_focus_loss_aborts(self):
        g, d, r = self.make()
        d.focus_lost_at = 150
        with self.assertRaises(AbortRun):
            self.run_ids(r, ['M2_walk_flat'])
        self.assertEqual(d.held, set())
        self.assertIn('focus', r.results[0]['failure']['msg'])

    def test_watchdog_releases_held_inputs_when_loop_stalls(self):
        g = MockGame()
        d = MockInputDriver(g)
        now = [0.0]
        wd = Watchdog(d, limit_s=0.5, clock=lambda: now[0])
        d.press('D')
        now[0] = 0.4
        self.assertIsNone(wd.check())
        self.assertEqual(d.held, {'D'})
        now[0] = 0.6
        self.assertIn('watchdog', wd.check())
        self.assertEqual(d.held, set())

    def test_existing_captures_are_never_touched(self):
        old = os.path.join(self.root, 'M3_jump_in_place_20261011-024349')
        os.makedirs(old)
        with open(os.path.join(old, 'segment_01.jsonl'), 'w') as f:
            f.write('{"not": "a valid row"}\n')
        digest = sha256(os.path.join(old, 'segment_01.jsonl'))
        g, d, r = self.make(retries=0, drop_keys=('D',))
        self.run_ids(r, ['M2_walk_flat'])
        self.assertTrue(os.path.isdir(old))
        self.assertEqual(sha256(os.path.join(old, 'segment_01.jsonl')), digest)
        with self.assertRaises(RuntimeError):
            r._preserve_failed(old)

    def test_session_writer_refuses_existing_directory(self):
        with self.assertRaises(FileExistsError):
            SessionWriter(self.root)

    # ------------------------------------------------------------------ calibration
    def test_calibration_finds_responsive_keys(self):
        keys = {'right': 'RIGHT', 'left': 'A', 'jump': 'W', 'grapple': 'LMB', 'weave': 'RMB', 'retract': 'CTRL'}
        g, d, r = self.make(keys={})
        g.keys = keys
        b = r.calibrate(B.load_defaults())
        self.assertEqual(b.keys, keys)
        self.assertEqual(d.held, set())

    def test_calibration_reports_unbindable_action(self):
        g, d, r = self.make(keys={})
        g.keys = {'right': 'D', 'left': 'A', 'jump': 'SPACE', 'grapple': 'LMB', 'weave': 'RMB', 'retract': 'F7'}
        b = r.calibrate(B.load_defaults())
        self.assertIsNone(b.keys['retract'])
        self.assertEqual(b.keys['right'], 'D')


class TestPipeline(unittest.TestCase):
    def test_run_tests_reports_per_test_outcomes(self):
        from autoref import runner as RN
        tmp = tempfile.mkdtemp(prefix='dp_mock_test_')
        try:
            out = RN.run_tests(os.path.join(tmp, 'empty'), os.path.join(tmp, 't.json'),
                               pattern='test_recovered_model.py')
            self.assertEqual(out['exit_code'], 0, out)
            self.assertEqual(out['counts']['fail'] + out['counts']['error'], 0)
            self.assertGreaterEqual(out['counts']['pass'], 15)
            self.assertTrue(all(t['outcome'] == 'pass' for t in out['tests']))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StateRules(unittest.TestCase):
    def row(self, **v):
        return {'frame': 1, 'world': {'speed': 60, 'iterations': 10, 'm_per_px': 0.03}, 'io': {'hm': 0},
                'player': {'id': 1, 'body': {'pos_px': [0, 0], 'vel_pxps': [0, 0]}, 'vars': dict(ground=5, **v)}}

    def test_readiness(self):
        self.assertEqual(S.readiness(self.row()), [])
        self.assertTrue(S.readiness(self.row(locked=1)))
        self.assertTrue(S.readiness(self.row(webshot_node=3)))
        r = self.row()
        r['player']['vars']['ground'] = -4
        self.assertTrue(S.readiness(r, 'ground'))
        self.assertTrue(S.readiness({'world': {'speed': 60}, 'player': None}))

    def test_consistency(self):
        a, b = self.row(), self.row()
        b['frame'] = 2
        self.assertEqual(S.consistency_issues(a, b), [])
        b['player']['id'] = 2
        self.assertTrue(any('respawn' in i for i in S.consistency_issues(a, b)))
        c = self.row()
        c['frame'] = 1
        self.assertTrue(any('counter' in i for i in S.consistency_issues(a, c)))

    def test_signals(self):
        self.assertTrue(B.signal({'hm': 1.0}, 'right'))
        self.assertFalse(B.signal({'hm': 1.0}, 'left'))
        self.assertTrue(B.signal({'jmp': 1}, 'jump'))
        self.assertFalse(B.signal({}, 'grapple'))
        self.assertFalse(B.signal({'grapple': '<kind6>'}, 'grapple'))


if __name__ == '__main__':
    unittest.main()
