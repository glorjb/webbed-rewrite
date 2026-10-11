"""-StateScan analysis: which READ-ONLY game state really tells "a menu is open / the game is paused".

Live finding (2026-10-11): readiness refused to start because instances of oMenuControllerPrompt and oSettingsMenu
existed, although the user was in gameplay. GameMaker menus can be persistent, active-but-hidden instances, so
existence proves nothing. The recovered pause/menu code is not decoded yet (its lifted source stays local), so
the indicator is MEASURED: the scan records, every frame, the menu instances' variables, every instance's `paused`
variable, the player's state, the foreground window, the frame rate and the raw physics-world bytes, while the user
keeps the menu closed (phase A), opens it (phase B) and closes it again (phase C). A candidate is a value that is
constant during B and different from every value seen in A and C (the first SETTLE frames after each phase change
are ignored, for reaction time).
"""

SETTLE = 90            # frames ignored after each phase change (user reaction)
PHASES = ('A_closed', 'B_open', 'C_closed')


def _settled(samples):
    out, last, since = [], None, 0
    for s in samples:
        if s['phase'] != last:
            last, since = s['phase'], 0
        since += 1
        if since > SETTLE:
            out.append(s)
    return out


def _series(samples):
    """{key: {phase: [values]}} for every observable scalar."""
    out = {}

    def put(key, phase, v):
        if isinstance(v, (int, float, str)) or v is None:
            out.setdefault(key, {}).setdefault(phase, []).append(v)

    for s in samples:
        ph = s['phase']
        for mid, m in (s.get('menus') or {}).items():
            for k, v in (m.get('vars') or {}).items():
                put('menu %s#%s.%s' % (m['obj'], mid, k), ph, v)
        for key, v in (s.get('paused') or {}).items():
            put('paused %s' % key, ph, v)
        p = s.get('player') or {}
        for k in ('present', 'locked'):
            put('player.%s' % k, ph, p.get(k))
        raw = s.get('world_raw')
        if raw:
            for i in range(0, len(raw), 2):
                put('world byte +0x%02X' % (i // 2), ph, raw[i:i + 2])
    return out


def analyze_statescan(samples):
    """Returns {'phases': per-phase facts, 'candidates': [...], 'indicator': best instance-variable candidate}."""
    phases = {}
    for ph in PHASES:
        ss = [s for s in samples if s['phase'] == ph]
        if not ss:
            continue
        frames = ss[-1]['frame'] - ss[0]['frame']
        dt = ss[-1]['t'] - ss[0]['t']
        mov = [s for s in ss if (s.get('player') or {}).get('speed') is not None]
        phases[ph] = {'samples': len(ss), 'fps': round(frames / dt, 1) if dt > 0 else None,
                      'webbed_in_front': round(sum(1 for s in ss if s.get('fg')) / len(ss), 2),
                      'menu_instances': sorted({'%s#%s' % (m['obj'], i) for s in ss for i, m in
                                                (s.get('menus') or {}).items()}),
                      'player_present': round(sum(1 for s in ss if (s.get('player') or {}).get('present')) / len(ss), 2),
                      'player_max_speed_pxps': round(max((s['player']['speed'] for s in mov), default=0.0), 2)}
    cands = []
    for key, by in _series(_settled(samples)).items():
        a, b, c = (by.get(p, []) for p in PHASES)
        if not (a and b and c):
            continue
        vb, vac = set(b), set(a) | set(c)
        if len(vb) == 1 and not (vb & vac):
            cands.append({'key': key, 'open_values': sorted(vb, key=str), 'closed_values': sorted(vac, key=str)})
    rank = {'paused': 0, 'menu': 1, 'player.locked': 2, 'player.present': 3, 'world': 4}
    cands.sort(key=lambda c: (rank.get(c['key'].split(' ')[0].split('.')[0] if not c['key'].startswith('player')
                                       else c['key'], 5), len(c['closed_values'])))
    indicator = None
    for c in cands:
        kind, _, rest = c['key'].partition(' ')
        if kind == 'menu':
            obj_id, _, var = rest.partition('.')
            indicator = {'kind': 'instance_var', 'obj': obj_id.split('#')[0], 'var': var,
                         'open_values': c['open_values'], 'closed_values': c['closed_values']}
            break
        if kind == 'paused':
            indicator = {'kind': 'instance_var', 'obj': rest.split('#')[0], 'var': 'paused',
                         'open_values': c['open_values'], 'closed_values': c['closed_values']}
            break
    ok_phases = all(p in phases for p in PHASES)
    if not ok_phases:
        verdict = 'incomplete scan (a phase has no samples)'
    elif indicator:
        verdict = 'menu-open indicator found: %s.%s = %s while open' % (indicator['obj'], indicator['var'],
                                                                     indicator['open_values'])
    elif cands:
        verdict = 'only non-variable candidates changed with the menu (see candidates); no indicator saved'
    else:
        verdict = ('nothing changed consistently with the menu: either the menu was not opened during phase B, or '
                   'its state is not in these variables')
    return {'phases': phases, 'candidates': cands[:40], 'indicator': indicator, 'verdict': verdict}
