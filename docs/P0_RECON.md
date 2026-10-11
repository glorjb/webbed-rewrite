# Desktop Playground — P0: Original Webbed recon

Status: **P0 complete; P1 in progress**, see [P1_REVERSE_ENGINEERING.md](P1_REVERSE_ENGINEERING.md).
Everything below is either *verified* (read directly from the installed files by the scripts in
`tools/recon/`) or explicitly marked *inferred* / *unknown*.
Nothing here is a claim of behavioural fidelity yet.

> **P1 updates (2026-10-11).** Several P0 unknowns are now resolved. They are marked **[P1]** inline;
> details and evidence are in P1_REVERSE_ENGINEERING.md:
> - the player IS a Box2D body: an 11 px circle created in code;
> - webs are NOT joints: custom force code;
> - the physics step is 1/60 s with 10 iterations (20 in room_SkatePark);
> - sound mappings `aTwangSnap` and `aTwang` are verified;
> - GameMaker is ≥ 2.3.2 (UTMT).

## 1. Installed build (verified)

| Item | Value | Source |
|---|---|---|
| Install | `C:\Program Files (x86)\Steam\steamapps\common\Webbed` | `um scan` |
| Store | Steam app 1390350, **buildid 7679547** | `appmanifest_1390350.acf` |
| Developer | Sbug Games (exe VersionInfo; FileVersion 1.0.0.0) | webbed.exe |
| data.win build timestamp | **2021-11-09 00:21:47 UTC** | GEN8 |
| Engine | GameMaker Studio 2.3.x era (GEN8 reports 2.0.0.0, bytecode format 17; has TGIN/SEQN/ACRV/TAGS/EMBI/DAFL; no FEAT/FEDS/PSEM/PSYS chunks → predates GM 2022.x/2023.x) | `tools/recon/winscan.py` |
| Exact runner version | **[P1]** UTMT detects GMS **≥ 2.3.2** (GEN8 says 2.3.2.0). The exact runner build number is still unknown; the runner prints `YoYo Games Runner v%d.%d(%d)[r%d]` at startup | UTMT `info`; webbed.exe strings |
| Fingerprint | **[P1]** `webbed.exe` SHA-256 `c63cecb63ed471bc68b2e5a0f90c4a0cf15a57266b7af1e2d80a0b04d0116dde`; `data.win` `6caf485d6027afa3270ef0ab19595b95e137188168d1244a4a5c05bf9e85398c` | sha256sum |
| Compilation | **YYC (native x86)**. data.win has **no CODE / VARI / FUNC chunks**, so there's no GML bytecode to decompile | chunk list |
| Arch | 32-bit (x86) webbed.exe | PE header |
| Game speed | 60 fps (GEN8 float at +708) | GEN8 |
| Window | 1280×720 base | GEN8 |
| Anti-cheat | none; single-player; Steam + GOG Galaxy DLLs present | `um scan` |
| Saves | `%LOCALAPPDATA%\Webbed` | `um scan` |
| Audio groups | `audiogroup_default` (embedded), `_ambience`, `_web_music`, `_music` → `audiogroup1–3.dat` | AGRP |

## 2. What this means for fidelity

**The biggest constraint:** all gameplay logic (movement controller, web creation and cutting, spider
drawing and leg animation, sound triggering, VFX spawning) is **native compiled code**. UndertaleModTool
**cannot** decompile it. To recover it:

- **Symbols survive.** webbed.exe keeps 3,556 `gml_*` names (Object events, scripts, room creation code),
  listed in `docs/inventory/gml_symbols.txt`. YYC registers each name next to its function pointer, so
  Ghidra can label every event and script. This turns "reverse a 20 MB exe" into "read ~40 named functions".
- **YYC calls the runtime through named built-ins** (`physics_joint_rope_create`, `physics_apply_force`,
  `audio_play_sound`, `part_*`, `draw_sprite_ext`…). The constants passed to these calls are what we need.
- **Runtime instrumentation** of the running game (reading instance variables, hooking built-ins) can
  measure the real values. That is a fallback for anything static analysis can't settle.

### Key gameplay functions to reverse (P1 targets)
- Player: `oPlayer_{Create_0, Step_0, Step_2, Draw_0, Alarm_0..4, Collision_oSolid, Other_4, CleanUp_0}`,
  `init_spider_variables`, `get_spider_ground`, `get_spider_webwalk`, `draw_spider`, `kill_player`.
- Webs: `create_web`, `split_web`, `bisect_web`, `snap_web`, `replace_with_web_node`, `web_set_a`,
  `web_set_o`, `player_set_web_attached`, `player_set_weave_base`, `make_web_net`, `reset_web_tracking`,
  `draw_rope`; objects `oWeb`, `oWebNode`, `oWebNode_phantom`, `oWebshotNode`, `oPulleyRope`, `oWebNet`,
  `oPlaceableWeb`, `oNoWebsArea`.
- Physics persistence: `save_physics_properties`, `load_physics_properties`.

## 3. Physics: what's recovered as data (verified)

Webbed runs on **GameMaker's built-in Box2D physics** (runner links `CRunnerContactListener` and
`PhysicsContactStack`; 231 of 467 objects are physics-enabled).

**Physics world (all 134 rooms are identical):** gravity **(0, 10)** m/s², **0.03 m per pixel**,
room speed taken from the 60 fps game speed.

| Object | Parent | Shape | Density | Friction | Restitution | Lin. damp | Ang. damp | Kinematic |
|---|---|---|---|---|---|---|---|---|
| oPlayer | oPhys | polygon (7 verts) | 0.55 | 1.0 | 0.15 | 0.1 | 0.1 | no |
| oWebNode | oGrabbable | circle | 2.0 | 0.2 | 0.1 | 2.0 | 20.0 | no |
| oWebNode_phantom | oPhys | circle | 2.0 | 5.0 | 0.1 | 2.0 | 20.0 | no |
| oWebshotNode | oPhys | circle | 2.0 | 5.0 | 0.1 | 2.0 | 20.0 | **yes** |
| oSpiderNPC / oPlayerPuppet | — | circle | 1.0 | 1.0 | 0.15 | 0.1 | 0.1 | no |
| oGround / oPlatform / slopes / rocks | oSolid | box/poly | 0 (static) | 0.2 | 0.1 | — | — | — |
| oSolidPlatform | oPlatform | box | 0 | **0.0** | 0.1 | — | — | — |

Full table: `docs/inventory/objects.json`.

**[P1] Resolved:** oPlayer's editor "uses physics" flag is 0, and its 7-point polygon is unused.
`oPlayer_Create_0` builds the body in code: circle radius 11 px, density 0.5 (not the editor's 0.55),
restitution 0.15, linear damping 0, friction 1.0.

**[P1] Corrected:** the P0 guess that webs were Box2D joint chains is **wrong**. No web or player code creates
joints. Webs are `oWeb` segments between `oWebNode` / `oWebNode_phantom` bodies, and tension is applied with
`physics_apply_force` in `oWeb_Step_0` and `oPlayer_Step_0`. Shot speed (35), ranges (600 / 1000), pull
power and snap threshold were recovered; see P1 §4 and §6.

## 4. Art inventory (verified names; pixels not yet extracted)

- 689 sprites, 1,723 texture-page items, 94 texture sheets (`docs/inventory/sprites.json`, which has
  size and origin per sprite).
- **The spider is not a frame-animated sprite.** It is assembled at runtime from parts:
  `sSpider_head_top`, `_head_shoot`, `_head_eat`, `_abdomen`, `_leg`, `_pedipalp`, `_blob`, `_blob_eyes`,
  `_blob_eat`, in **7 skin variants** (no suffix, 1–6), plus `_old` versions. Legs and posture are
  procedural (`draw_spider`), so animation fidelity depends on reversing native code, not on extracting
  frames.
- Web and VFX sprites: `sWebNode`, `sWebShot`, `sWebParticle`, `sWebNet`, `sPlaceableWeb`, `sSpiderBubble`.
- 7 shaders (SHDR) are present; their GLSL source is recoverable as data.
- No GM particle-system *assets* (no PSYS/PSEM chunks). Particles are created in code with `part_*`
  calls, so their parameters are in native code.

## 5. Audio inventory (verified names; mappings partly verified in P1)

273 sound assets (`docs/inventory/sounds.json`). **[P1]** All 273 are exported locally with volume and
pitch metadata. Verified from code: web cut → `aTwangSnap` (#64), web pluck → `aTwang` (#60), player spawn →
`aPopMouth1` (#157). The remaining rows below are still **inferred from names only**:

| Likely event | Candidates |
|---|---|
| Web shoot | `aThwip`, `aThwip2`, `aThwip3` |
| Web tension / stretch | `aStretch1`, `aStretch2`, `aRubberBand`, `aTwang` |
| Web break / cut | `aTwangSnap` |
| Walking | `aLittleFeet_01–08`, `aLittleFeetSoft_01–08` |
| Landing / impacts | `aThudBright1–3`, `aThudSmooth01–02`, `aSoftImpacts_01–06`, material impacts (`aWoodOnWood`, `aMetalOnMetal`, `aStoneOnStone`, `aDirtImpact`, `aMudImpact`, `aSolidOnWood`) |
| Skateboard (exists in the original!) | `aSkateboardLoop`, `aSkateFlap`, `mSkatePark` |
| Eat | `aEatFly`, `aEatFly2` |

Volume, pitch randomisation and which sound variant plays are decided in native code → unknown.

## 6. Reuse classification (initial)

| System | Class | Notes |
|---|---|---|
| Box2D world and fixture params | **Directly reusable data** | Values above. Box2D v2.x behaviour needs matching (GM's Box2D version and solver iterations: unknown) |
| Sprites, textures, shaders | **Direct import from the owner's local install** | Extract at runtime from the user's data.win; never ship them |
| Audio | **Direct import from the owner's local install** | Same rule |
| Player controller, web logic, spider IK, particles, SFX triggers | **Translate from reversed native code** | Requires Ghidra work on YYC functions |
| GM runner behaviour (event order, physics stepping, draw pipeline) | **Reconstruct** | Documented GM semantics plus measurement |

## 7. Next steps (completed in P1: tools installed, assets exported locally, exe labelled)

1. **UndertaleModTool (CLI)** to export sprites, texture pages, origins, sounds and shader source from the
   local data.win into an ignored `extracted/` folder, and to confirm the exact GM version.
   *Needs a download from GitHub (UnderminersTeam/UndertaleModTool).*
2. **Ghidra** to load webbed.exe, apply the `gml_*` name table, and reverse the P1 target functions
   (constants, joint types, forces, state machine).
3. **Reference capture.** Record the original game at 60 fps with a frame-locked capture, plus a small
   runtime probe (read-only memory reads of oPlayer x/y/phy_speed) to build measurable trajectories for
   regression tests.
4. Pick the reconstruction runtime: a Box2D port with matching version and settings is required for
   fidelity.

## Inaccessible or uncertain (running list)
- Exact GameMaker runner build string (version ≥ 2.3.2 is known).
- ~~Every gameplay constant inside native code~~ **[P1]** Player and web constants are recovered. Particle
  parameters and per-sound gain and pitch logic are still in progress.
- ~~Box2D solver settings~~ **[P1]** Recovered: 1/60 s steps, 10 velocity and 10 position iterations (20 in
  room_SkatePark). The exact Box2D/LiquidFun source version is still unknown.
- ~~Whether `oPlayer` is a Box2D body~~ **[P1]** It is (11 px circle).
- Legal: Webbed's EULA and mod policy have **not been reviewed yet**. Until they are, assets stay on the
  owner's machine and are never redistributed (`.gitignore` covers this).
