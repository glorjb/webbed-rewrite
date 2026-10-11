"""Action -> physical input bindings, and how each action is observed in the game's input object (oIO).

The runner never assumes a binding works: every press is acknowledged by reading oIO (captured by the probe),
and an unacknowledged press is a `missed_input` failure. Webbed's default bindings are NOT recovered from the
binary, so `tools/reference/auto_bindings.json` lists CANDIDATE keys per action; `calibrate()` tries them in
order and keeps the first one the game reacts to. The result is stored locally (outside the repository).
"""
import json
import os
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULTS_FILE = os.path.join(HERE, '..', 'auto_bindings.json')

# How an action shows up in oIO (variables read by webbed_probe.IO_VARS). `any`: one of the variables
# satisfies the test. Variable meanings are INFERRED from their names and confirmed per run by calibration.
SIGNALS = {
    'right':   {'any': [('hm', 'pos')]},
    'left':    {'any': [('hm', 'neg')]},
    'jump':    {'any': [('jmp_held', 'truthy'), ('jmp', 'truthy')]},
    'grapple': {'any': [('grapple', 'truthy'), ('grapple_pressed', 'truthy')]},
    'weave':   {'any': [('weave', 'truthy'), ('weave_pressed', 'truthy')]},
    'retract': {'any': [('retract', 'truthy')]},
}
# held-state variable used to measure how many frames the game saw an action as held
HELD_VAR = {'right': ('hm', 'pos'), 'left': ('hm', 'neg'), 'jump': ('jmp_held', 'truthy'),
            'grapple': ('grapple', 'truthy'), 'weave': ('weave', 'truthy'), 'retract': ('retract', 'truthy')}


def _test(v, how):
    if v is None or isinstance(v, str):
        return False
    if how == 'pos':
        return v > 0.5
    if how == 'neg':
        return v < -0.5
    return bool(v)


def signal(io, action):
    """True when oIO shows `action` as active."""
    return any(_test((io or {}).get(var), how) for var, how in SIGNALS[action]['any'])


def held(io, action):
    var, how = HELD_VAR[action]
    return _test((io or {}).get(var), how)


def load_defaults(path=DEFAULTS_FILE):
    with open(path) as f:
        d = json.load(f)
    return {k: list(v) for k, v in d['candidates'].items()}


class Bindings:
    """Resolved binding per action (one key each). Unresolved actions map to None."""
    def __init__(self, keys=None, source='defaults', calibrated=None):
        self.keys = dict(keys or {})
        self.source = source
        self.calibrated = dict(calibrated or {})   # action -> {'key', 'latency_frames'} from calibration

    @classmethod
    def first_candidates(cls, candidates):
        return cls({a: (c[0] if c else None) for a, c in candidates.items()}, source='defaults (uncalibrated)')

    def key(self, action):
        return self.keys.get(action)

    def to_json(self):
        return {'keys': self.keys, 'source': self.source, 'calibrated': self.calibrated}

    @classmethod
    def load(cls, path):
        with open(path) as f:
            d = json.load(f)
        return cls(d['keys'], source='local file %s' % path, calibrated=d.get('calibrated'))

    def save(self, path):
        """Write the bindings; an existing file is first copied to <name>.<timestamp>.bak.json (never lost)."""
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        backup = None
        if os.path.exists(path):
            root, ext = os.path.splitext(path)
            backup = '%s.%s.bak%s' % (root, time.strftime('%Y%m%d-%H%M%S'), ext)
            n = 1
            while os.path.exists(backup):
                n += 1
                backup = '%s.%s-%d.bak%s' % (root, time.strftime('%Y%m%d-%H%M%S'), n, ext)
            shutil.copy2(path, backup)
        with open(path, 'w') as f:
            json.dump(self.to_json(), f, indent=1)
        return backup


def load_probe_candidates(path=DEFAULTS_FILE):
    """Keys tried by W4_retract_probe while a grapple is attached."""
    with open(path) as f:
        return list(json.load(f).get('retract_probe', []))
