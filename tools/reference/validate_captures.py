"""List every reference capture and say whether it is usable for validation, and why not.

    python tools/reference/validate_captures.py            # all sessions
    python tools/reference/validate_captures.py M3         # sessions starting with M3

Exit code 0 when every listed session is usable, 1 otherwise (or when none is found).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'tests'))
import capture_lib as cap  # noqa: E402


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else ''
    found = cap.discover(prefix)
    print('capture root: %s' % cap.CAPTURE_ROOT)
    if not found:
        print('no session directories matching %r' % (prefix + '*'))
        return 1
    for s in found:
        print(s.summary())
    bad = [s for s in found if not s.usable]
    print('%d session(s), %d usable, %d unusable' % (len(found), len(found) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
