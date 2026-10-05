# ENV-NAME: effects of a change in secret env-var names (design, v297-env-name)

Status: design, plus the fixes that need no owner decision (lane C, v297-env-name, 2026-10-04). Tests: `../test_env_name_effects.py`.

## 1. Mechanism

`_claude_sandbox_settings(role)` denies, by name, every variable of the coordinator's environment that matches `SECRET_ENV_NAME`,
plus a fixed list (`credentials.envVars`). The list is part of `reviewer_flags`, `author_flags` and `gate_flags`, so each flags
digest changes whenever a shell adds or drops a secret-looking name (for example `CLAUDE_CODE_MESSAGING_TOKEN`).

FIELD-10 (d02db5f, owner decision 1) settled this for `probe_passed`: a names-only difference never voids a recorded PASS. The check
is re-run once with the report's own names. A PATH change still needs a new probe. The real dispatch always denies the current names.

## 2. The five effects

| # | Where | Effect today | Intended or accidental | This batch |
|---|---|---|---|---|
| 1 | `_claude_optin_current` | The operator's Claude-author opt-in is voided for good when `author_flags_digest` changes, names-only changes included. The refusal says only "author flags changed". | Binding the opt-in to the flags it was given under is intended (the comment says "voids it for good"). Recovery exists: `run --accept-unverified-claude-author --reason TEXT` writes a fresh opt-in. **Accidental:** the operator cannot tell that only env names changed. | Record the names at opt-in. On a void, record whether the change is names-only, and which names were added or removed. The refusal names the cause and the recovery. |
| 2 | `_probe_skip_accepted` | The `--accept-probe-skip` acceptance is voided for good when any of the three digests changes. Nothing reports the void: `probe_gate` prints only the probe and cache reasons. | Binding is intended, as for #1. **Accidental:** the void is silent. | Same record as #1. `probe_gate` reports a voided acceptance, its cause and the recovery (pass `--accept-probe-skip --reason` again, or run permission-probe). |
| 3 | `_probe_cache_key` | The key holds the three digests, so a names-only change misses the cache and needs a new probe. | Availability only. Whether a cached PASS may be reused under another name set is a policy question. `_probe_cache_reuse` already runs `probe_passed`, so FIELD-10's check would apply after the lookup. | Not changed: **owner decision D-ENV-2.** |
| 4 | `_probe_negative_status` | A non-PASS report stops being "current" when names change. It returns `''`, and `--accept-probe-skip` is then accepted over a FAIL report. That breaks P0-4b H1: "an acceptance never overrides current negative evidence". | **Accidental, and fail-open.** | Compare the digests as FIELD-10 does: if they differ, compare again with the report's own names. The negative status stays current, and the acceptance is refused. |
| 5 | `_verify_frozen_role_dispatch` (lifecycle on) | The frozen manifest holds the role flags with the names, so a names-only change raises `frozen role dispatch changed; abort or start a new run`. | Most likely **accidental**: the names do not change the deny policy, yet the run can only be aborted. It is lane A's W area, so lane A and the owner decide. | **Not touched.** Noted for lane A: use a names-neutral manifest digest. |

## 3. Owner decisions

- **D-ENV-1.** Should a names-only change void the operator opt-in (#1) and the probe-skip acceptance (#2) at all?
  - Recommendation: no, which extends FIELD-10's decision 1. The deny policy ("every secret-looking name present is denied") is unchanged; only the environment differs.
  - With the record from this batch, the names-only case is already detected, so the change would be one condition.
- **D-ENV-2.** Should the probe cache key leave out the names (#3)?
  - Recommendation: yes. Key on a names-neutral digest, and keep `probe_passed`'s FIELD-10 check on reuse.
- **Lane A (#5).** The lifecycle manifest digest has the same question. Recommendation: names-neutral, like D-ENV-2.

## 4. Tests (`../test_env_name_effects.py`)

- **#4:** a FAIL report, then a new secret name. `_probe_negative_status` still returns `FAIL`, and `run --accept-probe-skip` is refused. A real flags change (not names-only) still makes the report stale, as before. A PASS is never negative evidence.
- **#1:**
  - An opt-in, then a new or a vanished secret name: the opt-in is voided as before (no policy change). The void records `cause: secret env-var names changed` with `names_added` / `names_removed`, and the refusal names them.
  - A real author-flags change, or a record from before ENV-NAME (no names stored), gives `cause: flags changed`.
  - That a fresh opt-in clears the void is covered by `test_operator_roles.ClaudeAuthorTests.test_the_opt_in_is_voided_when_the_author_flags_change_and_never_revives`.
- **#2:**
  - An acceptance, then a new secret name: voided as before. `probe_gate`'s reason (and the CLI refusal) reports the voided acceptance, its cause and the recovery.
  - A real flags change says `flags changed`.
  - A void from a permission-probe re-run keeps its own reason (`permission-probe re-run`).
