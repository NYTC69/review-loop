# Concurrent runs and isolated Codex homes

Use a separate, absolute `CODEX_HOME` for each concurrently active lane/run.
Keep each lane's home stable across its permission probe, run and supported
resume. Do not share that home with another active run or interactive Codex
session that can change its configuration. The default home (`~/.codex`, used
when `CODEX_HOME` is unset) counts as a shared home: while any lane runs with a
non-default home, no lane and no interactive Codex session may use `~/.codex`,
because every non-default-home run also watches `~/.codex/config.toml` and
reports any change there, a trust block included, as unexpected (HOLD).
Separate worktrees and run directories are still required; a separate Codex home
does not replace either boundary.

## Why this matters

A v2.9.4 field report described two concurrent runs sharing one Codex home. Each
inserted a workspace trust block; the other run then saw a combined config change
and entered HOLD. The report is operator-supplied, not native behavior reproduced
by this document.

The source explains the failure path: `coordinator.py` captures the selected
Codex home's `config.toml`, then attributes only exact trust-block additions for
the current expected workspace(s). A second workspace's addition can remain after
that subtraction and be classified as `unexpected-content-change`. For a Codex
author, the other lane's trust block also changes this lane's
`author_flags_digest` (its `codex_config_sha256` removes only this lane's own
trust blocks), so the permission-probe PASS no longer matches the run. This is a
safety check, not permission to accept arbitrary concurrent changes.

Source anchors: `global_config_snapshot`, `_only_codex_workspace_trust_append`,
`attribute_global_config_changes`, and `_invoke_once` in `../coordinator.py`.

## Operator preparation, before either lane starts

1. Assign each active lane a distinct worktree, protected run directory and
   absolute, operator-owned Codex home. Keep the home outside every lane's
   author-writable workspace and temporary roots, and separate from run artifacts.
   Avoid symlink aliases that resolve to the same home.
2. Have the owner provision each home through an explicitly authorized login and
   configuration workflow. Do not copy credentials, tokens or an entire existing
   home as a shortcut. This guide does not authorize account access or prescribe
   credential contents.
3. Select that same home in the environment of the lane's coordinator invocation
   for the existing permission-probe, run and resume workflow. The coordinator
   resolves `CODEX_HOME` at startup and passes it to Codex children (the Codex
   author sandbox check instead uses a temporary home under the run directory
   holding a copy of its `config.toml`). An unset value falls back
   to the shared default home. A relative path is rejected, and when any role
   is Codex a home that is not an existing directory is refused before the
   probe or any dispatch.
4. Preserve the installed CLI/model defaults and all capability, program-binding,
   permission and probe checks. Perform the required preflight for each lane;
   do not copy a PASS receipt or override a failure because another lane passed.
   Existing cache validation remains authoritative.
5. Record the non-secret lane-to-home/run/worktree mapping in operator notes.
   Keep those paths and configuration stable while the lane is active. If a run
   already HOLDed, retain its receipts and use the documented recovery process;
   do not edit trust entries, switch its home mid-run, or rewrite evidence to
   manufacture a clean baseline.

These are preparation instructions only. No setup, login, trust mutation or
native probe is performed by reading this document.

## Residual shared state and limits

- With a non-default Codex home, `global_config_snapshot` also records the default
  `~/.codex/config.toml`. Other activity changing that file can still invalidate
  applicable checks. Do not disable monitoring to suppress it.
- The coordinator still records user-level Claude settings and installed-plugin
  metadata under the same OS home. `CODEX_HOME` does not isolate Claude. Probe and
  per-vendor turn attribution have their existing scopes; do not assume every
  captured file is handled identically in every phase.
- Capability checks still inspect system/managed Codex policy and applicable
  workspace configuration. Separate homes do not override `/etc` policy or macOS
  managed preferences and do not make an unapproved permission profile safe.
- Probe-pass storage remains under the user's shared cache
  (`~/.cache/review-loop/probe-pass`) with its existing identity checks; its key
  covers the probe surface version, the reviewer, author and gate flag digests
  and, when a role is Claude, the Claude CLI version. The `CODEX_HOME` path is not
  an input, but for a Codex author the author flags digest covers the selected
  home's `config.toml` contents (minus this workspace's trust blocks). Host
  processes, filesystem aliases, workspace leases, network limits and
  provider-side account limits are not isolated by this variable.
- Hooks/plugins restrictions, `--ignore-rules`, approval policy, sandbox boundaries
  and evidence validation remain unchanged. Never add broad permissions or waive
  a failing guard to make concurrent work proceed.

Source anchors: `Coordinator.__init__`, `probe_passed`, `_probe_cache_key`,
`_invoke_once` in `../coordinator.py`, and `inspect` in
`../codex_capability_guard.py`.

## Concurrent permission probes (FIELD-13, open)

Two `permission-probe` commands running at the same time can fail each other when
a Claude author probe is involved. The Claude author probe (`_claude_author_probe`
in `../coordinator.py`) creates its probe tree `paired-session-author-probe-*`
beside the run dir, in the run dir's parent. It judges writes outside its own tree
by comparing listings (`listing` in `../claude_author_probe.py`) of every entry in
that parent except its own tree and its own run dir, plus every
`/tmp/paired-session-author-probe-*` name. Entries in the parent are listed
without descending into them and with directory mtimes ignored, so writes inside
another run dir are not seen; an entry that is created, removed, replaced or (for a
file) modified there is. A Codex author probe keeps its probe tree inside its own
run dir, but it also makes scratch directories `ps-escape-*` in `/tmp`,
`.paired-session-escape-*` in the home directory and `paired-session-external-*` in
the temporary directory. A Claude author probe whose run dir's parent is one of
those directories sees them.

So a second Claude author probe whose run dir has the same parent always adds and
later removes its own `paired-session-author-probe-*` tree there, and creating or
removing another run dir or file in that parent during the probe window counts as
well, as does an operator file there that changes (for example a launcher log). The
first probe records these as changes beside its run dir and fails, and the other
probe can fail the same way. If a Claude-author run dir lies directly in `/tmp`
(`/private/tmp` on macOS), its probe tree is itself a
`/tmp/paired-session-author-probe-*` directory, which every concurrent Claude author
probe on the host sees through its `/tmp` listing, whatever its own parent. A FAIL
that lists only such sibling probe trees (directories), run dirs or operator files
you can attribute says nothing about the sandbox; an unknown new file beside the
run dir can still be this run's own escape.

Two other names are not FIELD-13 noise. A `.paired-session-os-probe-*` entry in the
parent is a Claude reviewer or gate OS-denial target (`_claude_os_probe_path`), and
a `/tmp/paired-session-author-probe-*.txt` file is an author probe's `/tmp` escape
target; both exist only when that write got past the sandbox. If either appears in
a report, treat it as a possible escape by the other run and check both runs'
probe reports.

Until a fix lands:
- run permission probes sequentially, one at a time on the host (the simplest
  option);
- or give each run dir its own parent directory, where neither parent is `/tmp`
  (or `/private/tmp`), the home directory or the temporary directory, so no other
  probe tree, scratch directory, run dir or operator file appears beside it or
  under the `/tmp` probe names. The `/tmp` escape-target `.txt` names
  stay host-wide, but they appear only after an escape, and the probe that escaped
  fails too.

## Validation boundary

Offline fixtures can show that a combined A+B trust append is rejected when only
A is expected, while isolated A and B snapshots each match their own expected
workspace. That characterizes attribution logic, not actual CLI writes or native
sandbox behavior. The owner must separately validate authorized home setup,
effective home selection and concurrent native runs on the target host. This
procedure reduces one shared-config collision; it does not resolve every parallel
conflict or establish provider quota isolation.
