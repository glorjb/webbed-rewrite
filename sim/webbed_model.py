"""Recovered Webbed (build 7679547) rules, written as plain Python for regression testing.

Every constant and formula here was read from the original binary; the evidence (function, GML
source line, confidence) is in docs/P1_REVERSE_ENGINEERING.md. This module contains NO original
game code or assets: it re-expresses recovered facts so the reconstruction can be tested against
measurements of the original game (tests/test_reference_captures.py).

Units: GameMaker pixels, steps (1 step = 1/60 s), degrees. `phy_speed` = px/step.
"""
import math
import struct

# ---------------------------------------------------------------- physics world (oGameManager)
ROOM_SPEED = 60                      # GEN8 game speed
M_PER_PX = 0.03                      # every room
GRAVITY_MPS2 = (0.0, 10.0)           # oGameManager_Alarm_0 (after a 2-frame pause at room start)
ITERATIONS_DEFAULT = 10              # oGameManager_Other_4
ITERATIONS_SKATEPARK = 20            # room_SkatePark
PHYSICS_PAUSED_FRAMES = 2            # alarm[0] = 2 at Room Start


def physics_substeps(update_speed=ROOM_SPEED, room_speed=ROOM_SPEED):
    """Runner FUN_01250870: list of dt values passed to b2World::Step each frame."""
    dt = 1.0 / update_speed
    out, s = [], update_speed / room_speed
    while s > 0:
        out.append(s * dt if s <= 1 else dt)
        s -= 1
    return out


def gravity_dv_pxps_per_step():
    """Velocity change per step from gravity alone, in px/s (no damping)."""
    return GRAVITY_MPS2[1] / M_PER_PX / ROOM_SPEED


def mps_to_px_per_step(v):
    return v / M_PER_PX / ROOM_SPEED


# ---------------------------------------------------------------- player body (oPlayer_Create_0)
PLAYER_RADIUS_PX = 11
PLAYER_DENSITY = 0.5
PLAYER_RESTITUTION = 0.15
PLAYER_FRICTION = 1.0
PLAYER_LINEAR_DAMPING = 0.0
PLAYER_BULLET = True
PLAYER_FIXED_ROTATION = True
PLAYER_BUOYANCY = 1.1
PLAYER_MATERIAL = 4


def player_mass_kg():
    r = PLAYER_RADIUS_PX * M_PER_PX
    return PLAYER_DENSITY * math.pi * r * r


# ---------------------------------------------------------------- player air drag (oPlayer_Step_0 L1575-L1583)
# phy_linear_damping = 0; drag = parachute ? 0.02 : 0.005;
# s = point_distance(0, 0, phy_speed_x, phy_speed_y)            (px/step, velocity before this frame's step)
# physics_apply_force(phy_com_x, phy_com_y, lengthdir(s*s*drag, point_direction(vx, vy, 0, 0)))
# Verified: constants read from .rdata (0x144E7D0 = 0.005, 0x144E7D8 = 0.02) and the M3 capture
# (blind quadratic fit k = 2.71e-4 /px vs. predicted 2.706e-4 /px, residual RMS 0.00014 px/s).
PLAYER_AIR_DRAG = 0.005
PLAYER_PARACHUTE_DRAG = 0.02


def player_air_drag_accel_pxps2(vx_pxps, vy_pxps, parachute=False):
    """Acceleration (px/s^2) from the player's scripted air drag; force in N on a Box2D body of player mass."""
    s = math.hypot(vx_pxps, vy_pxps) / ROOM_SPEED              # px/step, as GML phy_speed
    if s == 0:
        return 0.0, 0.0
    f = s * s * (PLAYER_PARACHUTE_DRAG if parachute else PLAYER_AIR_DRAG)   # N
    a_mps2 = f / player_mass_kg()
    a_pxps2 = a_mps2 / M_PER_PX
    return -a_pxps2 * vx_pxps / (s * ROOM_SPEED), -a_pxps2 * vy_pxps / (s * ROOM_SPEED)


def player_free_flight_dv(vx_pxps, vy_pxps, parachute=False):
    """Velocity change over one frame (px/s) with only gravity + scripted drag acting (damping 0).
    Box2D semi-implicit Euler: v += h * (g + F/m)."""
    ax, ay = player_air_drag_accel_pxps2(vx_pxps, vy_pxps, parachute)
    h = 1.0 / ROOM_SPEED
    return h * ax, h * (GRAVITY_MPS2[1] / M_PER_PX + ay)


# ---------------------------------------------------------------- tuning (init_spider_variables)
WALKFORCE = 5
MAX_WALKSPD = 3.5           # px/step
MAX_SLIPSPD = 2
JUMP_POWER = 4              # px/step
COYOTE_TIME = 5             # frames
WEBWALK_SPD = 3
WEBWALK_FORCE = 6
SWIMFORCE = 2
MAX_SWIMSPEED = 3
PULL_POWER = 5
WEAVE_PULL_POWER = 2
MAX_GDIST = 600
MAX_WEAVE_DIST = 1000
WEB_SHOT_SPEED = 35
WEB_WEAVE_SPEED = 35
WEBSHOT_DELAY = 15
RECOIL = 10
SHOOT_TIME = 10
TILT_ACC = 0.2
FOOTSTEP_DELAY = 4
WEB_ALPHA = 0.95

# ---------------------------------------------------------------- web segment (oWeb_Create_0)
WEB_PULL_POWER = 1
WEB_STRETCH = 5
WEB_SNAP_THRESHOLD = 3
WEB_FADE_TIME = 120
WEB_NODE = dict(radius_px=2, density=2.0, friction=0.2, lin_damping=2.0, ang_damping=20.0, buoyancy=1.25)
WEB_NODE_PHANTOM = dict(radius_px=2, density=2.0, friction=5.0, lin_damping=2.0, ang_damping=20.0)
GML_EPSILON = 1e-5          # math epsilon used by YYC compares (DAT_015dd6c0)

# ---------------------------------------------------------------- GML / YYC intrinsic semantics
def f32(x):
    return struct.unpack('<f', struct.pack('<f', x))[0]


def lengthdir_x(length, direction):
    """YYC intrinsic 0x1184D10: single precision, |cos| < 1e-4 snapped to 0."""
    c = f32(math.cos(f32(f32(direction) * f32(3.1415927) / f32(180.0))))
    v = f32(f32(length) * c)
    return 0.0 if abs(c) < 1e-4 else v


def lengthdir_y(length, direction):
    """YYC intrinsic 0x1184DA0: -(len * sin), single precision, |sin| < 1e-4 snapped to 0."""
    s = f32(math.sin(f32(f32(direction) * f32(3.1415927) / f32(180.0))))
    v = f32(-f32(f32(length) * s))
    return 0.0 if abs(s) < 1e-4 else v


def point_direction(x1, y1, x2, y2):
    """YYC intrinsic 0x1185380 (single precision): GameMaker y-down angle in [0, 360)."""
    dx, dy = f32(x2 - x1), f32(y2 - y1)
    if dx != 0:
        ang = f32(f32(math.atan2(dy, dx)) * f32(180.0) / f32(3.1415927))
        return -ang if ang <= 0 else 360.0 - ang
    if dy > 0:
        return 270.0
    if dy < 0:
        return 90.0
    return 0.0


def point_distance(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)


def dsin(a):
    return math.sin(math.radians(a))


def dcos(a):
    return math.cos(math.radians(a))


def angle_difference(a, b):
    return ((a - b + 540.0) % 360.0) - 180.0


def sign(x):
    return -1.0 if x < 0 else (0.0 if x == 0 else 1.0)


# ---------------------------------------------------------------- game scripts (verified by hand)
def xoff(x, y, ang):
    """gml_Script_xoff: x*dcos(a) - y*dsin(a)."""
    return x * dcos(ang) - y * dsin(ang)


def yoff(x, y, ang):
    """gml_Script_yoff: x*dsin(a) + y*dcos(a)."""
    return x * dsin(ang) + y * dcos(ang)


def rlerp(a, b, t):
    """gml_Script_rlerp."""
    return (a + angle_difference(b, a) * t + 360.0) % 360.0


PITCH_TABLE = [0.25, 0.26, 0.28, 0.29, 0.31, 0.33, 0.35, 0.37, 0.39, 0.42, 0.44, 0.47, 0.49, 0.52, 0.56, 0.59,
               0.62, 0.66, 0.7, 0.74, 0.79, 0.84, 0.89, 0.94, 1, 1.05, 1.12, 1.18, 1.25, 1.33, 1.41, 1.49,
               1.58, 1.68, 1.78, 1.88, 1.99, 2.11, 2.24, 2.37, 2.51, 2.66, 2.82, 2.99, 3.17, 3.36, 3.56, 3.77]


def get_pitch_in_tune(p):
    """gml_Script_get_pitch_in_tune: nearest entry of the 48-step table."""
    return min(PITCH_TABLE, key=lambda q: abs(q - p))


# ---------------------------------------------------------------- web behaviour
def web_endpoint(inst_x, inst_y, inst_image_angle, dist, rel_dir):
    """oWeb_Step_2 L42-45: endpoint rigidly follows the attached instance."""
    a = inst_image_angle - rel_dir
    return inst_x + lengthdir_x(dist, a), inst_y + lengthdir_y(dist, a)


def web_stretch_for(length):
    """reset_web_tracking: stretch = max(length / 20, 1) (overrides oWeb_Create's 5)."""
    return max(length / 20.0, 1.0)


def web_snap_threshold_for(stretch):
    """reset_web_tracking: snap_threshold = 2 + max(0, 5 - stretch)."""
    return 2 + max(0.0, 5 - stretch)


def web_tension_magnitude(current_length, length, stretch=None, pull_power=WEB_PULL_POWER):
    """oWeb_Step_0 L3-L24: force magnitude applied along `dir` (0 when not taut or <= 0.1).
    stretch defaults to the value reset_web_tracking derives from the rest length."""
    if stretch is None:
        stretch = web_stretch_for(length)
    s = current_length - length
    r = s / stretch
    if r <= 0:
        return 0.0
    f = pull_power * min(1.0, min(1.0, r))
    return f if f > 0.1 else 0.0


def web_should_break(buf1, buf2, buf3, breakable=True):
    """oWeb_Step_2 L56-L59: snap when the length change reverses direction with both changes > 4 px."""
    d2 = buf2 - buf3
    d1 = buf1 - buf2
    return bool(breakable and abs(sign(d2) - sign(d1)) > GML_EPSILON
                and abs(d2) - 4 > GML_EPSILON and abs(d1) - 4 > GML_EPSILON)


def snap_pitch(length):
    """snap_web L23: pitch of aTwangSnap."""
    return min(max(1.0 / (max(length, 1) / 75.0), 0.01), 100)


def pluck_pitch(length, avg_slack_v):
    """oWeb_Step_0 L93-L98 (note pitch before smoothing)."""
    return get_pitch_in_tune((1.0 / (length / 75.0)) * (avg_slack_v / length))


# ---------------------------------------------------------------- jump (oPlayer_Step_0 L1323-1327)
def jump_direction(input_dir, ground_angle):
    """MEDIUM confidence: uses a plain clamp where the game calls gml_Script_rclamp, whose
    comparison operators are not fully verified yet. Valid when input_dir is within +-180 of normal."""
    normal = ground_angle + 90.0
    clamped = max(min(input_dir, normal + 45.0), normal - 45.0)   # rclamp for the common in-range case
    return rlerp(clamped, normal, 0.5)


# ---------------------------------------------------------------- per-skin geometry (set_spider_skin)
SKINS = {
    'legspan': [30, 30, 44, 50, 50, 38, 70],
    'hipspan': [10, 10, 10, 10, 10, 10, 14],
    'abradius': [8, 9, 8, 9, 8, 8, 8],
    'abheight': [3, -3, 3, -1, -1, 3, 3],
    'bent_legs': [0, 0, 0.5, 0.75, 1, 0, 1],
    'head_top_move_ratio': [0.4, 0.4, 1, 0.75, 0.75, 1, 1],
    'head_top_offset': [0, 0, 2, -2, 2, 1, 0],
}


# ---------------------------------------------------------------- web shot (oPlayer_Step_0 L183-L219, L912-L916)
# Firing: gh, gv = lengthdir_x/y(web_shot_speed, point_direction(sx, sy, crosshair_tx, crosshair_ty)) (px/step),
#         retract = 0, slack = 0.75, webshot_node = instance_create_layer(gx, gy, "Webs", oWebshotNode).
# Every Step while the shot exists: webshot_node.phy_position = (gx, gy); webshot_node.phy_speed = (gh, gv).
# oWebshotNode is a KINEMATIC body, so the physics step moves it by exactly (gh, gv) px.
def webshot_body_after_step(gx, gy, gh, gv):
    """Expected (pos_px, vel_pxps) of the web-shot body after the frame's physics step."""
    return (gx + gh, gy + gv), (gh * ROOM_SPEED, gv * ROOM_SPEED)


# ---------------------------------------------------------------- grapple pull (oPlayer_Step_0 L681-L729)
# if d > 16:  w = sqr(1 - clamp((d - 10) / 64, 0, 1));  c = min(max(5, |v|), d / 5)
#   F = lerp(lengthdir(P, dir_g), lengthdir(c, dir_g) - v - (0, 10 * phy_mass), w)    (v, c in px/step)
#   physics_apply_force(phy_com, F)
# d = distance_to_point(gx, gy) (bounding-box distance), dir_g = point_direction(phy_com, g).
# P (pull strength, L661-L677) is NOT decoded yet: weave branch starts with weave_pull_power * clamp(d/10, 0, 1)
# (L661, L666 hand-verified); further terms on L667-L677 are unresolved. Do not use for fidelity claims.
def grapple_pull_force(d, vx_step, vy_step, dir_g_deg, P, mass_kg):
    if d <= 16:
        return 0.0, 0.0
    w = (1 - min(max((d - 10) / 64.0, 0.0), 1.0)) ** 2
    c = min(max(5.0, math.hypot(vx_step, vy_step)), d / 5.0)
    far = (lengthdir_x(P, dir_g_deg), lengthdir_y(P, dir_g_deg))
    near = (lengthdir_x(c, dir_g_deg) - vx_step, lengthdir_y(c, dir_g_deg) - vy_step - 10 * mass_kg)
    return far[0] + (near[0] - far[0]) * w, far[1] + (near[1] - far[1]) * w
