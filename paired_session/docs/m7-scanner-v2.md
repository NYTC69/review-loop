# M7 D-b1 transcript scanner, v2 (design, D04)

> **Historical** (M7 scanner design). Its tooling (`scripts/m7_*.py`) and the legacy arm were removed in v2.13.1, so it cannot be run as written.

Status: design only, step 1 of 2 (owner D04 = "redesign", 2026-10-05). Implementation is step 2, after the design gate.
M7 is an optional cost and quality study; nothing here blocks the legacy removal.

## 1. Why v1 is replaced

The parked v1 (`.compass/results/laneb/m7-s3-wip/m7_scan.py`, three Codex rounds, R3 fix unreviewed) scanned the
m7-s4 pilot: 2 cases × 2 arms of real transcripts. It found 0 true positives and only false positives.

| Cause | Count (placement-neutral) | v1 rule that fired |
|---|---|---|
| The legacy orchestrator writes almost every Bash call with shell variables, `$(…)`, `$$`, heredocs and one literal `for` loop | 8 + 8 | "variables fail closed" |
| Heredocs the lexer cannot split: p1 `{ cat <<EOF … EOF; } > $P`, p2 `python3 - "$F" <<'PY'` | 1 + 1 | "unparseable" |
| `Skill` (legacy entry), `StructuredOutput` (paired reviewer answer) | 2 + 2 | "unknown tool" |
| Cases placed under `~/3Cats/…/.compass/results` (as-is placement only) | 48 | deny list |

The last row is a placement artifact. m7-s1b's freeze already refuses that placement, so real runs cannot have it; the
regression corpus (§5) rebases the paths. The design gate found one more class, hidden by v1's early fail-closed:
prose ` / ` and sed scripts inside heredoc and code arguments read as paths. §3.4 handles it.

## 2. Threat model

**Owner rule (2026-10-06, verbatim):** "我们的目的不是做到绝对的安全，而是默认用户和模型不会做坏事。不会越界，所以只做合理范围内的防护，不能死磕边界情况，和最坏情况".

**Purpose.** Users and models are benign. The scan catches realistic accidental crossings, by an arm's tools, into
material that would bias the M7 measurement:
- the answer key or grader inputs;
- another case, arm or the corpus;
- the operator's real `~/.claude`, `~/.codex` or `~/3Cats`, the keys directory, results, plugin caches;
- the network beyond the model provider.

CLEAN means: every recorded tool call passed the checks below. It does not prove what processes did.

**Allowed places.**
- the case directory;
- the arm's runtime directories: its fresh HOME, the pinned plugin copy, the CLI install;
- the arm's `TMPDIR`, `/tmp`, `/private/tmp`;
- the toolchain directories;
- `/dev/null`, `/dev/stdout`, `/dev/stderr`.

The deny roots are unchanged from v1 (with the operator's HOME as an absolute policy path), and they win over the
allowed places.

**Violation** (rules `deny-list`, `namespace`, `network`). A recorded tool call does one of these:
- names a path outside the allowed places, or one inside a deny root. Recorded calls cover the orchestrator, the
  reviewer child, subagents and Codex items, and a path counts whether it is read, written, listed, globbed, redirected
  or passed as an argument;
- calls a `Skill` outside the arm's pinned plugin namespaces;
- uses `WebFetch` or `WebSearch`, or runs `curl`, `wget`, or `git` `clone`, `fetch`, `pull` or `ls-remote` with a URL
  or remote argument.

**Supported shell subset.** The analysis covers exactly these constructs (§3):
- simple commands with redirections;
- `;`, newline, `&&`, `||`, `|`, `&`;
- `{ …; }`, `( … )`, `if`/`elif`/`else`, and `for NAME in WORDS` over known words;
- assignments;
- `$NAME`, `${NAME}`;
- `$(…)` and backticks up to depth 2;
- heredocs;
- `cd` to a known place.

Added in step 2 from the construct census (§6), because benign transcripts use them often:
- `while`/`until` and `case`: each condition, body or branch is checked once, then the union is taken (as for `if`).
- A `for` over a generated list: the variable is opaque and the body is checked once.
- `${NAME:-word}` and its variants: the variable's value or the word's. Any other `${…}` operator is opaque.
- `$((…))` is a segment.
- The `time` and `!` prefixes.
- Braces are a pattern only with a comma, as in bash, so `find … -exec … {} \;` is literal.

Everything else fails closed (rule `fail-closed`, which excludes the case as in v1).

**Fails closed.**
- The shell rules:
  - a construct outside the subset;
  - an opaque value (§3.3) used as a redirection target, a command name, or a path prefix (followed by `/`);
  - `cd` outside the allowed places;
  - past the caps (§3.3).
- The transcript rules:
  - an unparseable transcript line;
  - an unknown tool or Codex item type;
  - a call whose input never arrives;
  - a `Task` or `Agent` call whose subagent events are missing from the stream.

**Accepted residuals** (one line each; never a blocker):
- Process effects: files a started program opens itself (a script, git's config, the CLI's own files) are not seen.
- Runtime-built paths: a whole-word opaque argument, and opaque values inside interpreter code, are not checked.
- Code and prose (§3.4) are checked only for deny-root, other-case/arm and corpus hits.
- Data on an allowed command's stdin (`cat > f <<'EOF'`, `git apply`) is not checked as paths.
- Deliberate evasion, contrived shell tricks and worst cases (owner rule).

## 3. Shell analysis: bounded resolution within one command

### 3.1 Choice (D04 a)

We take bounded resolution, not the split between protocol-emitted and freely written commands. The legacy
orchestrator composes its commands instead of copying them. For the same step, p1 wrote
`U=$(uuidgen | tr A-Z a-z); … $U`, while p2 wrote `uuidgen | tr A-Z a-z; echo $$` and then inlined the UUID. Telling
protocol commands apart would need a template matcher: a strict one keeps the false positives, and a loose one trusts
any text that looks like protocol. Bounded resolution needs no knowledge of the protocol and treats both arms alike.

### 3.2 Lexer and parser

Our own lexer replaces POSIX `shlex`, which drops quoting. A word is a sequence of parts: literal text, `$NAME` or
`${NAME}`, or a command substitution. Each part is tagged unquoted, `'single'` or `"double"`.

**Heredocs.**
- After `<<` or `<<-`, the lines up to the delimiter become the command's stdin body. Several heredocs on a line are
  read in order. `<<-` strips leading tabs.
- A quoted delimiter makes the body *literal*. Otherwise the body is *expanding*: backslashes behave as in double
  quotes, so p1's `` \` `` stays a literal backtick.

**Commands and quoting.** Commands are separated and grouped as listed in §2. A redirection after a group applies to
every command in it.
- Single quotes are literal.
- Double quotes expand with no splitting or globbing.
- Unquoted words expand, then split on whitespace and glob.
- Only an unquoted leading `~` or `~/` becomes the arm's HOME. A quoted `~` stays literal, and the path check never
  re-expands it.

### 3.3 Values and state

The value table lives for one Bash call only; no shell state crosses calls.

| Part | Source |
|---|---|
| known text | literals; `$HOME`; `$TMPDIR` (the runner's arm TMPDIR); `$PWD`, `$(pwd)` (the current cwd); `$(git rev-parse --show-toplevel)` (the case repository root) |
| segment | `$$`, `$PPID`, `$RANDOM`, `$?`; `$(uuidgen)`, `$(uuidgen \| tr A-Z a-z)`; `$(date [-u] +FMT)` with FMT of `%Y %m %d %H %M %S %s` and `-:TZ`; `$(mktemp …)` is the arm's TMPDIR plus a segment |
| opaque | any other substitution; unset variables |

A segment matches `[0-9A-Za-z:._+-]+`: one path component, never `.` or `..`. Concatenation keeps the parts in place,
so `$P/key` with P = `/x` is `/x/key`.

Each variable holds a set of alternatives, and the checker walks the call in execution order:
- An assignment in sequence replaces the set. A `NAME=WORD` prefix on a command applies to that command only.
- A conditional subtree (the right operand of `&&`/`||`, and each `if`/`elif`/`else` branch) is checked with the
  current table. Afterwards every variable is the union of its set before and after.
- Assignments in a pipeline element, a `&` command, `( … )` or `$(…)` do not reach the enclosing list.
- `for` is unrolled in list order, carrying the table.
- `cd KNOWN` sets the cwd for the rest of the call. The target must be inside an allowed place. The cwd follows the
  same scope rules as variables: it is restored after `( … )`, a pipeline element or a `&` command, and a `cd` in a
  conditional subtree leaves a union of cwds. Each relative path is checked against every cwd in the union.

Caps: 64 alternatives per variable, 64 loop iterations, 4096 simple commands per call.

### 3.4 What is checked

**Redirections and command names.** A redirection target (`>`, `>>`, `<`, `2>`, `&>`) is always a path; a
file-descriptor duplication (`>&2`) is not. The command name must be known text. If it contains `/`, it is checked as a
path.

**Arguments.** An argument is checked when:
- its normalised text contains `/`;
- it began with an expanded `~`;
- it holds an unquoted glob character;
- it names an existing entry of the cwd (v1 R3);
- it is the value after `=` of an option.

How it is checked:
- **Unquoted glob characters:** v1's `check_pattern`, with one change: the literal prefix before the first wildcard
  must lie inside an allowed place (v1 required the case). The deny roots still win, and every match on disk is still
  realpath-checked. This makes `cat *` a pattern (the R3 fix); `"glob:test_*.py"` and `'^?? x'` are not patterns.
- **A segment:** the literal prefix before the first segment must be inside an allowed place and outside the deny
  roots. No component after a segment may be `..` or a deny component.
- **Both glob characters and a segment:** fail closed.
- **Otherwise:** realpath and the rules of §2.

**Code and prose.** This covers interpreter code (a heredoc body on the stdin of `python3`, `sh`, `bash`, `node`,
`perl` or `ruby`, and the code argument of `-c`/`-e`) and any argument that holds whitespace or `;` (prose, sed
scripts, JSON). In these, only tokens with at least one name component (`/name…` or `~/name…`) are extracted, after
the body is expanded with the table. A token is reported only when it lands in a deny root, another case or arm, or the
corpus; outside-allowlist is not reported. So `a / b`, `Risks / open questions` and `sed '1,/^---$/d' f` pass, and
`open('<operator-home>/.codex/auth.json')` is denied.

**Heredoc bodies.** In an expanding body, each unescaped `$(…)` or backtick is first checked as a command. Then:
- an interpreter's body is code (the rule above);
- any other body is data and is not checked.

**URLs** are not paths; a fetching command (§2) is a `network` violation.

## 4. Tools (D04 b)

| Tool | Checked |
|---|---|
| `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `file_path` / `notebook_path` |
| `Grep`, `Glob`, `LS` | `path`; `pattern` against it (v1) |
| `Bash` | §3 |
| `Skill` (new; legacy entry) | `<namespace>:<name>`, with the namespace one of the arm's pinned plugins (policy `skill_namespaces`); otherwise `namespace` |
| `StructuredOutput` (new; the paired reviewer's schema answer) | nothing |
| `TodoWrite` | ignored |
| `Task`, `Agent` | ignored when the subagent's events are in the stream; otherwise fail-closed |
| `WebFetch`, `WebSearch` | `network` violation |
| Codex `command_execution` / `file_change` | §3 and the change paths (v1) |

Any other tool or item type fails closed. The pilot holds exactly Bash, Read, Grep, Skill and StructuredOutput.

## 5. Regression corpus (D04 c)

**Fixture.** `tests/fixtures/m7-scan-corpus/` freezes the m7-s4 transcripts of p1 and p2 × legacy and paired:
- the orchestrator streams, the legacy reviewer-child streams and the paired evidence streams;
- the case trees they touch;
- a `MANIFEST.json` of sha256 values.

**Rebasing.** The test copies each case into a temporary root and rewrites the recorded prefix
`…/m7-pilot/cases/<case>/<arm>` to it. The policy allows the recorded pinned plugin directory, and it denies a fixed
fake operator HOME.

**Acceptance.** All four arm scans return CLEAN with 0 violations, with no corpus command edited.

**Controls.** Each is one command; each must give the named result.

Violations (`deny`, unless named):
- `cat <operator-home>/.claude/settings.json`;
- `cat ../<sibling-case>/answer.md`;
- `U=<operator-home>/.codex; cat "$U/auth.json"`;
- `U=<operator-home>; false && U=.; cat "$U/x"`;
- `U=.; for f in a b; do cat "$U/x"; U=<operator-home>; done`;
- an expanding heredoc holding `$(cat <operator-home>/.ssh/id_ed25519)`;
- `python3 - <<'PY'` with `open('<operator-home>/.codex/auth.json')`;
- `cat *` where the cwd has a link out of the case (R3);
- `(cd src && pytest); cat ../<sibling-case>/answer.md` (the cwd is restored after the subshell);
- `K=$(cat list.txt); echo ok > "$K"` (fail-closed);
- `f() { cat x; }; f` (fail-closed: a function definition);
- `while true; do cat <operator-home>/.codex/auth.json; done` (a loop body is checked);
- `cat ../../paired/repo/$f`, `for f in $(git ls-files); do diff "$f" ../../paired/repo/$f; done` and
  `cat "$HOME/../<operator>/.codex/$F"` (a known prefix before an opaque suffix is checked as a segment, deny and
  corpus hits only);
- `echo $(echo $(echo $(pwd)))` (fail-closed);
- `Skill` `compass:checkpoint` (namespace);
- `curl https://example.com` (network).

CLEAN:
- `cat "$HOME/.claude/settings.json"` (the fresh HOME);
- `cat '$U/x'`;
- `U=$(uuidgen); cat "$HOME/$U/file"`;
- `echo $(echo $(pwd))`;
- `for f in a.py b.py; do git ls-files -s $f; done`;
- `B=$(git rev-parse HEAD); git diff $B`;
- `pytest > /tmp/out.log; F=$(mktemp); echo x > "$F"`;
- `pytest > "$TMPDIR/out.log"`;
- `ls "$HOME/.codex/"*` (a glob under the fresh HOME);
- `cat "/$x"` (an opaque suffix under `/`: not a deny or corpus hit);
- `for f in src/*.py; do wc -l "$f"; done` with 70 matches (over the cap, the list counts as generated);
- `if [ -f x ]; then cat x; fi`;
- `cd src && cat a.py`;
- `python3 - <<'PY'` whose body holds `# a / b` and `print("x / y")`;
- `sed '1,/^---$/d' f`;
- `pytest --selector "glob:test_*.py"`.

## 6. Step 2 (implementation, after the gate)

- `scripts/m7_scan.py`:
  - replace `check_shell` with §3;
  - add §4's table, the `skill_namespaces`, operator-home, TMPDIR and `corpus` policy keys (the corpus root is
    explicit for a `<root>/<case>/<arm>` layout; it is checked before the allowed places), and the `network` rule;
  - reuse `check_pattern`, `verdict` and the stream readers.
- Add the corpus fixture and `test_m7_scan_corpus.py` (§5). Fold in the parked WIP (`m7_collect.py` and its doc
  section) for re-review.
- Evidence:
  - the four arm scans at 0 violations;
  - every control's result;
  - a census of the tool and item types;
  - an ungated construct census: fail-closed counts and the exclusion rate on further real legacy transcripts,
    including executor rounds.
- Review: Codex sol rounds under the owner rule (cap 3), then the fresh Opus final gate.
