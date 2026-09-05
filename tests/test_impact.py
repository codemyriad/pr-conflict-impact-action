import copy
from pathlib import Path
import tempfile
import unittest

from impact import Git, analyze, body_for, reconcile


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


if __name__ == '__main__':
    unittest.main()
