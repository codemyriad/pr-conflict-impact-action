"""Differential Git merges and reconciliation of one warning per pull request."""

import base64
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

MARKER = '<!-- pr-conflict-impact -->'


class Git:
    def __init__(self, path, token=''):
        self.path = path
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1',
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0',
                        GIT_AUTHOR_NAME='PR conflict impact',
                        GIT_AUTHOR_EMAIL='impact@example.invalid',
                        GIT_COMMITTER_NAME='PR conflict impact',
                        GIT_COMMITTER_EMAIL='impact@example.invalid')
        # Credentials stay out of command arguments, URLs and stored Git config.
        auth = base64.b64encode(f'x-access-token:{token}'.encode()).decode()
        self.env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.extraHeader',
                        GIT_CONFIG_VALUE_0=f'AUTHORIZATION: basic {auth}')
        self.run('init', '--bare', path)

    def run(self, *args, input=None, check=True):
        result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
                                cwd=self.path, env=self.env, input=input,
                                capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(f'Git {args[0]} failed ({result.returncode}): {result.stderr}')
        return result

    def merge(self, base, head):
        result = self.run('merge-tree', '--write-tree', base, head, check=False)
        lines = result.stdout.splitlines()
        if result.returncode not in (0, 1) or not lines or not re.fullmatch(r'[0-9a-f]{40,64}', lines[0]):
            raise RuntimeError(f'Merge could not be evaluated: {result.stderr}')
        return result.returncode == 0, lines[0]

    def simulate(self, base, head, method):
        if method not in ('merge', 'squash'):
            raise ValueError('merge-method must be merge or squash')
        clean, tree = self.merge(base, head)
        if not clean:
            return None
        parents = ['-p', base] + (['-p', head] if method == 'merge' else [])
        return self.run('commit-tree', tree, *parents,
                        input='Synthetic conflict impact check\n').stdout.strip()


def analyze(git, pulls, method, delete_branch_on_merge=False):
    """Use immutable SHAs, not GitHub's eventually consistent mergeable field."""
    baseline = {}
    findings = {}
    for pr in pulls:
        pair = (pr['base']['sha'], pr['head']['sha'])
        baseline[pr['number']] = git.merge(*pair)[0]
    for candidate in pulls:
        number = candidate['number']
        findings[number] = []
        if not baseline[number]:
            continue
        after = git.simulate(candidate['base']['sha'], candidate['head']['sha'], method)
        for other in pulls:
            if other['number'] == number or not baseline[other['number']]:
                continue
            same_base = other['base']['ref'] == candidate['base']['ref']
            retargeted = (delete_branch_on_merge
                          and candidate['head']['repo'] is not None
                          and candidate['head']['repo']['full_name'] == candidate['base']['repo']['full_name']
                          and other['base']['ref'] == candidate['head']['ref'])
            if (same_base or retargeted) and not git.merge(after, other['head']['sha'])[0]:
                findings[number].append(other['number'])
    return findings


def body_for(numbers, method):
    if not numbers:
        return None
    return (MARKER + '\n'
            f'Merging this PR using **{method}** would make these currently mergeable PRs conflict:\n\n'
            + '\n'.join(f'- #{number}' for number in sorted(numbers)))


def reconcile(api, number, body, author):
    comments = [c for c in api.pages(f'issues/{number}/comments')
                if c['user']['login'] == author and c['body'].startswith(MARKER + '\n')]
    comments.sort(key=lambda c: c['id'])
    if body and not comments:
        api.call(f'issues/{number}/comments', 'POST', {'body': body})
    elif body and comments[0]['body'] != body:
        api.call(f'issues/comments/{comments[0]["id"]}', 'PATCH', {'body': body})
    # Also repair duplicates left by a previously interrupted/concurrent deployment.
    for comment in comments[1:] if body else comments:
        api.call(f'issues/comments/{comment["id"]}', 'DELETE')


# Waits between attempts of one API call. GitHub answers transient trouble
# (rate limits, abuse detection, outages) with 403, 429 or 5xx.
RETRY_DELAYS = (2, 5, 15, 30, 60)
MAX_RETRY_WAIT = 300


def retry_wait(error, attempt, method, now):
    """Seconds to wait before retrying a failed call, or None to give up."""
    if attempt >= len(RETRY_DELAYS):
        return None
    if not isinstance(error, urllib.error.HTTPError):
        # Network errors: a timeout or reset says nothing about the request.
        return RETRY_DELAYS[attempt] if isinstance(error, (urllib.error.URLError, TimeoutError)) else None
    headers = error.headers or {}
    limited = headers.get('x-ratelimit-remaining') == '0'
    if error.code == 429 or (error.code == 403 and (headers.get('retry-after') or limited)):
        if headers.get('retry-after', '').isdigit():
            return min(int(headers['retry-after']), MAX_RETRY_WAIT)
        if limited and headers.get('x-ratelimit-reset', '').isdigit():
            return min(max(int(headers['x-ratelimit-reset']) - int(now), 0) + 1, MAX_RETRY_WAIT)
        return RETRY_DELAYS[attempt]
    if error.code >= 500:
        return RETRY_DELAYS[attempt]
    # GitHub sometimes answers a read with a bare 403 for a few seconds. A
    # read is safe to repeat; a write that was refused is not retried.
    if error.code == 403 and method == 'GET':
        return RETRY_DELAYS[attempt]
    return None


class API:
    def __init__(self, repo, token, urlopen=urllib.request.urlopen, sleep=time.sleep, clock=time.time):
        self.root = os.environ.get('GITHUB_API_URL', 'https://api.github.com')
        self.repo = repo
        self.token = token
        self.urlopen = urlopen
        self.sleep = sleep
        self.clock = clock

    def call(self, path, method='GET', data=None):
        url = f'{self.root}/repos/{self.repo}' + (f'/{path}' if path else '')
        for attempt in range(len(RETRY_DELAYS) + 1):
            request = urllib.request.Request(
                url,
                data=json.dumps(data).encode() if data is not None else None,
                method=method,
                headers={'Authorization': f'Bearer {self.token}',
                         'Accept': 'application/vnd.github+json',
                         'Content-Type': 'application/json',
                         'User-Agent': 'pr-conflict-impact-action'})
            try:
                with self.urlopen(request, timeout=60) as response:
                    content = response.read()
                    return json.loads(content) if content else None
            except (urllib.error.URLError, TimeoutError) as error:
                wait = retry_wait(error, attempt, method, self.clock())
                if wait is None:
                    raise RuntimeError(f'{method} {url} failed: {describe(error)}') from error
                print(f'{method} {url}: {describe(error)}; retrying in {wait} s', flush=True)
                self.sleep(wait)

    def pages(self, path):
        for page in range(1, 10001):
            separator = '&' if '?' in path else '?'
            items = self.call(f'{path}{separator}per_page=100&page={page}')
            yield from items
            if len(items) < 100:
                return
        raise RuntimeError('Pagination limit exceeded')

    def snapshot(self):
        pulls = list(self.pages('pulls?state=open'))
        # Read branch tips explicitly: PR API base SHAs can lag a base push.
        bases = {pr['base']['ref'] for pr in pulls}
        tips = {base: self.call('git/ref/heads/' + urllib.parse.quote(base, safe=''))['object']['sha']
                for base in bases}
        for pr in pulls:
            pr['base']['sha'] = tips[pr['base']['ref']]
        return pulls


def describe(error):
    """An error with what GitHub said, so a failed run can be diagnosed."""
    if not isinstance(error, urllib.error.HTTPError):
        return str(getattr(error, 'reason', error))
    headers = error.headers or {}
    detail = [f'HTTP {error.code}']
    for name in ('retry-after', 'x-ratelimit-remaining', 'x-ratelimit-reset', 'x-github-request-id'):
        if headers.get(name):
            detail.append(f'{name}={headers[name]}')
    try:
        body = error.read().decode(errors='replace').strip()
    except Exception:
        body = ''
    if body:
        detail.append(body[:500])
    return ' '.join(detail)


def fingerprint(pulls):
    return sorted((p['number'], p['base']['ref'], p['base']['sha'],
                   p['head']['sha'], p['head']['ref']) for p in pulls)


def main():
    method = os.environ.get('IMPACT_MERGE_METHOD', 'merge')
    if method not in ('merge', 'squash'):
        raise ValueError('merge-method must be merge or squash')
    dry_run = os.environ.get('IMPACT_DRY_RUN', 'false').lower() == 'true'
    repo = os.environ['GITHUB_REPOSITORY']
    token = os.environ['IMPACT_TOKEN']
    api = API(repo, token)
    metadata = api.call('')
    # GitHub omits administrative settings from minimal installation tokens.
    if metadata.get(f'allow_{"merge_commit" if method == "merge" else "squash_merge"}') is False:
        raise ValueError(f'{method} merging is disabled in this repository')
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    fetch_error = None
    for attempt in range(3):
        pulls = api.snapshot()
        with tempfile.TemporaryDirectory(prefix='pr-conflict-impact-') as path:
            git = Git(path, token)
            refs = {f'+refs/heads/{p["base"]["ref"]}:refs/impact/base/{p["base"]["ref"]}' for p in pulls}
            refs.update(f'+refs/pull/{p["number"]}/head:refs/impact/pr/{p["number"]}' for p in pulls)
            if refs:
                # A PR closed since the snapshot, or a network hiccup, fails
                # the fetch; the next attempt starts from a fresh snapshot.
                try:
                    git.run('fetch', '--no-tags', metadata['clone_url'], *sorted(refs))
                except RuntimeError as error:
                    fetch_error = error
                    print(f'Fetch failed, retrying from a fresh snapshot: {error}', flush=True)
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
            # Catch changed/deleted refs before merge failures can be misclassified.
            if fingerprint(pulls) != fingerprint(api.snapshot()):
                continue
            findings = analyze(git, pulls, method,
                               os.environ.get('IMPACT_RETARGET_CHILDREN', 'false').lower() == 'true')
        if fingerprint(pulls) != fingerprint(api.snapshot()):
            continue
        if dry_run:
            print(json.dumps(findings, sort_keys=True))
            return
        # The default installation token authors comments as github-actions[bot].
        author = os.environ.get('IMPACT_COMMENT_AUTHOR', 'github-actions[bot]')
        for number, others in findings.items():
            reconcile(api, number, body_for(others, method), author)
        closed = event.get('pull_request', {})
        if closed.get('state') == 'closed':
            reconcile(api, closed['number'], None, author)
        return
    raise RuntimeError('PRs or base branches kept changing; no comments updated. Rerun the workflow.'
                       + (f' Last fetch error: {fetch_error}' if fetch_error else ''))


if __name__ == '__main__':
    main()
