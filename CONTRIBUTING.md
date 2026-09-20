# Contributing

Every accepted commit has one root `VERSION` and one new user-facing entry in
`CHANGELOG.md`. Install the hooks once after cloning:

```sh
python3 scripts/install_hooks.py
```

Python 3.10+ and Git are required for these scripts; they use only the Python
standard library. On Windows use Git for Windows (including its hook shell) and
replace `python3` with `py -3`. A clone does not automatically enable Git hooks.

## Making a commit

1. Stage the source changes you intend to commit.
2. Record a concrete change for users (or contributors for tooling-only work):

   ```sh
   python3 scripts/versioning.py note "Show reset countdowns beneath each quota bar."
   ```

3. Run relevant tests and `git commit -m "Describe the change"`.

The hook reads staged notes, increments `VERSION`, appends the matching changelog
entry, and stages only those metadata files and the emptied pending-note file.
You do not calculate or edit the version yourself. Multiple notes become bullets
in the same entry. A commit with no notes, placeholder notes, or unstaged changes
to metadata is refused. Other unstaged source files remain untouched. A failed
commit can be retried with the already prepared entry, without a second bump.

Review with `git show --stat` and `git show HEAD:CHANGELOG.md`. To validate locally:

```sh
python3 scripts/versioning.py check-history --head HEAD
python3 -m unittest discover -s tests -p test_versioning.py -v
```

Notes must describe what changed, its visible result, and any important limit.
Write only verified outcomes: do not claim Windows hardware tests, a signed
release, or rollback verification before they actually passed. The changelog is
public and shown to users during updates. Never include private account data.

## Version and integration policy

- Format: **`YYYY.M.N`**, UTC year/month, no leading zero on month or sequence.
  `2026.9.1`, `2026.9.2`, then `2026.10.1` are valid examples.
- Every new commit increments the current month's sequence by exactly one. A new
  month starts at `1`, even for a documentation-only commit. Commit UTC date and
  changelog date must agree. Do not backdate commits. The sequence is bounded by
  the firmware's unsigned 32-bit field (`1..4294967295`); exhaustion fails rather
  than wrapping or reusing a version. The next month still starts at `1`.
- The original commit `a2a4898eb5a3d1f2e2d9043cba82e59b33b8970b` is the sole
  unversioned bootstrap exception. The first implementation is `2026.9.1`.
- Changelog entries are appended at the **bottom**. Historic entries are immutable.
  Correct a previously published statement with a new, explicitly corrective note.
- `main` is append-only and integrated by **fast-forward**, with no merge commits,
  squash commits, rebases, or amendments of accepted history. Disable force pushes
  and require **version-policy/trusted** plus the appropriate source-test checks
  in repository rules. The trusted status must be supplied by GitHub Actions.
- A branch version is provisional until it is serialized after the current main
  tip. If main advances while a branch is under review, replay its changes as new
  commits from current main, with fresh notes and versions; do not merge two
  independently numbered histories. Validate the integration head again before
  fast-forwarding main. Maintainers must serialize concurrent integrations.
- Releases are selected validated commits. A version on every commit does **not**
  mean every commit is automatically released or installed on users' devices.

The pre-push hook validates **every** commit after bootstrap, including a bad
middle commit repaired later. It refuses non-fast-forward pushes and rewritten
tags. Trusted CI runs on `pull_request_target`, checks out only the protected
base, fetches candidate Git objects, and runs the **base's** checker against the
actual PR head. It never checks out or runs PR code. Its only write permission is
`statuses: write`, used to put pending/success/failure under
`version-policy/trusted` on the candidate SHA. The event's own job result is on
the base and cannot by itself gate the candidate correctly.

Source regression tests run separately on ordinary `pull_request`/`push` events
with read-only permissions and no release secrets. The informational push/manual
check uses the accepted main checker (previous main for a main push), rather than
a proposed replacement from a feature head. Complete history is validated,
including new branches and manual runs; existing pushed refs must remain
fast-forwards. New release tags may select an older validated commit already in
main; existing tags cannot be moved. Protect main and required statuses on GitHub
separately.

## Establishing and maintaining repository enforcement

The original bootstrap has no checker/workflow. Establish trust once: the owner
independently reviews the first governance commit, runs tests and history
validation locally, pushes it to an integration branch, and fast-forwards the
reviewed `2026.9.1` onto main before enabling required trusted checks. The
read-only push workflow has a seed fallback only for repository
`luvxinc/Sweetmeter`, actor `luvxinc`, base exactly the original bootstrap, a
single child of that bootstrap, and version exactly `2026.9.1`. It executes the
reviewed seed checker and validates the same rules; this is not another
unversioned exception. This fallback never runs in the trusted PR event and
cannot issue the required status. Once main contains its checker the fallback
is no longer used. CI cannot independently prove the owner's initial review.

After seeding, configure main for linear history, block force pushes/deletion,
and require `version-policy/trusted` from GitHub Actions and source tests. Open a
PR for an integration head to trigger trusted validation, then fast-forward the
**same SHA** after it passes. Do not use GitHub UI merge/squash buttons, which
create a different commit. Do not require a PR merge or an additional human
approval in the sole-owner workflow. If main advances, recreate the candidate
after current main and validate its updated PR again.

CODEOWNERS identifies the maintainer for governance changes without adding a
mandatory approval requirement. These checks prevent accidental numbering and
changelog violations, and test proposed policy changes with the accepted checker.
Maintainers must review governance changes before acceptance. Repository
administrators can change workflows, statuses, rules or the policy; hooks and
repository-controlled CI are not an unconditional security boundary against an
authorized administrator. Status names and Actions attribution do not make
every workflow immutable.

## Recovering from local mistakes

Do not use `git commit --amend` for a versioned commit. Git does not expose a
reliable amend flag to every pre-commit hook invocation. The post-commit check
reports such invalid local history, and pre-push/CI refuse it; a post-commit hook
cannot undo an existing commit. Before any publication, keep a backup branch and
recreate the intended source changes from a valid parent with new pending notes.
Do not copy the invalid `VERSION` or changelog entry into the replacement commit.
If a valid commit was already published, correct it with another new commit.

If a commit is prepared just before midnight/month rollover and then delayed,
the retry may fail because its UTC date is stale. Preserve its note text, restore
the three metadata files from HEAD (or remove generated VERSION/CHANGELOG only
for the original bootstrap), add the notes again, and commit normally. Do not
discard unrelated work. Missing notes are never fabricated automatically.

Local hooks are convenience and early validation, **not an unbypassable security
boundary**: `--no-verify`, disabled hooks, or direct Git plumbing can bypass them.
Independent base validation plus protected, linear, append-only main is the
enforcement layer within the maintainer trust boundary described above.
