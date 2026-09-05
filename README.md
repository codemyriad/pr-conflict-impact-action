# PR conflict impact

A GitHub Action that warns when merging one pull request would make another
currently mergeable pull request conflict. Pre-existing conflicts are omitted.

There is at most one marked bot comment per PR. Changed findings update that
comment in place; identical findings cause no write. When the findings clear,
the comment is deleted. No clean-state comment or Actions step summary is emitted.
The Actions run itself is still visible. The check is advisory and succeeds when
conflicts are found; infrastructure errors fail the run and preserve old comments.

## Install

Add `.github/workflows/pr-conflict-impact.yml` to the consuming repository:

```yaml
name: PR conflict impact
on:
  pull_request_target:
    types: [opened, synchronize, reopened, edited, closed]
  push:
    branches: ['**']
  workflow_dispatch:
  schedule:
    - cron: '23 * * * *'
permissions:
  contents: read
  pull-requests: write
concurrency:
  group: pr-conflict-impact
  cancel-in-progress: false
jobs:
  impact:
    runs-on: ubuntu-latest
    steps:
      - uses: codemyriad/pr-conflict-impact-action@v1 # Prefer a full commit SHA.
        with:
          merge-method: merge
```

Use one workflow with this concurrency group per repository. Each run takes a
fresh snapshot of **all** open PRs (including drafts and same-author pairs), so
coalescing pending events does not lose updates. A push to any base branch, PR
update/retarget/closure, manual run, or hourly refresh recomputes the warnings.
The schedule also catches automation events suppressed by GitHub's token rules.

The script rechecks PR heads and base tips before publishing. If they changed,
it retries the scan, up to three attempts. GitHub offers no transaction spanning
all PR heads and comments: a change during publication may briefly leave a stale
comment until the next queued run. Do not run competing copies of this action.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `token` | `github.token` | Contents read and pull-requests write access to the caller repository |
| `merge-method` | `merge` | `merge` or `squash`; must be enabled in the repository |
| `dry-run` | `false` | Log the findings as JSON without creating, editing, or deleting comments |

Use the default installation token. For a custom token, set the step environment
`IMPACT_COMMENT_AUTHOR` to its exact GitHub login so only its comments are managed.
An action version change must retain the marker `<!-- pr-conflict-impact -->`.

## How it works

For every candidate A and other PR B targeting the same base:

1. Merge B into the current base. If it already conflicts, omit B entirely.
2. Simulate merging A into the current base, creating only local Git objects.
3. Merge B into that synthetic commit. Warn only if this merge conflicts.

If A itself conflicts, its impact cannot be predicted; its warning is cleared.
If automatic branch deletion is enabled, also check same-repository child PRs
that would be retargeted from A's branch to A's base. With deletion disabled,
children continue to target their existing base and are not treated as retargeted.

Git's `merge-tree --write-tree` handles renames, add/add, modify/delete and other
structural conflicts. A conflict is exit status 1; other errors abort the scan.
No branch is updated, no PR code is checked out or executed, and nothing is pushed.
The implementation uses Python's standard library and Git in a temporary bare
repository, with global/system Git configuration and hooks disabled.

Requires Linux, Python 3.10+ and Git 2.38+. GitHub-hosted Ubuntu runners include
these. The trusted action runs under `pull_request_target` with minimal permissions;
do not add steps that execute PR code to this workflow.

## Limits

- Configure the merge method you actually use. The result is conditional on it;
  rebase-and-merge is not supported. Squash and merge can differ for stacked PRs.
- This detects Git conflicts, not semantic incompatibilities or failing tests.
- Protected-branch rules, reviews and merge queues are not merge conflicts.
- Custom merge drivers and local Git configuration are not reproduced.
- One candidate is tested at a time; it does not predict a whole merge order.
- Full Git history is fetched. A full refresh makes O(n²) merge checks across PRs
  sharing a base, plus cached baseline checks, and paginated GitHub API reads.
- Comments on a PR closed by a suppressed event are cleaned when its `closed`
  event is delivered; ordinary scans only reconcile currently open PRs.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests create real Git object histories and cover clean/new/pre-existing conflicts,
modify/delete, base isolation, squash topology, child retargeting, Git errors,
and comment creation, in-place update, no-op, deletion and ownership.
