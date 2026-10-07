# Delivery baseline and candidate manifest

`scripts/delivery_scope.py` defines a shared, machine-readable delivery object.
It records the pre-task state, explicit task scope, and current candidate without
changing source files, the user's index, Git objects, or Git refs. The only write
is an explicitly requested JSON artifact. This is the W01 object definition and freshness helper. The read-only W02
scanner is `scripts/security_preflight.py`; it validates this artifact but does not
change its read-only capture contract or provide user approval. The paired-session
coordinator calls both.

## Lifecycle and CLI

Capture the baseline **before** implementation or staging. Scope selectors are
literal repository-relative files or directory prefixes. Repeat `--scope` for
multiple selectors; `.` explicitly selects the entire repository. Selectors can
name files that do not yet exist. They do not expand globs or Git pathspec magic.

```sh
python3 scripts/delivery_scope.py --repo . capture \
  --scope scripts/example.py --scope tests/example_test.py \
  --output "$RUN_DIR"/evidence/example-baseline.json

python3 scripts/delivery_scope.py --repo . manifest \
  --baseline "$RUN_DIR"/evidence/example-baseline.json \
  --output "$RUN_DIR"/evidence/example-candidate-1.json

python3 scripts/delivery_scope.py --repo . check \
  --manifest "$RUN_DIR"/evidence/example-candidate-1.json
```

`capture --from-commit <commit>` records instead the state of a clean checkout of that commit (HEAD, index and
worktree all equal to it, nothing untracked), read from Git objects. It is the baseline of a review-only run, whose
live tree already holds the change under review, so that change counts as the task's own; the coordinator captures
it with `--scope .` (`RUN_DIR/evidence/delivery-baseline-<id>.json`).

The output parent directory must already exist. Artifacts inside the worktree
must be Git-ignored or untracked under `.review-loop/`; external artifacts also
work. Existing output
paths and Git administrative storage are rejected. Without `--output`, JSON is
written to stdout; redirect it only outside the observed worktree or into an
ignored artifact directory. Otherwise shell redirection itself creates an
observed input. JSON escapes unusual filenames, including tabs, newlines, and
non-UTF-8 filesystem bytes represented by Python surrogate escapes.

Keep the baseline immutable throughout the task. Each later candidate embeds
the same baseline and receives its own fingerprint. After any implementation or
staging change, create a new candidate filename from that original baseline.
Never recapture a baseline after editing merely to clear an overlap or freshness
failure: doing so changes the attribution boundary. Scope is bound at baseline
capture in version 1; widening it requires an explicit scope/baseline decision,
not silently editing the artifact.

Reviewers, security checks, and eventual delivery consumers can all name the
candidate's `fingerprint`. `check` reports whether that exact candidate still
matches the repository. It does not persist a passing claim or authorize any
subsequent action. The existing workflow remains responsible for its gates.

| Exit | Meaning |
| --- | --- |
| 0 | Capture/manifest succeeded, or candidate is fresh |
| 1 | Valid candidate is stale |
| 2 | Invalid scope, artifact, output path, or repository binding |
| 3 | Git/I/O failure, unsupported repository content, or unstable capture |

Errors are JSON on stderr; `check` always emits its fresh/stale result on stdout
when capture succeeds. Failed or unsupported capture produces no manifest.

## Schema 1

Both artifact kinds carry `schema: 1`, `policy`, `created_at`, and `fingerprint`.
The policy fixes literal scope matching, raw disk hashing, and the inclusion of
non-ignored untracked files except those under `.review-loop/`. A different
policy requires a different contract.

A `delivery-baseline` contains:

| Field | Meaning |
| --- | --- |
| `repository` | Canonical worktree and Git directory paths, plus Git object format |
| `scope` | Sorted, unique, canonical task-declared selectors |
| `state.head` | Commit OID, or null for an unborn repository |
| `state.head_paths` | Committed path → `{mode, oid}` |
| `state.index` | Path → ordered `{mode, oid, stage}` entries; all conflict stages retained |
| `state.index_file_sha256` | Raw index-file digest, or null if no index exists |
| `state.worktree` | Present file/symlink path → `{mode, oid, sha256, size}` |
| `state.untracked` | Sorted non-ignored untracked paths reported by Git |

The index remains independent of the working tree. For example, if `a.py`
contains staged user content plus further unstaged edits, all three versions
(HEAD, index, disk) have separate identities. A missing working-tree entry is a
tombstone when that path exists in HEAD or the index. Staged deletions whose
files remain on disk are also listed as untracked. Renames are represented as
deletion/addition endpoints; no similarity-based ownership inference is made.

A `delivery-manifest` embeds the complete `baseline` and adds:

| Field | Meaning |
| --- | --- |
| `baseline_fingerprint` | Identity of the pre-task baseline |
| `current`, `current_fingerprint` | Current state in the same format, and its digest |
| `task_declared_scope` | The baseline's explicit selectors; declaration is distinct from observed modification |
| `declared_content_paths` | Concrete paths known before or now that match those selectors, including unchanged/deleted paths |
| `baseline_changes` | Separate `staged`, `unstaged`, `untracked`, and `conflicted` pre-existing changes |
| `task_delta` | Post-baseline HEAD/index/disk differences within the declared scope |
| `outside_scope_delta` | Observed post-baseline differences outside that scope; these are unclaimed |
| `ownership_ambiguities` | Declared concrete paths already dirty at baseline, including untouched user changes |
| `head_changed`, `index_file_changed` | Whether HEAD or raw index bytes differ from baseline |

Every delta row carries `path`, `before`, `after`, `baseline_dirty`, and
`ownership`. Before/after independently carry the path's `head_paths`, `index`,
and `worktree` entries, using null for absence. This preserves index-only changes
even when disk content is unchanged. Baseline staged/unstaged rows use
`{path, change, before, after}`, where identities are `<mode>:<oid>` and absence
is null. Conflict paths are listed separately, not misreported as staged
deletions.

`declared-post-baseline` is a temporal/scope classification, not proof of who
authored the bytes. `ambiguous-baseline-overlap` explicitly means a file-level
ownership decision cannot isolate task content automatically. For example:

1. HEAD contains `base`; the user's working copy contains `base + draft`.
2. The task appends `fix` to that same file.
3. The baseline disk identity binds `base + draft`; the candidate binds
   `base + draft + fix`; the row is marked ambiguous.

Neither the entire file nor the user's staged version becomes task-owned.
`declared_content_paths` is not a list that may be passed directly to `git add`.
Likewise, a pre-existing unrelated staged change is retained in the baseline and
current index; being present in the index does not make it part of delivery.
HEAD movement and unresolved index stages require reconciliation by the
consumer; a fresh manifest can describe an ambiguous or conflicted state.

## Fingerprints, freshness, and evidence compatibility

Fingerprints are `sha256:` plus the SHA-256 of canonical JSON: sorted keys,
ASCII escapes, and compact separators. Each artifact's own `fingerprint` and
`created_at` are excluded from that artifact's hash. The candidate binds its
embedded baseline as well as current state and derived scope/attribution fields.
`current_fingerprint` hashes the entire current state. These are integrity and
identity checks, not signatures or attestations of authorship.

A live capture reads the complete inventory twice and requires equality. `check`
validates the stored artifact, recomputes derived fields, and recaptures the
repository. Any observed HEAD, index, disk, mode, or untracked inventory change
makes the candidate stale, including changes outside task scope. Index flags,
intent-to-add, and cache metadata are conservatively bound by the raw index
digest; a harmless cache refresh can therefore require a new candidate.
No lock is held across review, checking, and later actions. Matching reads are
an optimistic consistency check, not an atomic filesystem snapshot or protection
against changes after `check` returns.

Git blob identities are computed locally without writing objects. The
`<mode>:<oid>` representation matches Git's tree-entry shape; HEAD and index OIDs retain Git's native object
format. Disk entries additionally bind raw SHA-256 and byte size. A manifest contains identities, not historical file bytes or
a patch archive. When review needs a baseline preimage or an attributable patch,
retain the corresponding existing evidence snapshot or another authorized
preimage source and explicitly establish the matching identities. A manifest
fingerprint alone cannot reconstruct overwritten user content.

## Capture boundaries

- Inventory includes all tracked paths and non-ignored untracked files, even
  outside declared scope, so pre-existing and later unrelated work stays visible.
  Untracked content under `.review-loop/` (local config, old session files and
  tmp) is excluded; tracked files there,
  such as a committed `config.md`, stay in the inventory.
  Scope controls classification, not permission to discard outside changes.
- Git ignore rules exclude untracked files. An explicit selector naming only
  ignored, untracked content is rejected. A broader directory selector does not
  override ignore rules. Tracked files remain captured even if an ignore rule
  matches them.
- Regular files are streamed as raw bytes; executable mode and symlink target
  bytes are bound. Symlink targets are never opened, including parent symlinks
  during disk reads. Absolute paths, `..`, Git metadata components, and selectors
  traversing symlinks are rejected.
- No clean/smudge filter, hook, or content transformation runs. Consequently
  CRLF normalization, filters, or filesystem-mode differences may appear as raw
  baseline differences even when porcelain reports a clean file. Consumers must
  not confuse these raw identities with filtered staging output. Sparse checkout
  omissions are similarly recorded as absent disk content, not proof of a
  task-authored deletion.
- Submodules/gitlinks, untracked nested repositories, and special files such as
  FIFOs/sockets are rejected instead of silently claiming complete coverage.
  Empty directories are outside Git's file-level delivery model.
- The repository binding is local to one canonical worktree; moving the
  repository or applying a candidate to another worktree requires a new explicit
  baseline decision. Repository-local Git environment overrides are cleared and
  optional Git index locking/fsmonitor are disabled for read-only capture.
