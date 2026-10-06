"""LG2-b2: the review-pr `post` step (review-pr-port.md §2.5), an explicit operator action, never automatic.

`prepare` refuses anything but a complete report run with a pinned PR, scans the exact body file with the SECURITY
preflight's content rules (`scripts/security_preflight.py --file`; matched values are never printed) and returns the
confirmation: the bound command, the target, the reviewed head and the full body, with a digest of all of them.
`post` runs `gh pr review <url> --comment --body-file <report>` only with that digest and only while the PR head is the
one the report reviewed. Never --approve or --request-changes; no inline comments in v1.
"""
import argparse
import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIN_URL, PIN_HEAD, PIN_REPO = 'PR URL', 'Head', 'Target repository'   # state['review_pr'] labels (LG2-d writes them)


class PostRefused(Exception):
    pass


def _scan(report: Path) -> list:
    proc = subprocess.run([sys.executable, str(HERE.parent / 'scripts' / 'security_preflight.py'), '--file', str(report)],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode not in (0, 1):
        raise PostRefused('the secret scan could not run: ' + (proc.stderr.strip() or f'exit {proc.returncode}'))
    return json.loads(proc.stdout)['findings']


def prepare(run_dir) -> dict:
    run_dir = Path(run_dir)
    state = json.loads((run_dir / 'state.json').read_text())
    if not (state.get('config') or {}).get('review_report') or not (state.get('report') or {}).get('complete'):
        raise PostRefused('only a complete report run (REPORTED) is posted')
    pins = state.get('review_pr') or {}
    url, head = pins.get(PIN_URL), pins.get(PIN_HEAD)
    if not url or not head:
        raise PostRefused('the run has no pinned PR URL and head; a local review is not posted')
    report = run_dir / 'review-report.md'
    if hits := _scan(report):
        raise PostRefused('the secret scan matched, so the report stays local: ' +
                          ', '.join(f"{hit['rule']} at line {hit['line']}" for hit in hits))
    body = report.read_text()
    command = ['gh', 'pr', 'review', url, '--comment', '--body-file', str(report)]
    digest = hashlib.sha256(json.dumps([command, head, body]).encode()).hexdigest()[:16]
    return {'command': command, 'url': url, 'repository': pins.get(PIN_REPO), 'head': head, 'body': body, 'digest': digest}


def confirmation(plan: dict) -> str:
    return '\n'.join(['Post this review as a PR comment?', f"Target repository: {plan['repository'] or '-'}",
                      f"PR: {plan['url']} (number {plan['url'].rstrip('/').rsplit('/', 1)[-1]})",
                      f"Reviewed head: {plan['head']}", 'Command: ' + shlex.join(plan['command']),
                      f"To post, run again with --confirm {plan['digest']}", '', '--- full body ---', plan['body']])


def post(run_dir, confirm: str, runner=subprocess.run) -> str:
    plan = prepare(run_dir)
    if confirm != plan['digest']:
        raise PostRefused('the confirmation does not match the body and command shown; show them again')
    view = runner(['gh', 'pr', 'view', plan['url'], '--json', 'headRefOid'], capture_output=True, text=True, timeout=120)
    try:
        current = json.loads(view.stdout)['headRefOid'] if view.returncode == 0 else None
    except (ValueError, KeyError, TypeError):
        current = None
    if not current:
        raise PostRefused('cannot read the PR head: ' + (view.stderr or '').strip())
    if current != plan['head']:
        raise PostRefused(f"the PR head moved since the review ({plan['head']} -> {current}); the report describes another commit")
    done = runner(plan['command'], capture_output=True, text=True, timeout=120)
    if done.returncode:
        raise PostRefused('gh pr review failed: ' + (done.stderr or '').strip())
    return plan['url']


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Post a report run\'s review-report.md as a PR comment (two steps).')
    p.add_argument('--run-dir', required=True)
    p.add_argument('--confirm', help='the digest the first step printed with the full body')
    args = p.parse_args(argv)
    try:
        if args.confirm is None:
            print(confirmation(prepare(args.run_dir)))
        else:
            print('POSTED: review comment on ' + post(args.run_dir, args.confirm))
        return 0
    except (PostRefused, OSError, ValueError, subprocess.SubprocessError) as exc:
        print('REFUSED: ' + str(exc))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
