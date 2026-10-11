"""Run the repository's unittest suite and report per-test outcomes as JSON (sub-test failures included).

    python tools/reference/autoref/testrun.py [--json out.json] [--pattern test*.py]

DP_CAPTURES selects the capture root exactly as for `python -m unittest discover -s tests`.
Exit code 0 when nothing failed or errored (skips are allowed).
"""
import argparse
import json
import os
import sys
import time
import unittest

TESTS_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'tests'))


class JsonResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.records = {}

    def _rec(self, test):
        return self.records.setdefault(test.id(), {'id': test.id(), 'outcome': 'pass', 'detail': '',
                                                   'subtest_failures': 0, 'first_failures': []})

    def startTest(self, test):
        super().startTest(test)
        self._rec(test)

    def addSuccess(self, test):
        super().addSuccess(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        r = self._rec(test)
        r['outcome'] = 'fail'
        r['detail'] = self._exc_info_to_string(err, test)[-2000:]

    def addError(self, test, err):
        super().addError(test, err)
        r = self._rec(test)
        r['outcome'] = 'error'
        r['detail'] = self._exc_info_to_string(err, test)[-2000:]

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        r = self._rec(test)
        r['outcome'] = 'skip'
        r['detail'] = reason

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is None:
            return
        r = self._rec(test)
        r['outcome'] = 'error' if not issubclass(err[0], test.failureException) else 'fail'
        r['subtest_failures'] += 1
        if len(r['first_failures']) < 3:
            r['first_failures'].append('%s: %s' % (subtest._subDescription(), str(err[1])[:300]))


def run(pattern='test*.py'):
    sys.path.insert(0, TESTS_DIR)
    suite = unittest.defaultTestLoader.discover(TESTS_DIR, pattern=pattern, top_level_dir=TESTS_DIR)
    res = JsonResult()
    t0 = time.time()
    suite.run(res)
    tests = sorted(res.records.values(), key=lambda r: r['id'])
    counts = {k: sum(1 for t in tests if t['outcome'] == k) for k in ('pass', 'fail', 'error', 'skip')}
    return {'capture_root': os.environ.get('DP_CAPTURES', '(default)'), 'duration_s': round(time.time() - t0, 2),
            'counts': counts, 'tests': tests}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json')
    ap.add_argument('--pattern', default='test*.py')
    a = ap.parse_args()
    out = run(a.pattern)
    if a.json:
        with open(a.json, 'w') as f:
            json.dump(out, f, indent=1)
    for t in out['tests']:
        if t['outcome'] != 'pass':
            print('%-5s %s  %s' % (t['outcome'].upper(), t['id'], (t['detail'] or '').strip().splitlines()[-1:] or ''))
    print('tests: %(pass)d pass, %(fail)d fail, %(error)d error, %(skip)d skip' % out['counts'])
    return 1 if out['counts']['fail'] or out['counts']['error'] else 0


if __name__ == '__main__':
    sys.exit(main())
