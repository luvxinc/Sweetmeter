# Pending change notes

Before **each commit**, record the concrete result for a user or contributor:

```sh
python3 scripts/versioning.py note "Keep quota data visible while Bluetooth reconnects."
```

On Windows, use `py -3` in place of `python3`. This command stages only
`.changes/next.md`. Add more notes with more `note` commands. Do not put access
tokens, account identifiers, local paths, or personal usage data in these notes.

The commit hook reads the **staged** notes, computes the next UTC `YYYY.M.N`
version, appends one entry to `CHANGELOG.md`, stages those two files, and empties
the pending file. A failed commit can be retried without incrementing twice.
Missing or generic notes fail the commit; the hook never invents release notes.
Notes in a commit's changelog are used in user-visible release announcements.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the full version and history policy.
