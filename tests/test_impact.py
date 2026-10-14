import copy
from email.message import Message
import io
from pathlib import Path
import tempfile
import unittest
import urllib.error

from impact import API, RETRY_DELAYS, Git, analyze, body_for, reconcile


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.git = Git(self.temp.name)
        self.initial = self.commit(None, {'file.txt': 'original\n', 'other.txt': 'original\n'})

    def commit(self, parent, files):
        entries = []
        for name, value in sorted(files.items()):
            blob = self.git.run('hash-object', '-w', '--stdin', input=value).stdout.strip()
            entries.append(f'100644 blob {blob}\t{name}\n')
        tree = self.git.run('mktree', input=''.join(entries)).stdout.strip()
        args = ['-p', parent] if parent else []
        return self.git.run('commit-tree', tree, *args, input='fixture\n').stdout.strip()

    def pr(self, number, head, base=None, base_ref='main'):
        return {'number': number,
                'base': {'ref': base_ref, 'sha': base or self.initial,
                         'repo': {'full_name': 'test/repo'}},
                'head': {'ref': f'branch-{number}', 'sha': head,
                         'repo': {'full_name': 'test/repo'}}}

    def test_new_conflict_and_clean_other(self):
        a = self.commit(self.initial, {'file.txt': 'A\n', 'other.txt': 'original\n'})
        b = self.commit(self.initial, {'file.txt': 'B\n', 'other.txt': 'original\n'})
        c = self.commit(self.initial, {'file.txt': 'original\n', 'other.txt': 'C\n'})
        for method in ('merge', 'squash'):
            with self.subTest(method=method):
                self.assertEqual(analyze(self.git, [self.pr(1, a), self.pr(2, b), self.pr(3, c)], method),
                                 {1: [2], 2: [1], 3: []})

    def test_preexisting_conflict_is_omitted_even_when_candidate_changes_it(self):
        base = self.commit(self.initial, {'file.txt': 'base\n', 'other.txt': 'original\n'})
        a = self.commit(base, {'file.txt': 'A\n', 'other.txt': 'original\n'})
        b = self.commit(self.initial, {'file.txt': 'B\n', 'other.txt': 'original\n'})
        self.assertEqual(analyze(self.git, [self.pr(1, a, base), self.pr(2, b, base)], 'merge'),
                         {1: [], 2: []})

    def test_different_base_branches_are_not_compared(self):
        a = self.commit(self.initial, {'file.txt': 'A\n'})
        b = self.commit(self.initial, {'file.txt': 'B\n'})
        self.assertEqual(analyze(self.git, [self.pr(1, a), self.pr(2, b, base_ref='release')], 'merge'),
                         {1: [], 2: []})

    def test_modify_delete_conflict(self):
        a = self.commit(self.initial, {'other.txt': 'original\n'})
        b = self.commit(self.initial, {'file.txt': 'B\n', 'other.txt': 'original\n'})
        self.assertEqual(analyze(self.git, [self.pr(1, a), self.pr(2, b)], 'merge'), {1: [2], 2: [1]})

    def test_squash_topology_and_child_retarget(self):
        a = self.commit(self.initial, {'file.txt': 'A\n', 'other.txt': 'original\n'})
        b = self.commit(a, {'file.txt': 'B\n', 'other.txt': 'original\n'})
        prs = [self.pr(1, a), self.pr(2, b, a, base_ref='branch-1')]
        self.assertEqual(analyze(self.git, prs, 'merge', True)[1], [])
        self.assertEqual(analyze(self.git, prs, 'squash', True)[1], [2])
        self.assertEqual(analyze(self.git, prs, 'squash', False)[1], [])

    def test_invalid_objects_are_errors_not_conflicts(self):
        with self.assertRaises(RuntimeError):
            self.git.merge(self.initial, '0' * 40)

    def test_simulation_preserves_branch_refs(self):
        self.git.run('update-ref', 'refs/heads/main', self.initial)
        before = self.git.run('show-ref').stdout
        self.git.simulate(self.initial, self.initial, 'merge')
        self.assertEqual(self.git.run('show-ref').stdout, before)


class FakeAPI:
    def __init__(self):
        self.comments = []
        self.calls = []
        self.next_id = 1

    def pages(self, path):
        return copy.deepcopy(self.comments)

    def call(self, path, method='GET', data=None):
        self.calls.append((path, method, data))
        if method == 'POST':
            self.comments.append({'id': self.next_id, 'user': {'login': 'github-actions[bot]'}, **data})
            self.next_id += 1
        elif method == 'PATCH':
            next(c for c in self.comments if c['id'] == int(path.split('/')[-1])).update(data)
        elif method == 'DELETE':
            self.comments = [c for c in self.comments if c['id'] != int(path.split('/')[-1])]


class CommentTests(unittest.TestCase):
    def test_create_update_noop_delete_and_silence(self):
        api = FakeAPI()
        reconcile(api, 1, None, 'github-actions[bot]')
        self.assertEqual(api.calls, [])
        reconcile(api, 1, body_for([2], 'merge'), 'github-actions[bot]')
        original_id = api.comments[0]['id']
        reconcile(api, 1, body_for([2, 3], 'merge'), 'github-actions[bot]')
        self.assertEqual([c['id'] for c in api.comments], [original_id])
        count = len(api.calls)
        reconcile(api, 1, body_for([3, 2], 'merge'), 'github-actions[bot]')
        self.assertEqual(len(api.calls), count)
        reconcile(api, 1, None, 'github-actions[bot]')
        self.assertEqual(api.comments, [])

    def test_human_comments_are_preserved_even_with_marker(self):
        api = FakeAPI()
        api.comments = [{'id': 5, 'user': {'login': 'human'}, 'body': body_for([2], 'merge')}]
        reconcile(api, 1, None, 'github-actions[bot]')
        self.assertEqual(len(api.comments), 1)
        self.assertEqual(api.calls, [])

    def test_duplicate_bot_comments_are_repaired(self):
        api = FakeAPI()
        for i in (1, 2):
            api.comments.append({'id': i, 'user': {'login': 'github-actions[bot]'},
                                 'body': body_for([3], 'merge')})
        reconcile(api, 1, body_for([4], 'merge'), 'github-actions[bot]')
        self.assertEqual([c['id'] for c in api.comments], [1])


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def http_error(code, body=b'{"message": "Forbidden"}', **headers):
    message = Message()
    for name, value in headers.items():
        message[name.replace('_', '-')] = value
    return urllib.error.HTTPError('https://api.github.test/x', code, 'error', message, io.BytesIO(body))


class RetryTests(unittest.TestCase):
    def api(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.waits = []

        def urlopen(request, timeout):
            self.requests.append(request.get_method())
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return FakeResponse(answer)
        return API('test/repo', 'token', urlopen=urlopen, sleep=self.waits.append, clock=lambda: 1000)

    def test_transient_403_on_a_read_is_retried(self):
        # The failure seen in the wild: one bare 403 on git/ref/heads/main.
        api = self.api(http_error(403), b'{"object": {"sha": "abc"}}')
        self.assertEqual(api.call('git/ref/heads/main'), {'object': {'sha': 'abc'}})
        self.assertEqual(self.waits, [RETRY_DELAYS[0]])

    def test_server_errors_and_network_errors_are_retried(self):
        api = self.api(http_error(502), urllib.error.URLError('reset'), TimeoutError(), b'[]')
        self.assertEqual(api.call('pulls'), [])
        self.assertEqual(self.waits, list(RETRY_DELAYS[:3]))

    def test_rate_limits_wait_as_github_asks(self):
        api = self.api(http_error(429, retry_after='7'),
                       http_error(403, x_ratelimit_remaining='0', x_ratelimit_reset='1030'), b'{}')
        self.assertEqual(api.call(''), {})
        self.assertEqual(self.waits, [7, 31])

    def test_a_refused_write_is_not_retried(self):
        api = self.api(http_error(403, b'{"message": "Resource not accessible by integration"}'))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 403 .*Resource not accessible by integration'):
            api.call('issues/1/comments', 'POST', {'body': 'x'})
        self.assertEqual((self.requests, self.waits), (['POST'], []))

    def test_a_rate_limited_write_is_retried(self):
        api = self.api(http_error(403, retry_after='3'), b'{"id": 1}')
        self.assertEqual(api.call('issues/1/comments', 'POST', {'body': 'x'}), {'id': 1})
        self.assertEqual(self.waits, [3])

    def test_not_found_is_not_retried(self):
        api = self.api(http_error(404, b'{"message": "Not Found"}'))
        with self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
            api.call('git/ref/heads/gone')
        self.assertEqual(self.waits, [])

    def test_gives_up_with_what_github_said(self):
        errors = [http_error(403, x_github_request_id='ABC:1') for _ in range(len(RETRY_DELAYS) + 1)]
        api = self.api(*errors)
        with self.assertRaisesRegex(RuntimeError, r'GET .*/repos/test/repo/pulls failed: HTTP 403 x-github-request-id=ABC:1 .*Forbidden'):
            api.call('pulls')
        self.assertEqual(self.waits, list(RETRY_DELAYS))


if __name__ == '__main__':
    unittest.main()
