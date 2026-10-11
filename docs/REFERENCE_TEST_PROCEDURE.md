# Reference capture procedure (original Webbed)

Purpose: record measurable motion, web and input state from the **original** game, so the
recovered model and later reconstruction can be regression-tested against it
(`python -m unittest discover -s tests`).

## What the probe does and does not do
- Attaches **only** to `webbed.exe` with SHA-256 `c63cecb6…16dde` (Steam build 7679547); any other build is refused.
- Opens the process with **read-only** rights (`PROCESS_VM_READ`, `PROCESS_QUERY_LIMITED_INFORMATION`).
  No debugger, no breakpoints, no memory writes, no injected code. Game files are not touched.
- Samples **once per game frame** (fast lane: player, input, world, nearby webs, skateboards; about 1 ms of
  reads). Each row is bracketed by two reads of the game's frame-start timestamp and frame counter. A row
  is `torn` if either changed, meaning the next game update began while reading. Other webs are sampled in
  the idle time between frames (slow lane), and stop as soon as a new frame starts.
- Handles ASLR: the game's load address is found with a read-only module snapshot, then every address is rebased.
- Records only:
  - the player's Box2D pose and velocity;
  - selected player state variables (grounded, web flags, tilt, foot mode, …);
  - the game's own input-state object (`oIO`: move/aim/jump/web buttons as the game sees them);
  - every web segment (endpoints, rest and current length, slack, stretch, break buffers);
  - web nodes, and skateboards (pose, velocity).

  It does not read your keyboard, apart from the three hotkeys below.
- Output: `C:\Users\profe\DesktopPlaygroundLocal\captures\<scenario>_<time>\` (numbers only, outside the repo).

Hotkeys while recording (the game still receives the keys):

| Key | Action |
|---|---|
| **F9** | start / stop a recording segment (a beep confirms start) |
| **F10** | add a step marker (use where a step below says "marker") |
| **Ctrl+F12** | finish, save and exit the probe |

## Automated recording (recommended)
The automated runner plays the scenarios itself with ordinary keyboard and mouse input. It uses the read-only
probe above to watch the game every frame. **Status: tested only against a mock game in the cloud; the first
live run on your PC is the real test.** Report anything odd.

**One-time:**
1. `powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -SelfTest`
   This doesn't need the game. It must end with `SELF-TEST OK`.
2. Start Webbed, enter a level, and stand on solid ground. Then run:
   `powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -Check`
   This sends no input. It must say `readiness: READY`.
3. `powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -Calibrate`
   The spider moves, jumps and shoots a web or two while the runner finds which key does what. Keys are
   confirmed through the game's own input object. The result is saved to
   `DesktopPlaygroundLocal\auto_bindings.local.json`. A line saying `no reaction in oIO` for every candidate
   of an action means that action needs a key added to `tools\reference\auto_bindings.json`.

**Each session:**
`powershell -ExecutionPolicy Bypass -File tools\reference\auto_record.ps1 -All`
(or `-Suite M2,W1`, or `-Scenario W1_attach_hang`)

- Before each suite the console describes the spot it needs. Walk the spider there in the game, switch back
  to the console and press Enter. The runner then focuses the game window itself. Answer `s` to skip a suite,
  or `q` to quit.
- Then **don't touch the mouse or keyboard** until the console says the suite is done.
- **Emergency stop: F12 or Pause/Break.** Alt-Tabbing away or moving the mouse also stops the run.
  Every held key and button is released on any stop.

| Suite | What it does (scenario IDs) | Spot needed |
|---|---|---|
| **M2** | walk right 4 s, stop 2 s, walk left 4 s, stop 2 s (`M2_walk_flat`) | flat solid ground, ≥ 15 spider-lengths clear to the right |
| **W1** | aims straight up, shoots, hangs 8 s, releases, lands (`W1_attach_hang`) | ceiling/branch 4–8 spider-lengths straight above |
| **W2** | swings at 45° right and left (`W2_swing`); releases at the bottom of the swing, 3× (`W3_cut_mid_swing`); attaches at ~20° and holds retract 3 s (`W4_pull`) | ceiling reachable at 45° both sides, open space below; then a wall ~6 lengths away at ~20° |
| **W3** | weaves a permanent web, watches it, loads it (`W5_weave_line`); weaves and jerks it to provoke a snap (`W5_weave_snap`) | a wall 4–8 lengths away at ~20° up-right, open space to the left |

For each scenario the runner:
1. waits until the spider is controllable and at rest;
2. records automatically (same files as `record.ps1`, in `DesktopPlaygroundLocal\captures\`);
3. checks that every input reached the game, that the expected things happened (shot, attachment, release,
   landing, new web), that frames are consistent (no respawn, no room change), and that the capture passes the
   quality gate.

It retries transient failures once. Failed recordings are **kept** in `DesktopPlaygroundLocal\captures_failed\`
together with `auto_run.json`, which explains why they failed. They are kept out of `captures\` so they don't
break the test run.

At the end it runs the full test suite and writes `DesktopPlaygroundLocal\auto_runs\<time>\report.md`, which
contains:
- per scenario: pass or fail, the reason, measured input latency (frames), and observations (walk plateau and
  stop times, attach and release frames, the release mode, aim accuracy);
- the relevant test results.

Send me that report. Exit codes: 0 all good, 1 failures (see the report), 2 not ready / not started, 3 aborted.

The runner sends inputs through Windows exactly like a real keyboard and mouse. It never writes to the game's
memory. Timing is measured from what the game itself saw, and is reported, not assumed.

## Next recordings: M2, W1, W2, W3 (short version, manual)
Before each recording: be in a level on **solid ground** (`-Check` says READY and `support=ground`), away from
existing webs. Then run `record.ps1 -Scenario <ID>`, press F9, do the steps, press F9, then Ctrl+F12, and check
it prints `OK`.

| ID | Do this |
|---|---|
| **M2_walk_flat** | On long flat ground: hold **right** 4 s, release 2 s, hold **left** 4 s, release 2 s. |
| **W1_attach_hang** | Under a ceiling about 4–8 spider-lengths up: shoot one web straight up and hang without input for 8 s. Then release it and let the spider fall to the ground. |
| **W2_swing** | Shoot a web diagonally ahead (about 45°) and swing with no input for 8 s, then release. Repeat once on the other side. |
| **W3_cut_mid_swing** | Shoot diagonally, swing 3 s, **release** the web at the bottom of the swing, and land. Repeat 3 times. |

These check:
- that the web-shot projectile follows the recovered code exactly (velocity, position, 35 px/step launch speed);
- free fall after release;
- web geometry and stretch rules;
- walking speed.

The probe also confirms it saw every shot (`frames with a shot NOT captured` must be 0).

## Quick procedure (every session)
1. Start Webbed from Steam, load your save and **enter a level** so the spider is on screen and
   controllable. The title screen and menus contain no player.
2. In a terminal in `desktop-playground\`, run:
   `powershell -ExecutionPolicy Bypass -File tools\reference\record.ps1 -Check`

   It must end with **READY** (green), and show `oPlayer: 1`, a `player body: … support=…` line and a
   `fast lane: … ms` line (must be well under 8 ms). If it says **NOT READY**, follow the warning (usually
   "enter a level"). If you are in a level and it still says NOT READY, send me the output.
   `support=web` means the spider is standing on a web. Movement scenarios (M*) need `support=ground`.
3. Record a scenario (repeat for each ID below):
   `powershell -ExecutionPolicy Bypass -File tools\reference\record.ps1 -Scenario <ID_name>`
   - Click back into the game, press **F9** (you hear a beep), perform the steps, press **F9**.
     The terminal prints a status line every 2 s, and on stop: rows, rows with player, skipped frames.
   - You may do several F9…F9 attempts. Finish with **Ctrl+F12**.
   - The capture is validated automatically. It prints duration, rate (Hz), frame coverage, skipped and
     torn percentages, and must end with `OK`. A capture is usable only with ≥ 90% coverage and ≤ 5% torn
     rows. If it prints `UNUSABLE: …`, delete that folder and record again; tell me the reason if it repeats.
4. When done, check everything and run the tests:
   - `powershell -ExecutionPolicy Bypass -File tools\reference\record.ps1 -Validate`
   - `python -m unittest discover -s tests -v`

   Tests **skip** only for scenarios you haven't recorded. They **fail** if a recorded capture is unusable
   (with the reason) or if the original game disagrees with the recovered model.
   Delete unusable session folders from `C:\Users\profe\DesktopPlaygroundLocal\captures` before re-running.

Short recordings are fine. For M3, a few seconds with one or more jumps is enough, as long as each
jump starts and ends on the ground during the recording.

## Recording a scenario
For each scenario below, set up the starting situation, then do step 3 above with the exact ID shown,
for example `M3_jump_in_place`. Use your normal controls and default settings. Avoid pausing or opening
menus while recording; pause between attempts with F9 off. When a step says "do nothing", don't touch any input.

### Movement
| ID | Start | Steps (F9 to start, F9 to stop) |
|---|---|---|
| **M1_stand** | Spider resting on **flat, level** ground, no webs attached | Do nothing for 5 s. |
| **M2_walk_flat** | Flat ground with ≥ 15 spider-lengths of clear space to the right | Hold **right** for 4 s, release, wait 2 s (marker). Then hold **left** for 4 s, release, wait 2 s. |
| **M3_jump_in_place** | **Solid** flat ground (not a web line; `-Check` must say `support=ground`), away from webs, standing still | Tap **jump** once. Wait until landed and still (≈2 s). Repeat 5 times. |
| **M4_jump_moving** | Flat ground | Hold **right**; after 2 s tap **jump** while still holding; keep holding until 1 s after landing. Repeat 3 times. |
| **M5_slope** | Bottom of a clearly sloped surface | Hold toward the slope (walk up) for 3 s, marker, release and let the spider settle or slide for 3 s. |
| **M6_fall** | Spider on a ledge at least ~3 spider-heights high, no webs | Walk off the edge and fall to the ground without touching anything. Repeat 3 times. |

### Webs
| ID | Start | Steps |
|---|---|---|
| **W1_attach_hang** | Under a ceiling or branch about 4–8 spider-lengths above, on the ground | Shoot one web straight up to the ceiling (marker at the shot). Lift off/hang so the spider dangles; hands off for 8 s. |
| **W2_swing** | As W1, but attach the web diagonally (about 45° ahead) | Attach, then let the spider swing with **no input** for 8 s. Repeat once with the web on the other side. |
| **W3_cut_mid_swing** | As W2 | Attach, let it swing 3 s, then **cut/release** the web at the bottom of the swing (marker at the cut). Wait until landed. |
| **W4_pull** | On the ground, attach a web to a fixed surface ~6 lengths away | Use your **pull/retract** control for 3 s (marker when pull starts), release, wait 2 s. |
| **W5_web_bridge_load** | Build a horizontal web line between two surfaces (your weave/line control) | Walk onto the web line, stand on it 3 s, jump on it 3 times. Hard jumps may snap it; that's useful. |
| **W6_pluck** | A built web line you can reach | Walk on or pluck the web so it makes musical notes (marker before each pluck), 5 times, on 2 lines of different lengths if possible. |
| **W7_move_object** | Next to a small movable object (leaf, ball, crate) | Attach a web from the object to a wall and pull it 2–3 times; marker at each pull. |

### Skateboard
| ID | Start | Steps |
|---|---|---|
| **S1_skate_ride** | Next to a skateboard (any area that has one; the Skate Park if unlocked) | Get on the board, ride right for 4 s, marker, ride left 4 s, jump off. |
| **S2_skate_web** | Riding a skateboard | Attach a web to something ahead and let it pull the board for 3 s; release. |

## After recording
- Send me the output of `-Validate` and of the test run. The captures stay on your machine.
- If a step was impossible in your save (no skateboard, no slope, …), skip it and tell me.
