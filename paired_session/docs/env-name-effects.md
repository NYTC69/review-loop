# ENV-NAME: effects of a change in secret env-var names (design, v297-env-name)

Status: design, plus the fixes that need no owner decision (lane C, v297-env-name, 2026-10-04). v3.7.0 removed the
permission probe, its cache, the Claude-author opt-in and the probe-skip acceptance, so effects #1 to #4 below no longer
exist and their tests were deleted; they are kept as a record. Effect #5 still applies.

## 1. Mechanism

`_claude_sandbox_settings(role)` denies, by name, every variable of the coordinator's environment that matches `SECRET_ENV_NAME`,
plus a fixed list (`credentials.envVars`). The list is part of `reviewer_flags`, `author_flags` and `gate_flags`, so each flags
digest changes whenever a shell adds or drops a secret-looking name (for example `CLAUDE_CODE_MESSAGING_TOKEN`).

FIELD-10 (d02db5f, owner decision 1) settled this for the former probe PASS: a names-only difference never voided a
recorded PASS. The real dispatch always denies the current names.

## 2. The five effects

| # | Where | Effect (before v3.7.0) | Intended or accidental | This batch |
|---|---|---|---|---|
| 1 | `_claude_optin_current` (removed in v3.7.0) | The operator's Claude-author opt-in was voided for good when `author_flags_digest` changed, names-only changes included. The refusal said only "author flags changed". | Binding the opt-in to the flags it was given under was intended. **Accidental:** the operator could not tell that only env names changed. | Recorded the names at opt-in and, on a void, whether the change was names-only. |
| 2 | `_probe_skip_accepted` (removed in v3.7.0) | The probe-skip acceptance was voided for good when any of the three digests changed, silently. | Binding was intended, as for #1. **Accidental:** the void was silent. | Reported a voided acceptance, its cause and the recovery. |
| 3 | `_probe_cache_key` (removed in v3.7.0) | The key held the three digests, so a names-only change missed the cache. | Availability only. | Not changed: owner decision D-ENV-2 (moot since v3.7.0). |
| 4 | `_probe_negative_status` (removed in v3.7.0) | A non-PASS report stopped being "current" when names changed, so an acceptance could override a FAIL report. | **Accidental, and fail-open.** | Compared the digests as FIELD-10 does, so the negative status stayed current. |
| 5 | `_verify_frozen_role_dispatch` (lifecycle on) | The frozen manifest holds the role flags with the names, so a names-only change still fails the check. Since V3110-HINT the HOLD names the change when the manifest recomputed under the saved names equals the frozen one (digest and version too): `frozen role dispatch changed; credential env-var names added: …; removed: …; restore the environment (unset …; export NAME=<value>) and run resume again to continue the run`. Names only, never values. Restoring the environment and resuming continues the run. Any other drift still says `frozen role dispatch changed; abort or start a new run`. | Was **accidental**: the names do not change the deny policy, yet the run could only be aborted. | Not changed here. V3110-HINT added the diagnosis; the manifest itself still holds the names. |

## 3. Owner decisions

- **D-ENV-1** (opt-in and acceptance void on a names-only change) and **D-ENV-2** (names in the probe cache key) are moot
  since v3.7.0: the opt-in, the acceptance and the cache no longer exist.
- **Lane A (#5).** The lifecycle manifest digest has the same question. Recommendation: names-neutral. Until then,
  V3110-HINT tells the operator which names to restore (§2, #5).
