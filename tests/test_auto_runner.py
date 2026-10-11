"""Unit tests for the automated reference runner (tools/reference/autoref), using the MOCK game.

These test the runner's control flow, acknowledgement, quality gate, failure preservation and safety paths.
They say nothing about Webbed's physics: the mock's dynamics are deliberately not the recovered model, and no
mock output is ever written to the real capture root.
"""
import hashlib
import json
import math
import os
import shutil
import subprocess
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
from autoref import aim as A                                    # noqa: E402
from autoref import groundscan as G                             # noqa: E402
from autoref import runner as RN                                # noqa: E402
from autoref.inputhook import TakeoverMonitor                   # noqa: E402

DIAG = os.path.join(HERE, '..', 'tools', 'reference', 'diagnose_capture.py')
WALK = os.path.join(HERE, '..', 'tools', 'reference', 'analyze_walk.py')
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

    def make(self, retries=1, keys=None, trust_ground=False, setup_window=0, **kw):
        g = MockGame(**kw)
        d = MockInputDriver(g)
        cfg = Config(self.root, retries=retries, prompt=False, run_tests=False, mode='mock', trust_ground=trust_ground)
        cfg.setup_window_frames = setup_window
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

    def test_unresolved_binding_is_skipped_and_records_nothing(self):
        g, d, r = self.make(keys={'right': 'D', 'left': 'A', 'grapple': 'LMB'})
        res = self.run_ids(r, ['W4_pull'])
        self.assertEqual([x['status'] for x in res], ['skipped'])          # no retry, not a failure
        self.assertEqual(res[0]['failure']['kind'], 'binding_unresolved')
        self.assertIn('retract', res[0]['failure']['msg'])
        self.assertEqual(self.sessions(self.root) + self.sessions(r.cfg.failed_root), [])
        self.assertEqual(RN.run_outcome(res, None, {}), (4, []))           # nothing recorded != all passed

    def test_retract_probe_identifies_the_key_that_changes_the_rope(self):
        g, d, r = self.make(io_absent=('retract',), weave_reels_in=True)
        res = self.run_ids(r, ['W4_retract_probe'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        trials = {t['key']: t for t in res['observations']['retract_probe'][0]['trials']}
        self.assertNotIn('LMB', trials)                                   # grapple key itself is never probed
        self.assertNotIn('SPACE', trials)
        self.assertLess(trials['RMB']['d_slope_minus_baseline'], -1.0)    # reels in
        self.assertIn('weave', trials['RMB']['oio_changed'])
        for k in ('SHIFT', 'S', 'CTRL'):
            self.assertEqual(trials[k]['oio_changed'], {})
            self.assertAlmostEqual(trials[k]['d_slope_minus_baseline'], 0.0, delta=0.2)
        self.assertEqual(d.held, set())

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

    def test_missed_input_is_retried_and_every_attempt_preserved(self):
        g, d, r = self.make(retries=1, drop_keys=('D',))
        res = self.run_ids(r, ['M2_walk_short'])
        self.assertEqual(len(res), 2)
        for x in res:
            self.assertPreservedFailure(r, x, 'missed_input')
        self.assertEqual(len(self.sessions(r.cfg.failed_root)), 2)
        self.assertEqual(self.sessions(self.root), [])           # nothing unusable left where tests read
        self.assertEqual(d.held, set())

    def test_missed_grapple_fails_in_sighting_before_recording(self):
        g, d, r = self.make(retries=0, drop_keys=('LMB',))
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['failure']['kind'], 'missed_input')
        self.assertNotIn('session', res)                           # sighting happens before any recording
        self.assertEqual(self.sessions(self.root) + self.sessions(r.cfg.failed_root), [])

    # ------------------------------------------------------------------ live failure 1: aim feedback
    def test_live_aim_failure_is_solved_by_sighting(self):
        """Live 2026-10-11: crosshair_tx/ty static and oIO has no aimx/aimy -> feedback aim failed. Sighting
        measures the aim origin from the shots themselves; the real shot is direction-checked."""
        g, d, r = self.make(crosshair_static=True, io_absent=('aimx', 'aimy'))
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertAlmostEqual(res['sighting']['fit']['P'][0], 640.0, places=6)
        self.assertAlmostEqual(res['sighting']['fit']['P'][1], 360.0, places=6)
        self.assertLess(abs(res['observations']['aim_check'][0]['error_deg']), 1.0)

    def test_sighting_finds_an_off_centre_spider(self):
        g, d, r = self.make(camera_offset=(-180.0, 90.0), crosshair_static=True)
        res = self.run_ids(r, ['W2_swing'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        P = res['sighting']['fit']['P']
        self.assertAlmostEqual(P[0], 460.0, delta=1.0)
        self.assertAlmostEqual(P[1], 450.0, delta=1.0)
        for c in res['observations']['aim_check']:
            self.assertLess(abs(c['error_deg']), 1.0)

    def test_sighting_works_whether_or_not_gh_gv_survive_attachment(self):
        g, d, r = self.make(gh_reset_on_attach=False)
        self.assertEqual(self.run_ids(r, ['W1_attach_hang'])[0]['status'], 'pass')

    def test_wrong_aim_is_caught_by_the_shot_direction_check(self):
        g, d, r = self.make(retries=0)
        r._sight = lambda res: SC.SightedAim((640.0, 520.0), {'fit': {'rms_px': 0.0}})   # deliberately wrong
        res = self.run_ids(r, ['W2_swing'])[0]
        self.assertPreservedFailure(r, res, 'aim_failed')
        self.assertIn('intended 45 deg', res['failure']['msg'])

    def test_verified_aimscan_feedback_profile_replaces_sighting(self):
        g, d, r = self.make()
        r.aim_profile = {'feedback_pair': ['io.aimx', 'io.aimy']}
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertNotIn('sighting', res)
        self.assertEqual(res['observations']['aim'][0]['source'], 'io.aimx.aimy')
        self.assertLess(abs(res['observations']['aim_check'][0]['error_deg']), 2.5)

    # ------------------------------------------------------------------ live failure 2: false takeover
    def test_physical_mouse_movement_during_a_scenario_aborts(self):
        g, d, r = self.make()
        d.physical_at = [(g.index + 120, 'move')]
        with self.assertRaises(AbortRun) as cm:
            self.run_ids(r, ['M2_walk_short'])
        self.assertIn('physical mouse movement', str(cm.exception))
        self.assertEqual(d.held, set())
        self.assertPreservedFailure(r, r.results[0], 'aborted')

    def test_physical_key_press_aborts_and_f12_is_an_emergency(self):
        g, d, r = self.make()
        d.physical_at = [(g.index + 100, 'key')]
        with self.assertRaises(AbortRun) as cm:
            self.run_ids(r, ['M2_walk_short'])
        self.assertIn('physical key press', str(cm.exception))
        g2, d2, r2 = self.make()
        d2.physical_at = [(g2.index + 100, 'f12')]
        with self.assertRaises(AbortRun) as cm:
            self.run_ids(r2, ['M2_walk_short'])
        self.assertIn('EMERGENCY', str(cm.exception))

    def test_user_input_during_the_setup_prompt_is_not_a_takeover(self):
        g, d, r = self.make()

        def ask(msg):                    # the user walks the spider into place while the runner waits
            d.monitor.on_mouse(0x0200, 10, 10, False, False)
            d.monitor.on_mouse(0x0200, 300, 200, False, False)
            d.monitor.on_key(0x44, True, False, False)
            return ''
        r.ui.ask = ask
        res = self.run_ids(r, ['M2_walk_short'])
        self.assertEqual(res[0]['status'], 'pass', res[0].get('failure'))

    # ------------------------------------------------------------------ live failure 3: M2 left the ground
    def test_insufficient_ground_is_detected_before_any_input(self):
        """Live 2026-10-11: M2 walked ~840 px right and left the ground. The scan now says so first."""
        g, d, r = self.make(ground_x=(0, 600))
        res = self.run_ids(r, ['M2_walk_short', 'M2_walk_flat'])
        self.assertEqual([x['status'] for x in res], ['skipped', 'skipped'])
        self.assertEqual({x['failure']['kind'] for x in res}, {'insufficient_space'})
        self.assertFalse([e for e in d.events if e[1] in ('D', 'A')])   # no walking key was ever pressed
        self.assertEqual(self.sessions(self.root) + self.sessions(r.cfg.failed_root), [])

    def test_short_walk_runs_where_the_long_one_does_not_fit(self):
        g, d, r = self.make(ground_x=(0, 1000))
        res = self.run_ids(r, ['M2_walk_short', 'M2_walk_flat'])
        self.assertEqual([x['status'] for x in res], ['pass', 'skipped'], [x.get('failure') for x in res])
        s = cap.Session(os.path.join(self.root, res[0]['session']))
        walking = [f for f in s.frames if cap.support(f) == 'ground' and abs(f['io'].get('hm') or 0) == 1]
        self.assertGreaterEqual(len(walking), 60)                 # enough for test_walk_plateau_speed

    # ------------------------------------------------------------------ live failure 4: locked cursor, relative aim
    def test_locked_cursor_without_profile_fails_fast_without_firing(self):
        """Live 2026-10-11 (-AimScan): the game recentres the cursor every frame. Sighting cannot work then;
        the runner says so before firing anything."""
        g, d, r = self.make(retries=0, mouse_relative=True, cursor_recenters=True)
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['failure']['kind'], 'aim_failed')
        self.assertIn('run -AimScan first', res['failure']['msg'])
        self.assertFalse([e for e in d.events if e[1] == 'LMB'])
        self.assertEqual(self.sessions(self.root) + self.sessions(r.cfg.failed_root), [])

    def test_aim_scan_measures_the_native_relative_aim_and_scenarios_use_it(self):
        g, d, r = self.make(mouse_relative=True, cursor_recenters=True, crosshair_static=True)
        an = r.aim_scan(os.path.join(self.tmp, 'scan'))
        self.assertEqual(an['cursor']['mode'], 'recentred_or_locked')
        prof = an['relative_profile']
        self.assertIsNotNone(prof, an.get('impulse_rejected_pairs'))
        self.assertEqual(prof['pair'], ['player.aimx', 'player.aimy'])
        self.assertEqual(prof['method'], 'abs')
        self.assertEqual(prof['kind'], 'point')
        self.assertTrue(prof['kind_verified_by_aimdir'])
        self.assertAlmostEqual(prof['gain'][0][0], 0.75, places=3)       # mock view scale
        self.assertAlmostEqual(prof['gain'][1][1], 0.75, places=3)
        resp = an['impulse_response']['abs']
        for k in ('io.mx', 'io.my', 'io.gmx', 'io.gmy', 'io.aimdir', 'player.aimx', 'player.aimy'):
            self.assertIn(k, resp)
        self.assertFalse([e for e in d.events if e[0] == 'down'])        # the scan never clicks or presses
        with open(r.cfg.aim_profile_file) as f:
            self.assertEqual(json.load(f)['mode'], 'relative')
        res = self.run_ids(r, ['W1_attach_hang', 'W2_swing'])
        self.assertEqual([x['status'] for x in res], ['pass', 'pass'], [x.get('failure') for x in res])
        for x in res:
            self.assertTrue(x['aim_method'].startswith('relative'))
            self.assertNotIn('sighting', x)
            for c in x['observations']['aim_check']:
                self.assertLess(abs(c['error_deg']), 2.0)

    def test_relative_impulses_via_relative_moves_only(self):
        g, d, r = self.make(mouse_relative=True, cursor_recenters=True)
        r.aim_profile = {'mode': 'relative', 'method': 'rel', 'pair': ['io.mx', 'io.my'], 'kind': 'point',
                         'gain': [[0.75, 0.0], [0.0, 0.75]], 'lag_frames': 1}
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))

    def test_relative_only_aim_fails_cleanly_before_recording(self):
        """Absolute-cursor game whose aim ignores the absolute position: sighting rays are parallel."""
        g, d, r = self.make(retries=0)
        g.mouse_relative = True               # aim ignores absolute moves; cursor NOT locked (no recentring)
        d.cursor_client = lambda: None        # cursor position unknown -> not treated as locked
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['failure']['kind'], 'aim_failed')
        self.assertEqual(self.sessions(self.root) + self.sessions(r.cfg.failed_root), [])
        self.assertEqual(d.held, set())

    # ------------------------------------------------------------------ live failure 2/5: takeover arming
    def test_game_recentring_the_cursor_is_not_a_takeover(self):
        g, d, r = self.make(cursor_recenters=True)
        res = self.run_ids(r, ['M2_walk_short'])
        self.assertEqual([x['status'] for x in res], ['pass'], [x.get('failure') for x in res])

    def test_escape_during_readiness_is_not_a_takeover(self):
        """Live 2026-10-11: physical Escape (0x1B) during the initial readiness wait aborted the run. Detection is
        now armed only after readiness; F12 / Pause still work in every phase."""
        g, d, r = self.make()
        d.physical_at = [(g.index + 5, 'esc')]          # inside the readiness wait (settling takes 30 frames)
        res = self.run_ids(r, ['M2_walk_short'])
        self.assertEqual(res[0]['status'], 'pass', res[0].get('failure'))
        self.assertIn('takeover detection armed: M2_walk_short', '\n'.join(r.ui.lines))
        g2, d2, r2 = self.make()
        d2.physical_at = [(g2.index + 5, 'f12')]        # emergency stop is honoured even before arming
        with self.assertRaises(AbortRun):
            self.run_ids(r2, ['M2_walk_short'])

    def test_open_menu_delays_readiness_and_no_input_is_sent_meanwhile(self):
        g, d, r = self.make(menu_frames=150)
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        first_input = min(e[2] for e in d.events if e[0] == 'down')
        self.assertGreater(first_input, 150)

    # ------------------------------------------------------------------ live failure 6: ground continuity
    def test_ambiguous_ground_is_skipped_unless_trust_ground(self):
        unknown = G.make_solid(77, 'oRockPoly', 700, 590, 1, 1, 0, None, 2)     # polygon fixture, no sprite
        g, d, r = self.make(extra_solids=[unknown])
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'skipped')
        self.assertEqual(res['failure']['kind'], 'ground_ambiguous')
        self.assertIn('-TrustGround', res['failure']['msg'])
        self.assertFalse([e for e in d.events if e[1] in ('D', 'A')])
        g, d, r = self.make(extra_solids=[unknown], trust_ground=True)
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertIn('OVERRIDDEN', res['ground_gate'])
        self.assertIn('actual_max_right_px', res['ground_scan_check'])

    def test_leaving_the_ground_is_explained_in_diagnostic_mode(self):
        g, d, r = self.make(retries=0, ground_x=(0, 600), scan_unavailable=True)
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual((res['status'], res['failure']['kind']), ('skipped', 'ground_unavailable'))
        g, d, r = self.make(retries=0, ground_x=(0, 600), scan_unavailable=True, trust_ground=True)
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertPreservedFailure(r, res, 'setup_invalid')
        lg = res['observations']['left_ground'][0]
        self.assertAlmostEqual(lg['distance_from_start_px'], 200, delta=15)
        out = subprocess.run([sys.executable, DIAG, res['preserved_at']], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('support lost at frame', out.stdout)
        self.assertIn('ground instances walked on', out.stdout)

    def test_ground_scan_diagnostic_and_capture_comparison(self):
        g, d, r = self.make(ground_x=(0, 1000))
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        out = r.ground_scan(os.path.join(self.root, res['session']))
        self.assertEqual(out['profile']['confidence'], 'estimate')
        self.assertEqual([c['id'] for c in out['corridor']], [9001])
        inst = out['compare']['instances']
        self.assertEqual(len(inst), 1)
        self.assertEqual(inst[0]['verdict'], 'OK')
        self.assertFalse([e for e in d.events if e[2] > g.index - 1 and e[0] == 'down'])

    # ------------------------------------------------------------------ live failure 7: menus, focus, startup
    def test_persistent_menu_objects_do_not_block(self):
        """Live 2026-10-11: oMenuControllerPrompt / oSettingsMenu instances existed during gameplay and readiness
        refused to start. Existence is now evidence only; it is reported, not blocking."""
        g, d, r = self.make(persistent_menus=True)
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertEqual({m['obj'] for m in res['game_state']['menus']}, {'oSettingsMenu', 'oMenuControllerPrompt'})

    def test_real_pause_variable_delays_start_and_no_input_meanwhile(self):
        g, d, r = self.make(persistent_menus=True, menu_window=(0, 400))
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertGreater(min(e[2] for e in d.events if e[0] == 'down'), 400)

    def test_state_scan_finds_the_menu_indicator_and_readiness_uses_it(self):
        g, d, r = self.make(persistent_menus=True, paused_owner=False)
        b0 = g.index + 300 + 300                     # lead + phase A
        g.menu_window = (b0 + 30, b0 + 300)          # the "user" opens the menu during phase B
        an = r.state_scan(os.path.join(self.tmp, 'ss'))
        self.assertEqual(an['indicator']['obj'], 'oSettingsMenu')
        self.assertEqual(an['indicator']['var'], 'is_open')
        self.assertEqual(an['indicator']['open_values'], [1])
        self.assertTrue(any(c['key'].startswith('world byte') for c in an['candidates']))
        self.assertEqual(set(an['phases']), {'A_closed', 'B_open', 'C_closed'})
        self.assertFalse(d.events)                    # read-only: nothing pressed or moved
        self.assertTrue(os.path.exists(r.cfg.state_profile_file))
        g.menu_window = (g.index, g.index + 500)      # menu open again; only the indicator knows
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertGreater(min(e[2] for e in d.events if e[0] == 'down'), g.menu_window[1])

    def test_invisible_pause_stops_a_walk_as_not_controllable(self):
        g, d, r = self.make(retries=0, persistent_menus=True, paused_owner=False)
        g.menu_window = (g.index + 60, g.index + 5000)   # opens during the scenario, nothing tells the runner
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertPreservedFailure(r, res, 'not_controllable')
        self.assertEqual(d.held, set())

    def test_invisible_pause_never_fires_an_aimed_shot(self):
        g, d, r = self.make(retries=0, mouse_relative=True, cursor_recenters=True, paused_owner=False)
        r.aim_profile = {'mode': 'relative', 'method': 'abs', 'pair': ['player.aimx', 'player.aimy'],
                         'kind': 'point', 'gain': [[0.75, 0.0], [0.0, 0.75]], 'lag_frames': 1}
        g.menu_window = (g.index + 45, g.index + 5000)
        res = self.run_ids(r, ['W1_attach_hang'])[0]
        self.assertEqual(res['failure']['kind'], 'aim_failed')
        self.assertFalse([e for e in d.events if e[1] == 'LMB'])

    def test_initial_focus_transition_from_console(self):
        """Starting from PowerShell: Webbed is not in front; the runner brings it forward itself (no Alt+Tab)."""
        g, d, r = self.make()
        d.focus_lost_at = 0                            # console in front at start
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))

    def test_read_only_diagnostics_need_no_focus(self):
        g, d, r = self.make()
        d.focus_lost_at = 0
        d.refocus_ok = False
        out = r.ground_scan()
        self.assertEqual(out['profile']['confidence'], 'estimate')
        self.assertFalse(d.armed)
        self.assertFalse(d.events)

    def test_setup_window_waits_for_the_user_then_hands_off(self):
        g, d, r = self.make(setup_window=900)
        start = g.index
        d.physical_at = [(start + 100, 'move'), (start + 160, 'key'), (start + 220, 'move')]
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        first = min(e[2] for e in d.events if e[0] == 'down')
        self.assertGreaterEqual(first, start + 220 + 180)        # 3 s of game time after the last touch
        self.assertLess(first, start + 900)                       # ended early: the user had moved

    def test_emergency_stop_during_setup(self):
        g, d, r = self.make(setup_window=900)
        d.physical_at = [(g.index + 50, 'f12')]
        with self.assertRaises(AbortRun):
            self.run_ids(r, ['M2_walk_short'])
        self.assertFalse([e for e in d.events if e[0] == 'down'])

    # ------------------------------------------------------------------ live failure 8: M2 did not move
    def test_blocked_spider_is_blocked_not_paused_and_not_retried(self):
        """Live 2026-10-11: M2_walk_short failed twice as not_controllable on the first (right) walk. A spider
        pushing against an obstacle (physics running) is now 'blocked'; a frozen simulation stays not_controllable."""
        g, d, r = self.make(retries=2, block_right_x=411.5)
        res = self.run_ids(r, ['M2_walk_short'])
        self.assertEqual(len(res), 1)                                  # same spot would block again
        self.assertPreservedFailure(r, res[0], 'blocked')
        nm = res[0]['observations']['no_motion'][0]
        self.assertFalse(nm['physics_frozen'])
        self.assertEqual(nm['support'], 'ground')
        self.assertEqual(d.held, set())

    def test_directional_walk_uses_the_free_side(self):
        g, d, r = self.make(block_right_x=411.5, ground_x=(-1000, 2000))
        res = self.run_ids(r, ['M2_walk_dir'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        probe = res['observations']['direction_probe'][0]
        self.assertEqual(probe['usable'], ['left'])
        self.assertLess(probe['moved_px']['right'], 2.0)
        s = cap.Session(os.path.join(self.root, res['session']))
        walking = [f for f in s.frames if cap.support(f) == 'ground' and abs(f['io'].get('hm') or 0) == 1]
        self.assertGreaterEqual(len(walking), 60)

    def test_directional_walk_respects_the_ground_scan(self):
        g, d, r = self.make(ground_x=(250, 2000))          # 150 px left, 1600 right: only right is long enough
        res = self.run_ids(r, ['M2_walk_dir'])[0]
        self.assertEqual(res['status'], 'pass', res.get('failure'))
        self.assertEqual(res['observations']['direction_probe'][0]['usable'], ['right'])
        g, d, r = self.make(ground_x=(250, 600))           # neither side has 380 px: skipped before any input
        res = self.run_ids(r, ['M2_walk_dir'])[0]
        self.assertEqual((res['status'], res['failure']['kind']), ('skipped', 'insufficient_space'))
        self.assertFalse([e for e in d.events if e[0] == 'down'])

    def test_analyze_walk_measures_real_movement_from_captures(self):
        g, d, r = self.make(retries=0, block_right_x=411.5)
        blocked = self.run_ids(r, ['M2_walk_short'])[0]['preserved_at']
        g, d, r = self.make(retries=0, ground_x=(-200, 2000), trust_ground=True)
        ok = os.path.join(self.root, self.run_ids(r, ['M2_walk_flat'])[0]['session'])
        g, d, r = self.make(retries=0, ground_x=(0, 600), scan_unavailable=True, trust_ground=True)
        fell = self.run_ids(r, ['M2_walk_short'])[0]['preserved_at']
        sys.path.insert(0, os.path.join(HERE, '..', 'tools', 'reference'))
        import analyze_walk as AW
        a = AW.analyze(blocked)
        self.assertEqual(a['phases'][0]['class'], 'blocked_on_ground')
        self.assertIn('consistent', a['sampling']['verdict'])
        b = AW.analyze(ok)
        self.assertEqual([p['class'] for p in b['phases']], ['moved', 'moved'])
        self.assertTrue(b['level_walk'])
        for lw in b['level_walk']:
            self.assertAlmostEqual(lw['plateau_px_step'], 3.5, places=3)
        c = AW.analyze(fell)
        self.assertGreater(c['free_flight']['pairs'], 5)                # mock free flight uses the model: ~0 residual
        self.assertLess(c['free_flight']['max_abs_residual_pxps'], 1e-6)
        self.assertTrue(any(t['support'] == 'air' for t in c['transitions']))
        out = subprocess.run([sys.executable, WALK, '--tests', ok, blocked, fell], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('blocked_on_ground', out.stdout)
        self.assertIn('PASS  test_walk_plateau_speed', out.stdout)
        self.assertTrue(os.path.isdir(blocked) and os.path.isdir(fell))  # originals untouched

    # ------------------------------------------------------------------ diagnostics
    def test_aim_scan_finds_mouse_following_variables(self):
        g, d, r = self.make()
        an = r.aim_scan(os.path.join(self.tmp, 'scan'))
        vars_ = {f['var'] for f in an['followers']}
        self.assertTrue({'player.crosshair_tx', 'player.crosshair_ty', 'io.aimx', 'io.aimy'} <= vars_, vars_)
        self.assertEqual(an['cursor']['mode'], 'positional')
        self.assertNotIn('player.mock_unrelated_counter', vars_)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'scan', 'aimscan.jsonl')))
        self.assertTrue(os.path.exists(r.cfg.aim_profile_file))
        self.assertEqual(d.held, set())
        self.assertFalse([e for e in d.events if e[0] == 'down'])  # nothing was clicked or pressed

    def test_aim_scan_reports_locked_cursor_without_usable_variables(self):
        g, d, r = self.make(crosshair_static=True, io_absent=('aimx', 'aimy'), cursor_recenters=True)
        an = r.aim_scan(os.path.join(self.tmp, 'scan'))
        self.assertEqual(an['followers'], [])
        self.assertIsNone(an['relative_profile'])
        self.assertEqual(an['cursor']['mode'], 'recentred_or_locked')
        self.assertIn('no variable pair responded', an['recommendation'])
        self.assertFalse(os.path.exists(r.cfg.aim_profile_file))

    def test_aim_sight_diagnostic(self):
        g, d, r = self.make(camera_offset=(50.0, -40.0))
        out = r.aim_sight()
        self.assertTrue(out['ok'], out)
        self.assertAlmostEqual(out['P'][0], 690.0, delta=1.0)
        self.assertAlmostEqual(out['P'][1], 320.0, delta=1.0)

    # ------------------------------------------------------------------ safety
    def test_refused_focus_ends_not_ready_and_sends_nothing(self):
        g, d, r = self.make(retries=0)
        d.refocus_ok = False
        d.focus_lost_at = 0
        res = self.run_ids(r, ['M2_walk_short'])[0]
        self.assertEqual(res['failure']['kind'], 'not_ready')
        self.assertIn('not the foreground window', res['failure']['msg'])
        self.assertFalse([e for e in d.events if e[0] == 'down'])
        self.assertEqual(self.sessions(self.root), [])

    def test_emergency_stop_while_waiting_for_focus(self):
        g, d, r = self.make()
        d.emergency_at = 0
        with self.assertRaises(AbortRun) as cm:
            r.focus()
        self.assertIn('EMERGENCY', str(cm.exception))

    def test_focus_loss_only_aborts_while_armed(self):
        """Unarmed phases (readiness, setup, read-only diagnostics) tolerate focus changes; armed phases abort."""
        g, d, r = self.make()
        d.focus_lost_at = 0
        d.refocus_ok = False
        self.assertEqual(r.wait_ready('any', timeout=100), [])        # unarmed: no abort, no refocus needed
        d.arm()
        with self.assertRaises(AbortRun):
            r.wait_ready('any', timeout=100)
        r.guard.require_focus = False                                   # -Check: never
        self.assertEqual(r.wait_ready('any', timeout=100), [])

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
        self.assertIn('exist but no candidate', r.calibration['actions']['retract']['reason'])

    def test_calibration_explains_a_missing_oio_variable(self):
        g, d, r = self.make(keys={}, io_absent=('retract',))
        b = r.calibrate(B.load_defaults())
        a = r.calibration['actions']
        self.assertEqual(a['retract']['status'], 'unresolved')
        self.assertIn('oIO has no variable retract', a['retract']['reason'])
        self.assertEqual(a['retract']['tried'], ['SHIFT', 'S', 'CTRL', 'DOWN', 'MMB'])
        self.assertIn('retract', r.calibration['oio_variables_missing'])
        self.assertEqual({k: v for k, v in b.keys.items() if v},
                         {'right': 'D', 'left': 'A', 'jump': 'SPACE', 'weave': 'RMB', 'grapple': 'LMB'})
        self.assertIn('1 unresolved (retract)', RN.verdict([], {'calibration': r.calibration}))
        self.assertIn('NO gameplay scenarios', RN.verdict([], {'calibration': r.calibration}))

    def test_partial_recalibration_keeps_confirmed_keys(self):
        base = B.Bindings({'right': 'D', 'left': 'A', 'jump': 'SPACE', 'grapple': 'LMB', 'weave': 'RMB',
                           'retract': None}, calibrated={'right': {'key': 'D', 'latency_frames': 1}})
        g, d, r = self.make(keys={})
        g.keys = {'right': 'RIGHT', 'retract': 'CTRL'}          # only retract responds to a candidate now
        b = r.calibrate(B.load_defaults(), actions=['retract'], base=base)
        self.assertEqual(b.keys, {'right': 'D', 'left': 'A', 'jump': 'SPACE', 'grapple': 'LMB', 'weave': 'RMB',
                                  'retract': 'CTRL'})
        self.assertEqual(b.calibrated['right'], {'key': 'D', 'latency_frames': 1})
        self.assertEqual(r.calibration['actions']['right']['status'], 'kept')
        self.assertEqual(d.held, set())

    def test_bindings_save_backs_up_previous_file(self):
        path = os.path.join(self.tmp, 'auto_bindings.local.json')
        self.assertIsNone(B.Bindings({'right': 'D'}).save(path))
        backup = B.Bindings({'right': 'RIGHT'}).save(path)
        with open(backup) as f:
            self.assertEqual(json.load(f)['keys'], {'right': 'D'})
        self.assertEqual(B.Bindings.load(path).keys, {'right': 'RIGHT'})

    # ------------------------------------------------------------------ readiness gate
    def test_readiness_gate_never_continues_and_reports_measured_speed(self):
        g, d, r = self.make(jitter_pxps=3.0)          # body never below 1 px/s
        with self.assertRaises(SC.ScenarioFailure) as cm:
            r.ensure_ready('any', 'run start', timeout=120)
        self.assertEqual(cm.exception.kind, 'not_ready')
        self.assertIn('did not come to rest', cm.exception.msg)
        self.assertIn('3.00..3.00 px/s', cm.exception.msg)
        with self.assertRaises(SC.ScenarioFailure):
            r.calibrate(B.load_defaults())             # calibration is gated by the same check
        self.assertEqual(d.held, set())


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


class Takeover(unittest.TestCase):
    def test_monitor_tells_runner_input_from_user_input(self):
        m = TakeoverMonitor()
        m.arm()
        m.on_mouse(0x0200, 0, 0, True, True)              # ours (tagged SendInput)
        m.on_mouse(0x0200, 500, 500, True, True)
        m.on_mouse(0x0201, 500, 500, True, True)
        m.on_key(0x44, True, True, True)
        m.on_mouse(0x0200, 640, 360, True, False)         # injected by someone else (cursor tools / drivers)
        m.on_mouse(0x0200, 642, 361, False, False)        # physical jitter, 2 px
        self.assertIsNone(m.takeover())
        m.on_mouse(0x0200, 649, 361, False, False)        # physical, 9 px total
        self.assertIn('physical mouse movement', m.takeover())
        self.assertEqual(m.counts['foreign_injected_moves'], 1)

    def test_monitor_keys_buttons_and_arming(self):
        m = TakeoverMonitor()
        m.on_key(0x41, True, False, False)                # disarmed: ignored
        self.assertIsNone(m.takeover())
        m.arm()
        m.on_key(0x41, False, False, False)               # key up only: ignored
        self.assertIsNone(m.takeover())
        m.on_mouse(0x0204, 0, 0, True, False)             # another program's right click
        self.assertIn('injected', m.takeover())
        m.arm()
        self.assertIsNone(m.takeover())
        m.on_key(0x7B, True, False, False)                # F12
        self.assertTrue(m.emergency)
        self.assertIsNone(m.takeover())
        m.disarm()
        self.assertIsNone(m.takeover())


class AimGeometry(unittest.TestCase):
    def test_ray_intersection(self):
        P = (420.0, 300.0)
        rays = []
        for a in (240, 300, 270, 30):
            m = (P[0] + 100 * math.cos(math.radians(a)), P[1] - 100 * math.sin(math.radians(a)))
            rays.append((m[0], m[1], math.cos(math.radians(a)), -math.sin(math.radians(a))))
        fit = A.ray_intersection(rays)
        self.assertAlmostEqual(fit['P'][0], 420.0, places=6)
        self.assertAlmostEqual(fit['P'][1], 300.0, places=6)
        self.assertLess(fit['rms_px'], 1e-6)
        self.assertGreater(fit['min_t'], 0)

    def test_parallel_rays_are_rejected(self):
        fit = A.ray_intersection([(100, 100, 1, -1), (300, 120, 1, -1), (500, 50, 1, -1)])
        self.assertIsNone(fit['P'])

    def test_shot_direction_fallback(self):
        self.assertAlmostEqual(A.shot_direction({'gh': 0.0, 'gv': -35.0})[1], 90.0)
        d = A.shot_direction({'gh': 0, 'gv': 0, 'sx': 10, 'sy': 10, 'gx': 20, 'gy': 20})
        self.assertAlmostEqual(d[1], 315.0)
        self.assertIsNone(A.shot_direction({'gh': 0, 'gv': 0}))


class GroundScan(unittest.TestCase):
    SPR = {'name': 'sGround', 'w': 32, 'h': 32, 'ox': 16, 'oy': 16}

    def floor(self, sid, x0, x1, top, angle=0.0):
        return G.make_solid(sid, 'oGround', (x0 + x1) / 2.0, top + 16, (x1 - x0) / 32.0, 1.0, angle, self.SPR, 1)

    def test_instance_box(self):
        self.assertEqual(G.instance_box(100, 200, 4, 1, 0, self.SPR), (36.0, 184.0, 164.0, 216.0))
        l, t, r, b = G.instance_box(0, 0, 2, 1, 90, self.SPR)        # rotated: tall box
        self.assertAlmostEqual(r - l, 32, places=6)
        self.assertAlmostEqual(b - t, 64, places=6)

    def test_edge_wall_and_unavailable(self):
        floor = self.floor(1, 0, 600, 100)
        wall = G.make_solid(2, 'oGround', 516, 50, 1, 100 / 32.0, 0, self.SPR, 1)
        p = G.ground_profile([floor], (200, 89), 1)
        self.assertEqual(p['confidence'], 'estimate')
        self.assertEqual(p['right']['end'], 'edge')
        self.assertAlmostEqual(p['right']['clear_px'], 400, delta=G.STEP + 2)
        self.assertAlmostEqual(p['left']['clear_px'], 200, delta=G.STEP + 2)
        p = G.ground_profile([floor, wall], (200, 89), 1)
        self.assertEqual(p['right']['end'], 'wall')
        self.assertAlmostEqual(p['right']['clear_px'], 500 - 11 - 200, delta=G.STEP + 2)
        self.assertEqual(G.ground_profile([floor], (200, 89), 99)['confidence'], 'unavailable')
        self.assertEqual(G.ground_profile([floor], (200, 40), 1)['confidence'], 'ambiguous')

    def test_adjacent_pieces_with_small_steps_are_continuous(self):
        """The v1 scan stopped at the first piece; steps up to the body radius are walked over now."""
        pieces = [self.floor(1, 0, 112, 100), self.floor(2, 112, 224, 106), self.floor(3, 224, 336, 98),
                  self.floor(4, 336, 700, 103)]
        p = G.ground_profile(pieces, (56, 89), 1)
        self.assertEqual(p['confidence'], 'estimate')
        self.assertGreater(p['right']['clear_px'], 600)
        self.assertEqual([q[0] for q in p['right']['pieces']], [1, 2, 3, 4])

    def test_rotated_pieces_use_their_real_surface(self):
        """A slightly rotated slab: its axis-aligned box top is several px above the surface under the spider."""
        slab = self.floor(1, 0, 400, 100, angle=1.5)
        box_top = slab['box'][1]
        sp = G.span_at(slab['poly'], 200)
        self.assertGreater(sp[0] - box_top, 4.0)
        nxt = self.floor(2, 399, 800, sp[0] + 5)                      # continues near the slab's right end
        p = G.ground_profile([slab, nxt], (200, sp[0] - 11), 1)
        self.assertEqual(p['confidence'], 'estimate', p.get('reason'))
        self.assertGreater(p['right']['clear_px'], 450)

    def test_tall_step_is_a_wall_and_big_level_change_is_not_flat(self):
        p = G.ground_profile([self.floor(1, 0, 300, 100), self.floor(2, 300, 600, 80)], (100, 89), 1)
        self.assertEqual(p['right']['end'], 'wall')
        ramp = [self.floor(i, 100 * i, 100 * i + 100, 100 - 8 * i) for i in range(8)]
        p = G.ground_profile(ramp, (50, 89), 0)
        self.assertEqual(p['right']['end'], 'not_flat')

    def test_unknown_shape_in_corridor_makes_scan_ambiguous(self):
        unknown = G.make_solid(9, 'oRockPoly', 300, 95, 1, 1, 0, None, 2)
        p = G.ground_profile([self.floor(1, 0, 600, 100), unknown], (100, 89), 1)
        self.assertEqual(p['confidence'], 'ambiguous')
        self.assertIn('oRockPoly', p['reason'])

    def test_compare_with_capture_classifies_errors(self):
        def frames(gid, xs, top):
            return [{'player': {'body': {'pos_px': [x, top - 11]}, 'vars': {'ground': gid, 'ground_angle': 0}}}
                    for x in xs]
        ok = self.floor(1, 0, 300, 100)
        off = self.floor(2, 300, 600, 90)                            # model 10 px above the real surface
        rot = self.floor(3, 600, 900, 100, angle=3)                  # model rotated, reality flat
        fr = frames(1, range(10, 290, 10), 100) + frames(2, range(310, 590, 10), 100) + \
            frames(3, range(610, 890, 10), 100) + frames(4, range(910, 990, 10), 100)
        rows = {r['id']: r for r in G.compare_with_capture([ok, off, rot], fr)}
        self.assertEqual(rows[1]['verdict'], 'OK')
        self.assertIn('constant offset -10.0', rows[2]['verdict'])
        self.assertIn('varies along x', rows[3]['verdict'])
        self.assertIn('NOT IN SCAN', rows[4]['verdict'])

    def test_solid_objects_from_inventory(self):
        objs, sprites = G.load_inventory()
        names = G.solid_object_names(objs)
        self.assertIn('oGround', names)
        self.assertIn('oSolidPlatform', names)
        self.assertNotIn('oPlayer', names)


class ImpulseAnalysis(unittest.TestCase):
    def test_gain_integrator_and_pair_choice(self):
        imps = []
        for dx, dy in [(20, 0), (-20, 0), (60, 0), (-60, 0), (0, 20), (0, -20), (0, 60), (0, -60)]:
            base = {'player.aimx': 500.0, 'player.aimy': 300.0, 'io.flash': 0.0}
            series = [dict(base)]
            for k in range(1, 9):
                lagged = k >= 2
                series.append({'player.aimx': 500.0 + (0.5 * dx if lagged else 0),
                               'player.aimy': 300.0 + (0.5 * dy if lagged else 0),
                               'io.flash': 1.0 if k == 1 else 0.0})
            imps.append({'method': 'abs', 'd': (dx, dy), 'series': series})
        resp = A.analyze_impulses(imps)
        self.assertAlmostEqual(resp['abs']['player.aimx']['gx'], 0.5)
        self.assertTrue(resp['abs']['player.aimx']['integrator'])
        self.assertEqual(resp['abs']['player.aimx']['lag_frames'], 2)
        self.assertNotIn('io.flash', resp['abs'])                    # transient: no end change
        prof = A.choose_relative_profile(resp, {})
        self.assertEqual(prof['pair'], ['player.aimx', 'player.aimy'])
        self.assertEqual(prof['gain'], [[0.5, 0.0], [0.0, 0.5]])

    def test_relations_with_aimdir(self):
        rel = A.relations({'io.aimdir': 45.0, 'player.aimx': 110.0, 'player.aimy': 90.0, 'io.mx': 1.0, 'io.my': -1.0},
                          (100.0, 100.0))
        self.assertLess(rel['player.aimx/player.aimy']['point_err_deg'], 1e-6)
        self.assertLess(rel['io.mx/io.my']['vector_err_deg'], 1e-6)


if __name__ == '__main__':
    unittest.main()
