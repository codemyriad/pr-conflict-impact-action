"""Run only explicitly: creates fixture branches/PRs in the dedicated test repo.

Usage: python3 tests/e2e.py /home/silvio/dev/pr-conflict-impact-test
Requires gh authentication, a pushed main branch and the impact.yml workflow.
Refuses to run anywhere except codemyriad/pr-conflict-impact-test.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPO = 'codemyriad/pr-conflict-impact-test'
MARKER = '<!-- pr-conflict-impact -->'


def run(*args, input=None, env=None):
    result = subprocess.run(args, text=True, input=input, capture_output=True, env=env, check=True)
    return result.stdout.strip()


def api(path, *args):
    result = run('gh', 'api', f'repos/{REPO}/{path}', *args)
    return json.loads(result) if result else None


def commit(parent, content, message):
    with tempfile.TemporaryDirectory() as directory:
        env = dict(os.environ, GIT_INDEX_FILE=directory + '/index')
        run('git', 'read-tree', parent, env=env)
        blob = run('git', 'hash-object', '-w', '--stdin', input=content)
        run('git', 'update-index', '--add', '--cacheinfo', f'100644,{blob},fixture.txt', env=env)
        tree = run('git', 'write-tree', env=env)
        return run('git', 'commit-tree', tree, '-p', parent, input=message + '\n')


def push(sha, branch):
    run('git', 'push', 'origin', f'{sha}:refs/heads/{branch}')


def pr(branch, title):
    result = api('pulls', '--method', 'POST', '-f', f'head={branch}',
                 '-f', 'base=main', '-f', f'title={title}', '-f', 'body=Automated conflict-impact test fixture.')
    return result['number']


def refresh():
    previous = {r['id'] for r in api('actions/workflows/impact.yml/runs')['workflow_runs']}
    run('gh', 'workflow', 'run', 'impact.yml', '--repo', REPO)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        runs = api('actions/workflows/impact.yml/runs')['workflow_runs']
        current = next((r for r in runs if r['id'] not in previous), None)
        if current and current['status'] == 'completed':
            print(current['html_url'], current['conclusion'], flush=True)
            if current['conclusion'] != 'success':
                raise RuntimeError(f'Action run failed: {current["html_url"]}')
            return
        time.sleep(5)
    raise TimeoutError('GitHub Actions did not finish within ten minutes')


def comments(number):
    return [c for c in api(f'issues/{number}/comments')
            if c['user']['login'] == 'github-actions[bot]' and c['body'].startswith(MARKER)]


def assert_targets(number, targets, expected_id=None):
    found = comments(number)
    if not targets:
        assert found == [], (number, found)
        return None
    assert len(found) == 1, (number, found)
    actual = [line for line in found[0]['body'].splitlines() if line.startswith('- #')]
    assert actual == [f'- #{n}' for n in sorted(targets)], actual
    if expected_id is not None:
        assert found[0]['id'] == expected_id, found
    return found[0]['id']


def main():
    os.chdir(Path(sys.argv[1]).resolve())
    assert run('git', 'remote', 'get-url', 'origin') == f'git@github.com:{REPO}.git'
    existing = api('pulls?state=all')
    resume = '--resume' in sys.argv
    assert not existing or resume, 'Use a fresh fixture repository or explicitly --resume its fixtures.'
    head = run('git', 'rev-parse', 'HEAD')
    def text(a='original', b='original', old='original'):
        return f'first={a}\n' + '\n' * 12 + f'second={b}\n' + '\n' * 12 + f'stale={old}\n'
    if resume:
        prs = {p['head']['ref']: p for p in existing}
        assert set(prs) == {'fixture-a', 'fixture-b', 'fixture-c', 'fixture-stale'}
        a, b, c, stale = [prs[branch]['number'] for branch in
                           ('fixture-a', 'fixture-b', 'fixture-c', 'fixture-stale')]
        a_sha = prs['fixture-a']['head']['sha']
        base = api('git/ref/heads/main')['object']['sha']
    else:
        initial = commit(head, text(), 'Initial fixture')
        base = commit(initial, text(old='base'), 'Advance base before opening a stale PR')
        push(base, 'main')
        a_sha = commit(base, text(a='A', old='base'), 'Candidate A')
        push(a_sha, 'fixture-a')
        a = pr('fixture-a', 'Candidate A')
        push(commit(base, text(a='B', old='base'), 'Conflicting B'), 'fixture-b')
        b = pr('fixture-b', 'Conflicting B')
        push(commit(base, text(b='C', old='base'), 'Initially independent C'), 'fixture-c')
        c = pr('fixture-c', 'Initially independent C')
        push(commit(initial, text(old='stale'), 'Pre-existing conflict'), 'fixture-stale')
        stale = pr('fixture-stale', 'Pre-existing conflict: must never be reported')
    print(f'Fixtures: candidate={a}, conflicting={b}, independent={c}, stale={stale}', flush=True)
    refresh()
    original_id = assert_targets(a, [b])
    assert_targets(b, [a])
    assert_targets(c, [])
    assert_targets(stale, [])
    print(f'CREATE verified: comment {original_id}; stale PR omitted', flush=True)
    changed = commit(a_sha, text(a='A', b='A', old='base'), 'Make candidate also conflict with C')
    push(changed, 'fixture-a')
    refresh()
    assert_targets(a, [b, c], original_id)
    assert_targets(c, [a])
    print('UPDATE verified: same comment ID, changed targets', flush=True)
    last_updated = comments(a)[0]['updated_at']
    refresh()
    assert_targets(a, [b, c], original_id)
    assert comments(a)[0]['updated_at'] == last_updated
    print('NO-OP verified: unchanged comment timestamp', flush=True)
    cleared = commit(changed, text(old='base'), 'Remove conflicting changes')
    push(cleared, 'fixture-a')
    refresh()
    for number in (a, b, c, stale):
        assert_targets(number, [])
    print('DELETE verified: all warnings removed; pre-existing conflict still silent', flush=True)


if __name__ == '__main__':
    main()
