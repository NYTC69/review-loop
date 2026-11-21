"""Run one CI group of paired_session test parts, a few parts at a time, each in its own process.

usage: python3 .github/paired-session-run-group.py '<json list of {"module", "shard", "of"}>' <workers>
Each part runs in a fresh interpreter (modules can disturb each other in one process), and its output is printed as
one block when it finishes, so the "FAIL: test (module)" lines stay intact for ci_wait.sh. Exit 1 if any part fails.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PART = r'''
import os, sys, unittest
def walk(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite): yield from walk(t)
        else: yield t
loader = unittest.TestLoader()
ids = sorted(t.id() for t in walk(loader.loadTestsFromName('paired_session.' + os.environ['MODULE'])))
shard, of = int(os.environ['SHARD']), int(os.environ['OF'])
mine = [x for k, x in enumerate(ids) if k % of == shard]   # round-robin spreads the slow tests
if sys.platform != 'darwin':   # the candidate-test sandbox is macOS-only by design
    skip = {l.strip() for l in open('.github/paired-session-darwin-only.txt') if l.strip() and not l.startswith('#')}
    dropped = [x for x in mine if x.rsplit('.', 1)[-1] in skip]
    mine = [x for x in mine if x not in dropped]
    print(f'skipped {len(dropped)} darwin-only tests', flush=True)
print(f'shard {shard}/{of}: {len(mine)} of {len(ids)} tests', flush=True)
result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(loader.loadTestsFromNames(mine))
sys.exit(0 if result.wasSuccessful() else 1)
'''


def run(part):
    env = dict(os.environ, MODULE=part['module'], SHARD=str(part['shard']), OF=str(part['of']))
    start = time.monotonic()
    proc = subprocess.run([os.environ.get('PY', sys.executable), '-c', PART], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return part, proc.returncode, proc.stdout, time.monotonic() - start


def main():
    parts, workers = json.loads(sys.argv[1]), int(sys.argv[2])
    failed = []
    with open('test.log', 'w') as log, ThreadPoolExecutor(max_workers=workers) as pool:
        for part, code, out, seconds in pool.map(run, parts):
            name = f"{part['module']} {part['shard']}/{part['of']}"
            block = f'===== {name}: exit {code}, {seconds:.0f} s =====\n{out}\n'
            sys.stdout.write(block)
            sys.stdout.flush()
            log.write(block)
            if code:
                failed.append(name)
    print('failed parts: ' + (', '.join(failed) if failed else 'none'))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
