# Hook runner boundary (M4 batch 4)

> **Historical** (fake-only candidate-tree lifecycle design, M3/M4). The real lifecycle is W, [doc 6](e2e-6-worktree-lifecycle.md), the default since v2.10.0.

Design only; lifecycle off. Extends [doc 4](e2e-4-delivery-close.md) and [doc 2a](e2e-2a-tree-binding.md) under D8. Coordinator owns dispatch, budgets, receipts and HOLDs; hooks never edit the ledger.

## First-version rule and gate

- Only inventoried `pre-commit`, `prepare-commit-msg`, `commit-msg` run; other active hooks HOLD. Any checkout/Git/index/message mutation or nonzero exit HOLDs: P uses abort or EXEC repair/new convergence; Q uses abort or restart P. No adoption or replay (R23-5 F1/F3).
- Reserve one Local-check attempt per `(item,run,epoch,OID)` before spawn. Exhaustion/uncertainty HOLDs; abort is legal until budget extension exists. No executable supported hook gets a current-OID SKIP receipt.
- After the last writer, P needs no-change PASS/SKIP; later writes invalidate it. Q needs its own receipt. Final gate, accept and DELIVERY require current OID, inventory/config/message/runtime/probe digests and hook receipt with review/tests/SECURITY. Doc 4 lacks this gate: activation HOLDs until it and batch-8 provenance are wired (F2/F3).

## Isolation and no-change verifier

- Journal exact run-owned VM/container/volume names before creation; start/resume clean only these and verify absence. Refuse a conflicting user VM. Use run-private XDG outside writer roots; preflight confirms no user config write or host HOME mount. Name exact cleanup command. Container `--timeout` plus host watchdog bound lifetime after coordinator crash; uncertain teardown HOLDs (F5).
- Use inspected explicit connection; scrub connection/proxy env. Pin Podman/machine OS/image IDs, `--pull=never`, fixed UID, no host share/socket/device/credential, `DEVNULL` stdin, no TTY, resource caps. Seeder/verifier/Containerfile are in an operator-owned pinned OCI archive; verify loaded image ID/config digest. All seeder/hook/verifier/probe containers use `--network=none`, private PID namespace, `--timeout`, read-only rootfs except explicit VM volume. Unknown effective setup HOLDs (F4/F11).
- VM→host loopback needs a firewall and denial probe from the VM root network namespace, not a container. Recheck firewall rules hash and executed negative/positive controls for **each run VM**; otherwise activation HOLDs pending owner risk decision (R1 M3/R2 M1).
- Seeder receives bounded current-OID snapshot via `podman cp` into VM volume, validates it, exits. Hook container mounts only that volume. After all descendants stop, distinct-UID verifier mounts it read-only and returns bounded hashes/status; host compares its manifest. Unproven installed copy/volume semantics HOLD; no tmpfs/export/adoption (F1).
- Seed checkout, message and scratch Git: HEAD=frozen parent (Q: C1), candidate objects, index via VM `read-tree` of P/Q. **Before hooks**, seeder proves `write-tree`=input OID using a temporary index. Runner invokes hooks in Git order with fixed argv (`pre-commit` none; `prepare-commit-msg <message> message`; `commit-msg <message>`) and scratch-only Git env. **After hooks, verifier never invokes Git**: no-follow compare checkout, every GIT_DIR byte (refs/objects/config/index included), message and new/ignored entries against seed manifest; drift HOLDs. Set `GIT_OPTIONAL_LOCKS=0`, disable gc/maintenance; HOME/TMP/XDG/cache outside compared volume (R2 A1).
- Revalidate after candidate writes, just before seeding. Copy checked hook bytes; verify digest inside. Reject malformed inventory, aliases, links, unsupported shebang/CRLF/Mach-O or missing image interpreter. Neutralize nested hooks, fsmonitor, filters, signing, editor, trailers, SSH and credential helpers; unknown executable config HOLDs. Interpreter identity is image ID plus path (F9/I1–I4).
- Receipt binds item/run/epoch/OID, inventory/config/Git/env/image/machine digests, request ID, hook exits, verifier/probe/teardown/output hashes. Persist pending ID before spawn; resume consumes one exact receipt. Stale/missing evidence HOLDs; hook stdout is not authority.

## Activation and reviewed implementation steps

- Installed probes bind runtime versions/specs and prove execution: checkout write allowed; host canary, VM-root loopback/network and detached child denied; stdin EOF, `/dev/null`, timeout and post-stop absence. Skipped/CLI-denied is UNKNOWN. Fake tests include `test_gate_accept_delivery_require_current_hook_receipt`, `test_wrong_layer_probe_is_unknown`, inventory aliases, `git add`, index/message/cache drift and stale Q receipt. No real proof means lifecycle off (F10).
- Reviewed steps, each ≤60 product Python lines: (1) schema/env — unreviewed hook? (2) pinned VM/image — wrong target? (3) seeder/scratch Git — wrong OID? (4) watchdog — survivor? (5) no-Git verifier/receipt — mutation PASS? (6) budget/OID gate — bypass accept? (7) fake E2E/installed probes — skipped PASS? Image/Containerfile count in owning batch review. Activation switch checks every prerequisite, including batch-8 provenance and doc-4 gate wiring.
