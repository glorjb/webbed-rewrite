# Desktop Playground — P1: Reverse engineering the original Webbed

Target: Steam build **7679547**: `webbed.exe` SHA-256 `c63cecb6…16dde`, `data.win` SHA-256 `6caf485d…5398c`.
All addresses are valid **only** for this build.

**Status (2026-10-11, updated): P1 in progress; P2 not started.** The core physics, web mechanics, VFX tables,
sound mappings and frame order are recovered from the binary with evidence.
**Validated against the running original** (one M3 capture, see "First physics validation"): free flight
with the scripted quadratic air drag, ground-jump velocity, and head-bump restitution. **Everything else is
still unvalidated**: walking (M2), the web shot and grapple (W1/W2), and permanent webs (W3/W5) have no
captures yet. An automated, state-aware recorder now exists for them (section 11). It has been tested only
against a mock game; **live validation on Windows is pending.**
Nothing in this document is claimed to be behaviourally faithful until the corresponding capture tests pass.

### Confidence levels
- **Verified:** read from the binary and confirmed by an independent path, either a second artefact
  (data.win, runner getter, Ghidra) or a hand decode of the assembly.
- **High:** read from compiled code by the lifter, unambiguous, with no second check yet.
- **Medium:** the value was read, but its meaning or the guarding condition is partly inferred.
- **Unknown:** not recovered yet.

---

## 1. Method and tools
| Tool | Purpose |
|---|---|
| UndertaleModTool CLI 0.9.2.0, Ghidra 12.1.4 (both SHA-256-checked against official release digests) | asset export; runner decompilation |
| `tools/recon/yyc_labels.py` | 3,558 GML functions, 2,551 built-ins, 256 built-in variables, 1,904 variable IDs mapped |
| `tools/recon/yyclift2.py` | **structured** YYC → pseudo-GML lifter (CFG, cold-path pruning, if/else/loop/with, short-circuit `&&`/`||`, arrays, x87/SSE float tracking, intrinsics, typed operator wrappers, returns, `++`/`--`) |
| `tools/recon/yycdis.py` | annotated disassembly (used to hand-verify every Verified item) |
| `tools/recon/ghidra/ApplyYYCLabels.java` | pushes labels into Ghidra; decompiles by name, `@addr` or `in0xADDR` (containing function) |

Lifted pseudo-GML and Ghidra output are derived from the game's code, so they stay **local**
(`C:\Users\profe\DesktopPlaygroundLocal\work\gml2\`). The repo contains only tools, facts and tests.

**Lifter validation.** Every physics-critical line below was decoded by hand from the assembly and compared
with the lifter output. Doing this found **five lifter bugs**, all fixed:
- a dropped `&&` operand;
- a vtable call made through a register;
- a stale result after a compound operator;
- RValue type-dispatch branches treated as GML ifs;
- a variable-reference spill that hid assignments.

The lifter still shows `?` where a value can't be tracked, and it never invents values.

### YYC runtime helpers identified (selection)
| Address | Meaning |
|---|---|
| `0x402130/0x401FF0/0x402310` | RValue assign / copy |
| `0x40AC40` `*=` · `0x40DD50` `+=` · `0x410590` `-=` · `0x41FC50` `/=` · `0x41D3A0` mod · `0x4163B0/0x4394A0` `+=` (double) | compound operators |
| 20+ wrappers (e.g. `0x421770`, `0x463650`, `0x447DE0`, `0x439160`) | binary ops; operand types (RValue / int / double / float) read from the wrapper |
| `0x11C59B0` | compare → -1/0/1 (-2 = incomparable); wrappers `0x433A90 ==`, `0x42F6F0 <=`, `0x5F1F00 <`, `0x948680 >=`, `0x470D80 >`, `0x5F1D40 >`(double), `0x412E60 ==`, `0x45FCE0 <`(double) |
| `0x1184D10 / 0x1184DA0` | `lengthdir_x / lengthdir_y`: **float32**, \|cos\|/\|sin\| < 1e-4 snapped to 0 |
| `0x1185380` | `point_direction` (float32) |
| `0x1184830 abs` · `0x11854D0 sign` · `0x1185450 random` · `0x1185470 a+random(b)` · `0x1184E40 max` · `0x11850E0 min` · `0x13F7220 fmod` | intrinsics |
| `0x11DA980` | direct `draw_sprite_ext(sprite, subimg, x, y, xs, ys, rot, col, alpha)` |
| `0x119C270 / 0x119C630 / 0x119C130` | `with` iterator begin / next / end |
| `0x11A1200 / 0x11A1210 / 0x117B170` | array element read / length / copy-on-write element write |
| `0x4056D0` | operator type-error reporter; first argument is the operator (`"++"`) |
| `@@This@@`, `@@Other@@` | resolved via their `{name*, fn}` call slots |

---

## 2. Runtime and frame order
### Per-frame order: runner `DoAStep` (`FUN_0122C880`, decompiled)
1. For every active instance: `xprevious = x`, `yprevious = y`, then advance `image_index`; Animation End fires here (`FUN_0122D400`).
2. **Begin Step** (event 3, subtype 1).
3. Async networking handler.
4. **Alarms** (`FUN_0130BA90`: loops 12 alarms, event type 2).
5. Keyboard (`FUN_012F24B0`), then gesture/mouse (`FUN_0130DE00`, `FUN_012F2560`). *Medium:* identified from the call shapes.
6. A time-based handler (`FUN_01287160`, probably timelines). *Medium.*
7. **Step** (event 3, subtype 0).
8. Sequence/broadcast events (`FUN_0127C430`).
9. **Motion / physics** (`FUN_0122D260`). If the room has a physics world, Box2D **replaces** classic `hspeed/vspeed` motion.
10. HTTP async.
11. Classic collision events, **only when there's no physics world**.
12. **End Step** (event 3, subtype 2).
13. Frame counter `[0x1679CB8]++`.
14. Draw, in a separate function.

**Consequence (Verified):** forces applied in `oPlayer_Step_0` / `oWeb_Step_0` are integrated in the **same frame's**
Box2D step. `oPlayer_Step_2` / `oWeb_Step_2` (End Step) see post-step positions.

### Physics stepping
- Stepping is `FUN_01250870`, decompiled:
  `dt = 1/update_speed; for (s = update_speed/room_speed; s > 0; s--) b2World::Step(s<=1 ? s*dt : dt, iters, iters)`.
  **Verified.**
- `CPhysicsWorld` constructor defaults are iterations = 10, update speed = room speed, gravity (0, 10).
- Webbed's `oGameManager` Room Start sets:
  - iterations to **20 in `room_SkatePark`, 10 elsewhere**;
  - gravity to (0, 0) and pauses physics;
  - `alarm[0] = 2`, which unpauses physics and sets gravity to (0, 10).

  **High.**
- **Normal room:** one step of 1/60 s, 10 velocity + 10 position iterations, gravity (0, 10) m/s², 0.03 m/px,
  and physics paused for the first 2 frames.
- **Units (Verified from getters):** `phy_speed_x = body.linvel.x / m_per_px / room_speed`, so `phy_speed` is in
  **px per step**. `phy_position = body.pos / m_per_px`.

### Memory layout used by the read-only probe (all Verified from runner getters)
| Item | Location |
|---|---|
| Current room / room index / frame counter | `[0x1679C68]` / `[0x1679C90]` / `[0x1679CB8]` |
| Active instance list | `room+0x80` head; `CInstance+0x148` next; skipped if `(+0x80 & 3) != 0` |
| `id` / `object_index` / `x`,`y` (float) / `image_angle` / `image_xscale` | `+0x84` / `+0x88` / `+0xB8`,`+0xBC` / `+0xAC` / `+0xA4` |
| Box2D body | `CInstance+0x74` → `[0]` = `b2Body*`: pos `+0x0C`, angle `+0x48`, linvel `+0x50`, angvel `+0x58` |
| Physics world | `room+0xB4`: update speed `+0x58`, iterations `+0x5C`, m/px `+0x60` |
| Instance variables | `CInstance+0x2C` → `CHashMap<int,RValue*>`: `[0]` size, `[4]` elements of 12 B `{RValue*, key, hash}`, hash 0 = empty |

## 3. Player body (`oPlayer_Create_0`): High, cross-checked with OBJT where possible
- Circle radius **11 px**, density **0.5** (the editor's 0.55 is unused), restitution **0.15**
  (Verified against OBJT), linear damping **0**, friction **1.0**.
- **`phy_bullet = 1`, `phy_fixed_rotation = 1`:** the body never rotates, so the spider's tilt is purely drawn.
- `buoyancy = 1.1`, `material = 4`, `aim_assist_dist = 15`, `spawn_timer = 10`. Checkpoint = the nearest `oCheckpoint`.
- Mass is derived by Box2D: 0.5 · π · 0.33² ≈ 0.171 kg.
- **Air drag** (`oPlayer_Step_0` L1575–1583): `phy_linear_damping = 0` every frame, plus a force of
  `drag · s²` N opposite the velocity, where `s` is speed in px/step and `drag` is 0.005, or 0.02 with a
  parachute. **Verified** against the M3 capture: 367 frame pairs, residual RMS 0.000005 px/s.

## 4. Tuning constants (`init_spider_variables`): High
- **Ground movement:** walkforce 5, max_walkspd 3.5 px/step, max_slipspd 2, jump_power 4 px/step, coyote_time 5 frames.
- **Webs:** max_gdist 600, max_weave_dist 1000, web_shot_speed 35, web_weave_speed 35, webshot_delay 15,
  pull_power 5, weave_pull_power 2.
- **Web walking:** webwalk_spd 3, webwalk_force 6.
- **Swimming:** swimforce 2, max_swimspeed 3.
- **Shooting:** recoil 10, shoot_time 10.
- **Animation:** tilt_acc 0.2, footstep_delay 4, web_alpha 0.95, `web_colour` white.

## 5. Player movement (`oPlayer_Step_0`, ~1,700 structured lines; partially read)
- **Helper scripts (Verified by hand):**
  - `xoff(x,y,a) = x·dcos(a) − y·dsin(a)` and `yoff(x,y,a) = x·dsin(a) + y·dcos(a)`;
  - `rlerp(a,b,t) = (a + angle_difference(b,a)·t + 360) mod 360`;
  - `alt_darccos(x) = darccos(clamp(x,−1,1))`.
- **`rclamp`:** angle clamp via `normalise_angle`. *Medium:* comparison operators not hand-verified.
- **Walking:** `physics_apply_force(phy_com_x, phy_com_y, xoff(io.hm·walkforce·2, 0, ground.image_angle), yoff(…))`.
  The speed target is `xoff/yoff(max_walkspd·io.hm, 0, rclamp(ground_angle, −45, 45))`. *High (calls); Medium (gating conditions).*
- **Clinging:** `physics_apply_force(phy_com_x, phy_com_y, 0, −10·phy_mass)`, which exactly cancels gravity. *High (call); Medium (condition).*
- **Air torque:** `physics_apply_local_force(0, −16, io.hm·2.5, 0)`. **Swimming:** `physics_apply_force(…, io.hm·swimforce, clamp(io.vm − io.jmp_held, −1, 1)·swimforce)`.
- **Jump (L1323–1327):**
  - direction `rlerp(rclamp(io.mvdir, n−45, n+45), n, 0.5)` with `n = ground_angle + 90`;
  - per-axis velocity `max2(jump_power·jump_allowed (+ ground velocity), current)`;
  - plus impact particles.

  *High*, except `max2`'s exact semantics, which are *Medium*.
- **Ground velocity matching:** `lerp(phy_speed, ground.phy_speed, 0.2)`. *High.*

## 6. Webs (`oWeb_*`, `create_web`, `snap_web`, `reset_web_tracking`)
**Webs are not Box2D joints.** A whole-program scan shows joints only in level props. **Verified.**

| Rule | Recovered form | Confidence |
|---|---|---|
| Segment state | `oWeb_Create`: pull_power 1, stretch 5 (overridden, see the next row), snap_threshold 3, breakable 1, fade_time 120, anchored 1 | High |
| Stiffness from rest length | `reset_web_tracking`: **`stretch = max(length/20, 1)`**, **`snap_threshold = 2 + max(0, 5 − stretch)`**; also resets `dir`, `current_length`, `last*` | High |
| Endpoint attachment (End Step L42–45) | `ox = o.x + lengthdir_x(odist, o.image_angle − odir)`, `oy = o.y + lengthdir_y(…)`; same for `a` (float32 lengthdir) | **Verified** (hand decode) |
| Length / direction (End Step L47–50) | `dir = point_direction(ox,oy,ax,ay)`, `current_length = point_distance(…)` | High |
| Tension (Step L2–L34) | if `a != o`: `s = current_length − length`, `r = s/stretch`; if `r > 0`: `f = pull_power · min(1, min(1, r))`; if `f > 0.1`: inside `with(o)` and `with(a)`, apply `physics_apply_force` at the endpoint with `lengthdir(f, dir)` (and the opposite direction at `a`) to instances where `!(web.alarm[1] > 0) ‖ object_index ∈ {oWebNode, oWebNode_phantom}` | **Verified** for L2–L34 (hand decode); the `a`-end direction is *Medium* |
| Node keep-alive | in `with(o)`/`with(a)`: if `object_index == oWebNode && attached_webs <= 1 && other.alarm[1] <= 0` then `other.alarm[1] = other.fade_time` | High |
| **Break by jerk** (End Step L56–59) | buffers `b3←b2←b1←current_length`; `d2 = b2−b3`, `d1 = b1−b2`; **snap if breakable ∧ \|sign(d2)−sign(d1)\| > ε ∧ \|d2\| > 4 ∧ \|d1\| > 4**: a length reversal with both per-frame changes over 4 px | **Verified** (hand decode) |
| Break by over-stretch (Step L118–125) | if `a == …` and the web is not in `oPlayer.web_collisions` and breakable: `instance_destroy`, or `snap_web` if `current_length > ref · snap_threshold` | Medium (`ref` operand not resolved) |
| Re-anchoring (End Step L3–34) | when `parent_web` exists and both `collision_line`s are clear, re-links to a new `oWebNode`; if an attached instance no longer exists, creates an `oWebNode_phantom` at that end and sets `anchored = 0` | High |
| `snap_web(web)` | cut at t = 0.5: new `oWeb` and `oWebNode` at the midpoint; far end moved to the new web; `length` and `current_length` × 0.5; fragments shorter than 20 px are destroyed | High (hand-checked) |
| Slack dynamics (Step L54–L73) | `slack_v += r·slack…`, `clamp(slack_v, −1, 1)`, `slack += slack_v`, `clamp(slack, −1, 1)`, `avg_slack_v = lerp(avg, slack_v, 0.5)`, zeroed when \|slack\| and \|slack_v\| < 0.01 | Medium (lifter block order differs from source order) |
| Attachment counters | `a.attached_webs++`, `o.attached_webs++` once per web (`marked_a/o`) | High |

## 6b. Web shot and grapple (`oPlayer_Step_0`): a separate system from permanent webs
The player has **two independent web systems**:
- **Grapple:** a web-shot projectile, then a force the player applies to itself.
  - State lives on `oPlayer`: `gx, gy, gh, gv, sx, sy, slack, retract, webshot_node`.
  - The projectile is an `oWebshotNode`.
  - No `oWeb` segment is involved in the pull.
- **Permanent webs:** `oWeb` segments between `oWebNode`s, with their own tension and snap rules (section 6).

Do not merge the two into a generic rope.

| Rule | Recovered form | Confidence |
|---|---|---|
| Firing (L183–L219) | `gh, gv = lengthdir_x/y(web_shot_speed, point_direction(sx, sy, crosshair_tx, crosshair_ty))` (px/step, so launch speed 35 px/step); `retract = 0`; `slack = 0.75`; `webshot_node = instance_create_layer(gx, gy, "Webs", oWebshotNode)` | High (lifted); not yet checked against a capture |
| Shot motion (L912–L916) | every Step while the shot exists: `webshot_node.phy_position = (gx, gy)`, `webshot_node.phy_speed = (gh, gv)`. `oWebshotNode` is a **kinematic** body (OBJT, Verified), so the frame's physics step moves it by exactly `(gh, gv)` px | High (lifted); the capture tests `test_shot_body_follows_shot_state` / `test_shot_launch_speed` are ready |
| Grapple pull structure (L681–L729) | if `d > 16`: `w = sqr(1 − clamp((d − 10)/64, 0, 1))`, `c = min(max(5, |v|), d/5)`; `F = lerp(lengthdir(P, dir_g), lengthdir(c, dir_g) − v − (0, 10·phy_mass), w)`; `physics_apply_force(phy_com_x, phy_com_y, F)`. `d = distance_to_point(gx, gy)` measures from the **bounding box**, `dir_g = point_direction(phy_com, g)`, `v` in px/step | High (structure) |
| Pull strength `P` (L661–L677) | the weave branch starts with `weave_pull_power · clamp(d/10, 0, 1)` (L661, L666 hand-verified); the further terms on L667–L677 are **unresolved**; `pull_power = 5` exists in `init_spider_variables` | **Unknown** |
| Near-anchor regime | `d ≤ 16`: no pull force from this block | High (structure) |
| Attachment / release transitions | which variable flips on attach (`attached`, `grapple`, `webshot_node`), and whether release happens on button-up or by toggle | **Unknown.** The automated W1/W2 runs log every change of these variables frame by frame and record the observed release mode |
| Aim point (live, 2026-10-11) | `player.crosshair_tx/ty` stayed constant while the cursor moved (the firing code reads them, so they are probably written only when firing; not verified), and `aimx`/`aimy` are not variables of `oIO`. Neither is used as feedback any more. The runner aims with **sighting shots**: each shot's launch vector `(gh, gv)` (or `(gx − sx, gy − sy)`, if `gh/gv` are cleared on attaching) gives a screen-space ray through the cursor position. With a uniform-scale view, ≥ 3 rays meet at the shot origin's screen position (least squares, with the residual as a check), and every real shot's direction is verified afterwards. `-AimScan` dumps every `oIO`/`oPlayer` variable against the cursor to find a real tracking variable, if one exists | **Unknown** (aim variable); the sighting method relies only on the lifted firing formula (High) plus the view assumption, which the residual tests |
| Relative-mouse aim (live -AimScan, 2026-10-11) | The cursor is **locked**: Webbed recentres it every frame (fixed point [1280, 800], the window centre) and aims from mouse deltas. Variables that changed with relative motion: `io.aimdir, io.gmx, io.gmy, io.mx, io.my, player.abx, player.aby, player.aimx, player.aimy`. Their meaning is **measured, not decoded**. `-AimScan` sends single delta impulses: an absolute move to "lock point + d" (exact pixels, no pointer acceleration) and relative moves. For every variable it fits the gain per pixel, whether the change persists (integrator), and the lag. It checks whether `io.aimdir` equals the direction from the player to a candidate pair (point) or of the pair (vector), and saves a relative aim profile. `RelativeAim` steers that pair; every real shot is still direction-checked | **Medium** once a live profile exists (measured relation); the decode of `oIO_Step_1` / `oPlayer_Step_0` aim lines is still to do |
| Retract / pull control | Live calibration (2026-10-11): none of SHIFT, S, CTRL, DOWN, MMB changed `oIO.retract`. `retract` has one global variable ID (101181), but YYC IDs are per *name*, so that does not show `oIO` owns it, and the probe returns only variables that exist on the instance. The player's `retract` is reset to 0 on firing (L183–L219); the pull-strength block (L661–L677) branches on **weave** | **Unknown.** Hypotheses: (a) `oIO` has no `retract`; (b) pulling = grapple + weave. `W4_retract_probe` measures each candidate key's effect on `oIO`, player variables and rope length while a grapple is held |

`tools/reference/analyze_grapple.py` measures `P` from W* captures without decoding it:
1. It inverts Box2D's semi-implicit Euler step over each hanging frame pair, with no horizontal input and no
   contact.
2. It subtracts gravity and, under hypothesis A, the verified air drag.
3. It solves the structure above for `P`. The component perpendicular to `dir_g` must be about 0 if the
   structure is right.

It reports how `P` depends on `d` and `|v|`, and the residual of each candidate formula. Synthetic data generated
with a known `P` is recovered exactly (`tests/test_analyze_grapple.py`). Three sensitivities need care:
- `d` depends on the player's mask bounding box, which isn't recovered (`--bbox-offset`).
- Whether the air drag also acts while grappling is not verified (hypotheses A and B).
- Frames with `w ≈ 1` carry no information about `P`.

## 7. Procedural spider animation (`draw_spider`, ~820 structured lines; partially read)
- **Per-skin geometry** (`set_spider_skin`, 7 skins, **High**):
  - legspan 30/30/44/50/50/38/70
  - hipspan 10×6/14
  - abradius 8/9/8/9/8/8/8
  - abheight 3/−3/3/−1/−1/3/3
  - bent_legs 0/0/0.5/0.75/1/0/1
  - head_top_move_ratio 0.4/0.4/1/0.75/0.75/1/1
  - head_top_offset 0/0/2/−2/2/1/0
  - a 9-sprite set per skin (head, head_eat, head_top, abdomen, leg, pedipalp, blob, blob_eyes, blob_eat)

  Default skin is 0.
- **Draw order:** parachute leaf → web nodes → baby spiders on the abdomen → abdomen (frame 1, then 0) →
  legs → head (with a `merge_color` hurt flash) → hats → pedipalps (mirrored, yscale ±1).
  All drawn with direct `draw_sprite_ext`. *High.*
- **Legs:**
  - foot target distance `d = min(legspan, dist(hip, foot))`;
  - knee angle from `alt_darccos` (law of cosines);
  - upper segment drawn at `dir + knee`, lower segment from the knee to the foot;
  - a straight single segment instead when `|angle_difference| > 100`;
  - four `foot_mode` states, a `feet[]` array of foot positions, and `feet_up` alternation.

  *High (structure); **Unknown** (exact segment-length arithmetic):* inline float math that the lifter
  loses across loop joins, and Ghidra times out on this 288 KB function.
- **Pedipalps:** `pedreach` approaches 6 at rate 0.5, 3 at 0.25, 4 at 0.1, or 0 at 0.1 depending on state;
  `pedreach_angle` follows with rlerp at 0.5 / 0.25 / 0.1. **Head:** `lerp(head, abdomen, 0.8)`.
  **Tilt:** rlerp at 0.2 / 0.75. *High.*
- **Footsteps:**
  - every `footstep_delay` (4) frames while walking and not slipping;
  - `array_random(footsteps)` played at the foot position, falloff 32–64, gain `clamp(speed, 0.15, 1)`;
  - pitch 0.5 on certain surfaces;
  - on a web (`foot_mode == 2`), the web is plucked: `slack_v = 0.025·random_sign()`, `pluck_string = 1`.

  *High.*

## 8. VFX and SFX
- **Particles** (`oParticleEffectsRenderer_Create_0`, 0 unresolved values, **High**): 18 types with complete
  colour, alpha, sprite, speed, direction, gravity, orientation, size, life and death parameters:
  - door_sparkle;
  - sparkle_pink/green/teal/yellow (`sGemSparkle`);
  - confetti (`sConfetti`);
  - pollen, pollen_dust and dust (`sDust`);
  - splinters, splinters2;
  - mud_splash/drip/bubble (`sMudSplash`);
  - dirt (`sDirt`);
  - feathers (`sBirdFeathers`);
  - spark (`sSpark`);
  - puff_cloud (`sDeathParticle`).

  The full table is in the local lift and will be transcribed into the reconstruction.
  Particles are drawn and updated manually through `global.ps_effects`.
- **Impact SFX** (`check_impact` + `oGameManager_Create`, **High**):
  - `Δv = |v_prev| − |v|`; when `|Δv| > 1`, pick `array_random` from an **8×8 material-pair grid**;
  - falloff 64–300, gain `clamp((|Δv|−1)/5, 0, 1)` or `clamp((|Δv|−4)/10, 0, 1)`;
  - impact particles at the contact point;
  - screen shake `max(0, |Δv|−5)/2` for the player.

  Grid sets:
  - default: `aDirtImpact_01–06`;
  - material 0: `aThudBright1–3`;
  - other pairs: soft `aSoftImpacts_01–04`, mud `aMudImpact_01/03`, wood `aWoodOnWood_01–03`,
    `aSolidOnWood_01–03`, stone `aStoneOnStone_01–04`, metal `aMetalOnMetal_01–03`.

  The player is material 4. *The grid-creation call is mislabelled by the lifter (`ds_grid_create` inferred); Medium.*
- **Web SFX (Verified IDs):**
  - cut: `aTwangSnap`, pitch `clamp(1/(max(len,1)/75), 0.01, 100)`, falloff `current_length·1.5` to `·3`;
  - pluck: `aTwang` at the midpoint, pitch quantised to the **48-step tuning table** (0.25…3.77), gain
    `clamp(avg_slack_v·2, 0, 1)`, then smoothed (pitch lerp 0.25, gain lerp 0.1, cap 0.5).
- **Player SFX:** death `aCartoonyDeath` with 10 `pt_puff_cloud`, angular velocity `sign(tilt_momentum+vx)·500`, and respawn after `alarm[0] = 75`; respawn `aPopMouth1`. *High.*
- **Shaders:** 7 recovered as source. *Verified.*

## 9. Reference capture and regression tests (M3 recorded; M2/W* pending)
- `tools/reference/webbed_probe.py`:
  - **read-only polling**: `PROCESS_VM_READ` only, no debugger, no injected code, attaches only to this build's hash;
  - frame-synchronised to the runner's own frame counter, so it samples once per frame at 60 Hz;
  - records player body and state, `oIO` input state, all webs (including break buffers), nodes and skateboards;
  - hotkeys: F9 start/stop, F10 marker, Ctrl+F12 save;
  - launcher: `tools/reference/record.ps1`.
- `docs/REFERENCE_TEST_PROCEDURE.md`: the numbered scenarios M1–M6, W1–W7 and S1–S2.
- `sim/webbed_model.py`: the recovered rules as testable Python.
- Tests (`python -m unittest discover -s tests`):
  - 15 model tests pin the recovered formulas; **all pass**;
  - 9 capture tests (an inventory check plus 8 comparisons against the original): world config, free-fall gravity, walk plateau, jump
    speed, web length/dir, endpoint attachment, break rule, stretch rule. They **skip** until captures exist.

  Forces are not directly observable read-only (Box2D clears `m_force` after each step). They are derived
  from Δv, mass and gravity in analysis.

## 10. Still unverified / unresolved
1. **Only M3 is captured:** every runtime claim outside free flight, jumps and restitution is unvalidated
   against the running game. M2 and W1–W5 are automated (section 11) but not yet recorded live.
   - **Grapple pull strength `P`** (L661–L677) and the grapple attach/release state transitions (section 6b):
     `analyze_grapple.py` will measure `P` from W* captures, and the decode must then agree with it.
   - **The retract / pull control** (section 6b): no key produced `oIO.retract` in live calibration.
     Run `W4_retract_probe`, then decode L661–L677 and the `oIO` Step to confirm.
2. `draw_spider` leg-segment arithmetic, plus remaining `?` values in the large events
   (`oPlayer_Step_0` ~215, `draw_spider` ~300) caused by values lost at loop/join points.
3. Branch conditions guarding clinging, walking cap and web pull in `oPlayer_Step_0`. The calls are known; the gates are partly unknown.
4. `max2`, `rclamp`, `raycast_lengthdir` exact semantics; the `ref` operand in the over-stretch snap.
5. Skateboard physics (`oSkateboard_Step_0/1`): lifted but not analysed yet.
6. Box2D/LiquidFun exact version and any GameMaker patches. Fidelity needs matching `b2World::Step`
   (warm starting, TOI, sleep). The layout matches Box2D 2.3 at the offsets used.
7. Keyboard/mouse/timeline handler identities in the frame loop (Medium).
8. The legal review of Webbed's EULA/mod policy is still pending.

### Capture pipeline fix (after the first user recording)
- **Bug:** `webbed.exe` is built with `DYNAMICBASE` (ASLR). The probe read the static addresses for base
  `0x400000`, but the game loaded at `0x2F0000`. It read unrelated memory (frame counter 0), so it never
  wrote a row: `segment_01.jsonl` was 0 bytes. The test loader then silently ignored the empty session and
  reported "no capture".
- **Fix:** the module base comes from a read-only Toolhelp module snapshot, and every static address is rebased.
- **Verified live** on the running game, at the title screen:
  - frame counter advancing at 60 fps;
  - room index 18 = `room_Hub`;
  - physics world read as speed 60, iterations 10, 0.03 m/px, matching the recovered values;
  - `oIO` found;
  - a timed 3 s capture gave 180 consecutive rows with 0 gaps, 0 torn rows and 16.67 ms mean spacing,
    all schema-valid.

  **Not yet verified live:** reading the player and its Box2D body, because there's no player on the title screen.
- **Diagnostics added:**
  - attach report: counter advancing, room name, instance and player counts, body readability;
  - `READY` / `NOT READY` result from `-Check`;
  - live status while recording, and per-segment rows / player rows / skipped / torn in `session.json`;
  - `validate_captures.py`;
  - tests now **fail**, with the reason, when a capture exists but is unusable, and **skip** only when
    nothing was recorded. A new inventory test fails on any unusable session.
- The first M3 recording contains no data and cannot be used. It must be re-recorded.

### Sampling-consistency fix (after the second M3 recording)
- **Measured in that capture:** 13.9 s, 131 rows (9.4 Hz), 15.8% frame coverage, 84% of frames skipped,
  100% torn. Cause: every row enumerated all ~320 webs and ~185 nodes in `room_Hub` with their variables
  (~10,000 reads, ~106 ms per row, about 6 frames).
- **Old `torn` definition:** the frame counter changed during the row. It was set on every row because each
  row took longer than a frame. It was also insufficient: the counter increments at the *end* of an update,
  so a read overlapping the *start* of the next update went undetected.
- **New design** (still `PROCESS_VM_READ` only, the game is never paused):
  - **Fast lane per frame:** player pose and velocity, state variables, `oIO` input, world, skateboards, and
    up to 8 focus webs within 300 px. About 25 reads and under 1 ms on the title screen; the in-level cost is
    reported by `-Check`.
  - **Slow lane:** all other webs and endpoint nodes in the idle time, on a 6 ms budget, stopping on any
    frame change. Results go to `segment_NN_webs.jsonl`.
- **New `torn`:** the frame-start timestamp (`[0x1679BF8]`, written first in `DoAStep`) and the frame counter
  are read together before and after each row; torn if either changed. Verified live that they strictly
  alternate (start, then about 1 ms later the counter).
- **Live result** on the title screen: 299 rows in 5.0 s (59.8 Hz), 0 skipped, 0 torn, fast lane at most 1.5 ms,
  game frame time 16.67 ms (14.3–18.9 ms). **Not yet measured in a level with the player present.**
- **Same-frame guarantee:**
  - an untorn row's player body, position and input belong to the state between frame *n*'s End Step and
    frame *n+1*'s start;
  - Draw-phase variables (e.g. `tilt`, `foot_mode`, written in `draw_spider`) may already reflect frame *n*'s
    Draw. They are not used by the physics tests.
- **Selection fixes** (assertions unchanged):
  - `landed` is not a "grounded" flag; it stayed 0 while standing on a web. Support now comes from
    `ground` / `webwalk`.
  - Take-offs require the jump input. Web take-offs are counted but not asserted.
  - Free flight excludes frames within 20 px of any recorded web line.
- **Quality gate:** sessions with < 90% coverage or > 5% torn rows are unusable, and the tests fail.
- Another bug fixed: the title screen and hub are both `room_Hub`, so a player spawning later wasn't detected.
  The slow lane now refreshes the player, input and skateboard pointers.
- That second M3 capture was recorded standing on a web, and its quality is below the gate. It can't be used.

### First physics validation (M3 capture `M3_jump_in_place_20261011-024349`)
Capture: 36.3 s, 2,171 rows, 100% coverage, 0 skipped, 0.6% torn. It is preserved unmodified; a read-only
copy is kept in `DesktopPlaygroundLocal\captures_backup` with identical SHA-256.

**Root cause of the 239 `test_free_fall_gravity` failures: one systematic model error.** The model treated free
flight as gravity-only with damping 0. The original applies a **scripted quadratic air drag every frame**,
recovered from `oPlayer_Step_0` and hand-verified in the assembly:

```
L1575  phy_linear_damping = 0
L1576  drag = parachute ? 0.02 : 0.005          // .rdata 0x144E7D8 = 0.02, 0x144E7D0 = 0.005
L1581  s = point_distance(0, 0, phy_speed_x, phy_speed_y)        // px/step, before this frame's step
L1583  physics_apply_force(phy_com_x, phy_com_y, lengthdir_x(s*s*drag, point_direction(vx, vy, 0, 0)), lengthdir_y(…))
```

- Forces are in newtons on the Box2D body (mass = 0.5 · π · 0.33² kg). Integration is Box2D semi-implicit
  Euler: `v += h·(g + F/m)`. The runner adds no other damping, since `phy_linear_damping` is forced to 0.
- **The editor's linear damping of 0.1 is unused:** `uses_physics` is off and the code forces damping to 0,
  and the capture contradicts it.
- **Hypotheses compared** on 367 free-flight frame pairs (`tools/reference/analyze_free_flight.py`,
  plot `docs/analysis/free_flight_residuals.png`):

  | Hypothesis | RMS (px/s) | Max (px/s) |
  |---|---|---|
  | H0 gravity only | 0.10705 | 0.22349 |
  | H1 Box2D linear damping 0.1 (editor) | 0.13093 | 0.16253 |
  | H1 linear damping, best fit 0.045 | 0.02756 | 0.06083 |
  | **H2 recovered drag 0.005** | **0.000005** | **0.00001** |

  A blind least-squares quadratic coefficient gives k = 2.7064e-4 /px. The recovered constant predicts
  k = 0.005 / (60² · m · 0.03) = 2.7064e-4 /px, a 0.00% difference.
- **Jump (8 of 8 jumps):** measured vy on the jump frame is −234.1846 px/s. Prediction: −jump_power (−240) set in
  Step, then one step with gravity and drag = −234.1846. **Exact to 4 decimals.** The game's `ground` variable lags
  take-off by 2 frames; the test now finds the jump frame from `io.jmp` while the previous frame was supported.
- **Head bump (8 of 8):** at the same point of every jump, vy changes −131.32 → +18.85. Box2D restitution
  predicts `0.15 × (−131.3237 + 5.5556 + 0.0778) = 18.8535`; measured 18.8536. This dynamically confirms the
  player restitution of 0.15, and it was correctly excluded from the free-fall test as a contact frame.
- **Model corrections:** `sim/webbed_model.py` adds `PLAYER_AIR_DRAG = 0.005`, `PLAYER_PARACHUTE_DRAG = 0.02`,
  `player_air_drag_accel_pxps2()` and `player_free_flight_dv()`.
- **Test changes:** free fall now checks both velocity components against the full model; the jump test adds an
  exact-velocity assertion; there is a new restitution test. No tolerance was changed: all remain 0.05 px/s.
- **Suite:** 26 tests, all pass; 5 skipped (M2 and W* not recorded yet).
- **Still unverified:** the parachute drag value (0.02) in motion; drag while walking or web-swinging; ground
  velocity transfer in jumps from moving ground; jumps from webs.

### Corrections to earlier statements
- **P1 v1:** "web stretch = 5" is only the Create value; `reset_web_tracking` replaces it with `max(length/20, 1)`.
- **P1 v1:** player flags were unknown; they are `phy_bullet = 1` and `phy_fixed_rotation = 1`.
- **P1 v1 probe:** used a debugger with a hardware breakpoint. Replaced by pure read-only polling to meet
  the no-injection requirement.

## 11. Automated reference recording (state-aware runner)
Goal: record M2 and W1–W5 without manual play, keep the probe read-only, and never claim timing that wasn't measured.

**Architecture** (`tools/reference/autoref/`, CLI `auto_runner.py`, launcher `auto_record.ps1`):
- **State:** the existing probe (`webbed_probe.Game` / `Recorder`, unmodified) is imported, and its frame loop
  is re-created so the runner can act between captures. Rows, torn detection and the slow lane are identical.
  Sessions use the probe's file layout, so `capture_lib` and every existing test read them unchanged.
  Runner-only data goes to `inputs.jsonl` / `auto_run.json`, which the capture loader ignores.
- **Input:** OS-level `SendInput` (keyboard scan codes, mouse buttons, absolute cursor moves). There are no
  memory writes and no injection. Inputs are sent only while the Webbed window is the foreground window.
- **Feedback:**
  - every press and release is acknowledged through the game's own input object `oIO`
    (`hm`, `jmp`, `grapple`, `weave`, `retract`) within 8 frames; otherwise the attempt fails as `missed_input`;
  - acknowledgement latency and the held duration the game actually saw are recorded per input;
  - aiming is closed-loop on the player's `crosshair_tx/ty`: the screen-to-room mapping is measured from two
    probe moves, then refined by Newton steps to within 2°.
- **Readiness:**
  - player and Box2D body present, `oIO` present, not `locked`;
  - no shot, grapple or weave active; not in water, on a skateboard or under a parachute;
  - the required support (`ground` for every scenario);
  - at rest (|v| < 1 px/s for 30 frames).
- **Judgement per attempt:**
  - the scenario's own checks (shot appeared, attached, detached, landed, stayed on ground, new `oWeb` created);
  - frame consistency: counter monotonic, no room change, no respawn (player id change or a jump over 64 px),
    world unchanged;
  - the same quality gate as the tests (`capture_lib.Session`: coverage ≥ 90 %, torn ≤ 5 %, valid rows).
- **Preservation:**
  - failed attempts are moved to `captures_failed/` with their `auto_run.json`; nothing is deleted;
  - a run may only move sessions it created itself;
  - passing sessions stay in `captures/`, where the tests read them;
  - transient failures (missed input, invalid capture, inconsistency, timeout, aim) are retried.
- **Safety:** every stop releases all held keys and buttons. Stops:
  - F12 or Pause;
  - focus loss;
  - physical mouse takeover;
  - a watchdog thread, if the loop stalls for 0.75 s with inputs held;
  - Ctrl+C, any exception, and `atexit`.
- **Bindings:** Webbed's default bindings are not recovered. Calibration tries candidate keys and keeps the
  first one `oIO` reacts to. The result is stored in `DesktopPlaygroundLocal\auto_bindings.local.json`.

**Scenarios** (IDs from REFERENCE_TEST_PROCEDURE.md): M2 → `M2_walk_flat`; W1 → `W1_attach_hang`;
W2 → `W2_swing`, `W3_cut_mid_swing`; W4 → `W4_retract_probe` (experiment), `W4_pull`, which is skipped while no
retract key is confirmed; W3 (permanent webs) → `W5_weave_line`, `W5_weave_snap`.
- **Inferred, to be confirmed by the first live run:**
  - the grapple "attached" predicate (`attached` or `grapple` set, no shot in flight);
  - the anchor = `(gx, gy)`;
  - the weave gesture (hold weave until the shot ends, then release);
  - the variable meanings of `oIO`.

  Raw variable transitions are logged, so a wrong interpretation is visible in the data.
- **Not automated:** cutting a permanent web (control not recovered); skateboard scenarios.

**Verification status:**
- 77 runner unit tests pass against `autoref/mock.py`, a fake game with crude dynamics. It is **not** the
  recovered physics, and its output is never written to the real capture root. They cover:
  - the happy path and probe-compatible files;
  - missed inputs with retries and preservation;
  - torn and skipped frames;
  - no player;
  - a frozen game;
  - a respawn;
  - no surface; walking off a ledge;
  - a missing binding;
  - toggle-mode release;
  - emergency stop; focus loss; the watchdog;
  - existing captures untouched;
  - calibration;
  - the aim fallback from `crosshair_tx/ty` to `oIO.aimx/aimy`; a clean failure without aim feedback;
  - `-Check` working without focus;
  - a readiness gate that never continues after a failed check and reports the measured speed;
  - calibration explaining a missing `oIO` variable; partial recalibration keeping confirmed keys;
    bindings backup;
  - unresolved bindings skipped, not failed; "nothing recorded" never reported as passed;
  - the retract probe identifying the key that changes the rope (on the mock);
  - a focus-restore failure and an emergency stop while waiting for focus.

**Live status (2026-10-11, owner's PC):**
- Done:
  - attach and the instance-list refresh work;
  - calibration confirmed D and A (1-frame acknowledgement latency), SPACE (2), RMB/weave (1) and
    LMB/grapple (2);
  - the suite reported 53 pass, 0 fail, 9 skip.
- Not done:
  - retract is unresolved;
  - **no gameplay scenario has passed live**;
  - walking, grapple, pulling and permanent-web physics remain **unvalidated**.
- The live provider and the input driver did run against Webbed (calibration and the scenario attempts
  below). Real input latency and hold accuracy will come from the first passing scenarios.

**First live scenario run (2026-10-11): runner failures, fixed in the runner (not in the model):**
- **M2 `setup_invalid` (left the ground).** The scenario walks about 840 px at 3.5 px/step, but its setup text asked
  for "15 spider-lengths". Fixes:
  - a read-only ground scan (`groundscan.py`: solid instances' sprite boxes, an estimate) checks the clear
    distance *before* any input;
  - `M2_walk_short` (90-frame holds, about 380 px on each side) was added;
  - the failure now reports the distance walked, the position, the velocity and the last ground instance;
  - `diagnose_capture.py` summarises the preserved capture.

  The actual cause still has to be confirmed from that capture.
- **W1/W4 `aim_failed`.** No usable feedback variable exists (see the 6b "Aim point" row). Replaced by sighting
  shots plus a direction check on every real shot. `-AimScan` / `-AimSight` diagnostics were added.
- **Second live run (2026-10-11).**
  - `-AimScan`: cursor locked, so sighting cannot work. The relative aim above replaces it for Webbed, and the
    runner now refuses to fire sighting shots when it detects a locked cursor.
  - The ground scan skipped M2 (64 px right / 40 px left, although a real walk went about 296 / 366 px).
    groundscan v2 changes:
    - exact rotated rectangles instead of axis-aligned boxes;
    - steps of up to 11 px are continuous ground;
    - non-box fixtures make the scan `ambiguous`; the runner skips then, unless `-TrustGround`, a marked
      diagnostic mode;
    - `-GroundScan -CompareCapture` tests the model against where the spider really stood, per instance:
      missing class / constant offset / error along x.
  - A physical Escape during readiness aborted the run. Takeover detection is now armed only after focus,
    readiness (including "no menu open / not paused") and setup; F12/Pause work in every phase.
- **Third live run (2026-10-11): start-up blocked.**
  - Readiness refused to start because `oMenuControllerPrompt` and `oSettingsMenu` instances existed. Existence
    is not state: these can be persistent, hidden instances.
  - Focus-loss abort was active in every phase, including readiness and read-only diagnostics, and a
    gameplay-readiness gate ran after `-GroundScan`.
  - Fixes:
    - the pause decision uses only evidence: any instance's `paused` variable, found by an instance scan, or the
      indicator measured by `-StateScan`;
    - `-Check` prints a read-only state snapshot;
    - focus-loss abort only while armed;
    - read-only diagnostics never focus or arm;
    - a single `prepare()` start-up path: announce, non-injecting refocus (4 strategies), hands-off wait, then arm;
    - a walk acknowledged by `oIO` that doesn't move the spider within 30 frames stops as `not_controllable`.
  - The pause/menu GML (`oGameManager` / menu objects) is still to be decoded locally; the `-StateScan`
    indicator is a measurement.
- **False "mouse moved by user".** The detector compared the cursor with where the runner had put it. That breaks
  when the game moves or locks the cursor; it is the likely cause, and `-AimScan` measures it. Takeover now uses
  low-level hooks: the runner's input is tagged (`dwExtraInfo`), and only physical input (or another program's
  keys and clicks) counts. Detection is armed only while the runner owns the game. F12/Pause, focus loss and the
  watchdog are unchanged.
