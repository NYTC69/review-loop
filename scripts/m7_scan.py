#!/usr/bin/env python3
"""M7 D-b1 transcript and tool-log scanner, v2 (design: paired_session/docs/m7-scanner-v2.md).

Usage: m7_scan.py POLICY.json OUT.json  |  m7_scan.py --census FILE...
Policy: {"case_dir": P, "case": "cNN", "arm": A, "base": 40-hex, "diff_sha256": 64-hex, "cwd": P (default case_dir/repo),
  "corpus": P (the root every other case and arm lies under; default the parent of case_dir),
  "repo": P (the case repository root, default the cwd), "home": the arm's HOME, "tmpdir": the arm's TMPDIR,
  "operator_home": P (default the account's home), "keys_dir": P, "allow": [runtime dirs], "toolchain": [dirs],
  "deny": [extra dirs], "pinned": [{"dir": P}], "skill_namespaces": [...] (default ["review-loop"]),
  "ignore_tools": [...], "artifacts": [{"name", "path", "kind": "claude-stream" | "codex-events"}]}

Threat model (owner rule, 2026-10-06): users and models are benign; the scan catches realistic accidental crossings
(another case or arm, the answer key, the operator's real config, the network) and must not exclude normal transcripts.
Every tool call in every artifact is extracted; each path is resolved with realpath and must lie inside the case, an
allowed runtime place (the arm's HOME, TMPDIR, /tmp, the pinned plugin copy) or a toolchain dir, and outside the deny
roots, which always win. Bash commands are analysed by bounded resolution within one call (m7_shell parses them):
known values, segments and opaque values; the supported subset is listed in the design §2; anything else fails closed.
The scan is lexical: it does not trace the processes a command starts. Exit 0 CLEAN, 3 VIOLATION, 1 error.
"""
import glob, hashlib, json, os, pwd, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import m7_shell as sh  # noqa: E402

sha = lambda data: hashlib.sha256(data).hexdigest()
DEFAULT_TOOLCHAIN = ["/usr", "/bin", "/sbin", "/opt/homebrew", "/Library/Developer", "/System", "/private/etc/ssl",
                     "/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/urandom"]
DENY_PARTS = ((".compass", "results"), ("real-run",), ("plugins", "cache"))
PATH_TOOLS = {"Read": ("file_path",), "Write": ("file_path",), "Edit": ("file_path",), "MultiEdit": ("file_path",),
              "NotebookEdit": ("notebook_path",), "NotebookRead": ("notebook_path",), "LS": ("path",),
              "Glob": ("path", "pattern"), "Grep": ("path",)}
IGNORE_TOOLS = ("TodoWrite", "StructuredOutput")
SUBAGENT_TOOLS = ("Task", "Agent")   # ignored when the subagent's own events are in the stream
NETWORK_TOOLS = ("WebFetch", "WebSearch")
IGNORE_ITEMS = ("reasoning", "agent_message", "todo_list")
GLOB_CHARS = re.compile(r"[*?\[\]{}]")
UNQUOTED_GLOB = re.compile(r"[*?\[]|\{[^{}]*,[^{}]*\}")   # bash expands braces only with a comma: `{}` is literal
SEG = "\x00"   # a segment inside known text
CODE_TOKEN = re.compile(r"(?<![\w.~:/$-])(~?/[A-Za-z0-9_.@+-][^\s'\"`;|&<>(){}\[\],*?]*)")
CODE_RULES = ("deny-list", "deny-pattern", "corpus-outside-case")
INTERPRETERS = re.compile(r"(python[0-9.]*|node|perl|ruby|sh|bash|zsh|dash|ksh)")
SHELLS = ("sh", "bash", "zsh", "dash", "ksh")
DATE_FMT = re.compile(r"\+(?:%[YmdHMSs]|[-:TZ])*")
MAX_ALTERNATIVES, MAX_ITERATIONS, MAX_COMMANDS, MAX_DEPTH = 64, 64, 4096, 2


def under(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def denied_parts(parts):
    return any(parts[i:i + len(seq)] == seq for seq in DENY_PARTS for i in range(len(parts)))


def expand_braces(pattern, limit=64):
    """Shell brace alternatives of a path pattern, or None when a group cannot be expanded safely."""
    out, todo = [], [pattern]
    while todo:
        text = todo.pop()
        start = text.find("{")
        if start < 0:
            if "}" in text:
                return None
            out.append(text); continue
        depth, commas, end = 0, [], None
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i; break
            elif text[i] == "," and depth == 1:
                commas.append(i)
        if end is None or not commas:   # unbalanced, a literal group or a {a..b} range
            return None
        cuts = [start] + commas + [end]
        todo += [text[:start] + text[a + 1:b] + text[end + 1:] for a, b in zip(cuts, cuts[1:])]
        if len(todo) + len(out) > limit:
            return None
    return sorted(set(out))


class Unsupported(Exception):
    """A construct outside the bounded subset: the call fails closed."""


# --- values: a tuple of pieces ("k", text) | ("s",) | ("o",); a variable holds a list of such values ---------------
def value(*pieces):
    out = []
    for piece in pieces:
        if piece[0] == "k" and out and out[-1][0] == "k":
            out[-1] = ("k", out[-1][1] + piece[1])
        elif piece[0] != "k" or piece[1]:
            out.append(piece)
    return tuple(out)


KNOWN = lambda text: value(("k", text))
SEGMENT, OPAQUE = (("s",),), (("o",),)


class State:
    def __init__(self, variables, cwds):
        self.vars, self.cwds = variables, cwds

    def copy(self):
        return State(dict(self.vars), list(self.cwds))

    def union(self, other):
        for name in set(self.vars) | set(other.vars):
            left, right = self.vars.get(name, [OPAQUE]), other.vars.get(name, [OPAQUE])
            merged = list(dict.fromkeys(left + right))
            if len(merged) > MAX_ALTERNATIVES:
                raise Unsupported("more than %d alternatives for $%s" % (MAX_ALTERNATIVES, name))
            self.vars[name] = merged
        self.cwds = list(dict.fromkeys(self.cwds + other.cwds))


class Scanner:
    def __init__(self, policy, changed):
        real = lambda p: os.path.realpath(os.path.expanduser(str(p)))
        self.case_dir = real(policy["case_dir"])
        # the corpus root: every other case and arm lies under it (checked before the allowed places, so it wins over
        # /tmp or TMPDIR); explicit for layouts such as <root>/<case>/<arm>, else the case directory's parent (gate)
        self.corpus = real(policy["corpus"]) if policy.get("corpus") else os.path.dirname(self.case_dir)
        self.cwd = real(policy.get("cwd") or os.path.join(self.case_dir, "repo"))
        self.repo = real(policy.get("repo") or self.cwd)
        self.home = real(policy.get("home") or self.cwd)
        self.tmpdir = real(policy["tmpdir"]) if policy.get("tmpdir") else None
        operator = policy.get("operator_home") or pwd.getpwuid(os.getuid()).pw_dir
        self.deny = [real(p) for p in [os.path.join(operator, d) for d in (".claude", ".codex", "3Cats")]
                     + [policy["keys_dir"]] + policy.get("deny", [])]
        self.allow = [real(p) for p in policy.get("allow", []) + [self.home, "/tmp", "/private/tmp"]
                      + ([self.tmpdir] if self.tmpdir else [])]
        self.toolchain = [real(p) for p in policy.get("toolchain", DEFAULT_TOOLCHAIN)]
        self.pinned = [real(p["dir"]) for p in policy.get("pinned", [])]
        self.namespaces = set(policy.get("skill_namespaces", ["review-loop"]))
        self.changed = [c for c in changed if c]
        self.ignore = set(policy.get("ignore_tools", IGNORE_TOOLS))
        self.calls, self.violations, self.tool_log = 0, [], []

    # --- paths (v1, with the glob prefix widened to the allowed places) ---------------------------------------------
    def resolve(self, raw, cwd=None):
        text = raw[len("file://"):] if raw.startswith("file://") else raw
        if text == "~" or text.startswith("~/"):
            text = self.home + text[1:]
        return os.path.realpath(os.path.join(cwd or self.cwd, text))

    def verdict(self, path):   # None when allowed, else the policy rule
        if any(under(path, d) for d in self.deny):
            return "deny-list"
        if denied_parts(tuple(Path(path).parts)):
            return "deny-pattern"
        if under(path, self.case_dir):
            return None
        if under(path, self.corpus):
            return "corpus-outside-case"
        for pin in self.pinned:
            if under(path, pin):
                rel = os.path.relpath(path, pin)
                if rel in self.changed or os.path.dirname(rel) in {os.path.dirname(c) for c in self.changed}:
                    return "pinned-copy-of-changed-path"
        if any(under(path, a) for a in self.allow + self.toolchain):
            return None
        return "outside-allowlist"

    def flag(self, cause, raw, resolution, rule, where):
        entry = json.dumps({"artifact": where[0], "line": where[1], "raw": raw})
        self.tool_log.append(entry)
        self.violations.append({"cause": cause, "raw_input": json.dumps(raw)[1:-1], "resolution": resolution,
                                "policy_rule": rule, "artifact": "tool_log", "line": len(self.tool_log),
                                "source": {"artifact": where[0], "line": where[1]}})

    def fail(self, cause, raw, where):
        self.flag(cause, raw, "unresolved", "fail-closed", where)

    def check_path(self, raw, where, cwd=None):   # a single literal path
        path = self.resolve(raw, cwd)
        rule = self.verdict(path)
        if rule:
            self.flag("path outside the case", raw, path, rule, where)

    def check_pattern(self, pattern, where, base=None):
        """A glob or brace pattern. Braces are expanded first (comma lists, nested; a range, a group without a comma or
        more than 64 alternatives fails closed). In each alternative the literal prefix before the first wildcard
        component must lie inside an allowed place (v2: the case or an allowed runtime or toolchain root); no ".." may
        follow a wildcard; no deny component may appear anywhere; and every match on disk now must resolve (realpath, so
        symlinks too) to an allowed place."""
        if not GLOB_CHARS.search(pattern):
            return self.check_path(pattern, where, base)
        alternatives = expand_braces(pattern)
        if alternatives is None:
            return self.fail("brace group that cannot be expanded safely", pattern, where)
        for alt in alternatives:
            parts = alt.split("/")
            cut = next((i for i, part in enumerate(parts) if re.search(r"[*?\[]", part)), None)
            if cut is None:
                self.check_path(alt, where, base); continue
            prefix = "/".join(parts[:cut]) or ("/" if alt.startswith("/") else ".")
            path = self.resolve(prefix, base)
            rule = self.verdict(path) or ("deny-pattern" if denied_parts(tuple(Path(path).parts) + tuple(parts[cut:])) else None)
            if rule or ".." in parts[cut:]:
                self.flag("glob or brace pattern that can expand outside the allowed places", pattern, path,
                          rule or "fail-closed", where); continue
            for match in self.disk_matches(os.path.join(path, *parts[cut:])):
                real = os.path.realpath(match)
                if self.verdict(real):
                    self.flag("glob match resolves outside the allowed places", pattern, real, self.verdict(real), where); break

    @staticmethod
    def disk_matches(pattern):   # the one place that matches a glob on disk (census mode overrides it: no disk walk)
        return glob.glob(pattern, recursive=True)

    def check_segment(self, text, where, cwd, only=None):
        """A path with segments (§3.4): the literal prefix before the first segment must be in an allowed place; no
        component after it may be '..' or a deny component. With `only`, report only those rules."""
        parts = text.split("/")
        cut = next(i for i, part in enumerate(parts) if SEG in part)
        prefix = "/".join(parts[:cut]) or ("/" if text.startswith("/") else ".")
        path = self.resolve(prefix, cwd)
        after = tuple(p.replace(SEG, "x") for p in parts[cut:])
        rule = self.verdict(path) or ("deny-pattern" if denied_parts(tuple(Path(path).parts) + after) else None)
        rule = rule or ("fail-closed" if ".." in after else None)
        if rule and (only is None or rule in only):
            self.flag("path with a generated component outside the allowed places", text.replace(SEG, "<segment>"),
                      path, rule, where)

    def check_code(self, text, where, cwd):
        """Code and prose (§3.4): only tokens with a name component; only deny, other-case/arm and corpus hits."""
        for token in dict.fromkeys(CODE_TOKEN.findall(text)):
            path = self.resolve(token, cwd)
            rule = self.verdict(path)
            if rule in CODE_RULES:
                self.flag("path in code or prose", token, path, rule, where)

    # --- the shell (§3) ---------------------------------------------------------------------------------------------
    def check_shell(self, command, where):
        state = State({"HOME": [KNOWN(self.home)], **({"TMPDIR": [KNOWN(self.tmpdir)]} if self.tmpdir else {})},
                      [self.cwd])
        self.commands = 0
        try:
            self.run_list(sh.parse_command(command), state, where, 0)
        except Unsupported as exc:
            self.fail("shell construct outside the supported subset: " + str(exc), command, where)

    def run_list(self, tree, state, where, depth):
        for node, sep in tree[1]:
            if sep == "&":
                self.run_andor(node, state.copy(), where, depth)
            else:
                self.run_andor(node, state, where, depth)

    def run_andor(self, node, state, where, depth):
        if node[0] == "bad":
            raise Unsupported(node[1])
        self.run_pipe(node[1], state, where, depth)
        for _, pipe in node[2]:   # a conditional subtree: the table is the union of before and after
            branch = state.copy()
            self.run_pipe(pipe, branch, where, depth)
            state.union(branch)

    def run_pipe(self, pipe, state, where, depth):
        if pipe[0] == "bad":
            raise Unsupported(pipe[1])
        cmds = pipe[1]
        if len(cmds) == 1:
            return self.run_command(cmds[0], state, where, depth, None)
        for i, cmd in enumerate(cmds):   # each element runs in its own subshell; nothing leaks
            self.run_command(cmd, state.copy(), where, depth, cmds[i - 1] if i else None)

    def run_command(self, cmd, state, where, depth, _previous):
        kind = cmd[0]
        if kind == "bad":
            raise Unsupported(cmd[1])
        if kind == "subshell":
            self.run_list(cmd[1], state.copy(), where, depth)
            return self.redirections(cmd[2], state, where, depth, None)
        if kind == "group":
            self.run_list(cmd[1], state, where, depth)
            return self.redirections(cmd[2], state, where, depth, None)
        if kind == "if":
            outcomes, current = [], state.copy()
            for condition, body in cmd[1]:
                self.run_list(condition, current, where, depth)
                taken = current.copy()
                self.run_list(body, taken, where, depth)
                outcomes.append(taken)
            if cmd[2] is not None:
                taken = current.copy()
                self.run_list(cmd[2], taken, where, depth)
                outcomes.append(taken)
            else:
                outcomes.append(current)
            merged = outcomes[0]
            for other in outcomes[1:]:
                merged.union(other)
            state.vars, state.cwds = merged.vars, merged.cwds
            return self.redirections(cmd[3], state, where, depth, None)
        if kind == "case":   # every branch from the same table, plus no match; then the union (census-driven, §2)
            self.expand(cmd[1], state, where, depth, split=False)
            outcomes = [state.copy()]
            for branch in cmd[2]:
                taken = state.copy()
                self.run_list(branch, taken, where, depth)
                outcomes.append(taken)
            merged = outcomes[0]
            for other in outcomes[1:]:
                merged.union(other)
            state.vars, state.cwds = merged.vars, merged.cwds
            return self.redirections(cmd[3], state, where, depth, None)
        if kind == "loop":   # while / until: the condition and the body once, then the union (census-driven, §2)
            self.run_list(cmd[1], state, where, depth)
            body = state.copy()
            self.run_list(cmd[2], body, where, depth)
            state.union(body)
            return self.redirections(cmd[3], state, where, depth, None)
        if kind == "for":
            items, generated = [], False
            for word in cmd[2]:
                for fields in self.expand(word, state, where, depth, split=True):
                    for field in fields:
                        if any(p[0] != "k" for p in field[0]):
                            generated = True; continue
                        text = field[0][0][1] if field[0] else ""
                        if field[1]:   # a glob word: checked, then its matches are the list
                            for cwd in state.cwds:
                                self.check_pattern(text, where, cwd)
                                items += sorted(os.path.relpath(m, cwd) if not os.path.isabs(text) else m
                                                for m in self.disk_matches(os.path.join(cwd, text)))
                        else:
                            items.append(text)
            if len(items) > MAX_ITERATIONS:   # gate: a long list is treated as generated (each match was checked)
                items, generated = [], True
            for item in items:
                state.vars[cmd[1]] = [KNOWN(item)]
                self.run_list(cmd[3], state, where, depth)
            if generated:   # a generated list (`for f in $(…)`): the variable is opaque; the body once, then the union
                body = state.copy()
                body.vars[cmd[1]] = [OPAQUE]
                self.run_list(cmd[3], body, where, depth)
                state.union(body)
            return self.redirections(cmd[4], state, where, depth, None)
        return self.simple(cmd, state, where, depth)

    # --- words --------------------------------------------------------------------------------------------------------
    def lookup(self, name, state):
        if name in state.vars:
            return state.vars[name]
        if name in ("$", "PPID", "RANDOM", "?", "#arith"):
            return [SEGMENT]
        if name == "PWD":
            return [KNOWN(c) for c in state.cwds]
        return [OPAQUE]

    def substitution(self, tree, state, where, depth):
        """Check the command inside $(…) (depth ≤ 2) and classify its output (§3.3)."""
        if depth + 1 > MAX_DEPTH:
            raise Unsupported("command substitution deeper than %d" % MAX_DEPTH)
        self.run_list(tree, state.copy(), where, depth + 1)
        words = self.static_pipeline(tree)
        if words is None:
            return [OPAQUE]
        if words == [["pwd"]]:
            return [KNOWN(c) for c in state.cwds]
        if words == [["git", "rev-parse", "--show-toplevel"]]:
            return [KNOWN(self.repo)]
        if words[0] == ["uuidgen"] and (len(words) == 1 or words[1:] in ([["tr", "A-Z", "a-z"]],
                                                                         [["tr", "[:upper:]", "[:lower:]"]])):
            return [SEGMENT]
        if len(words) == 1 and words[0][0] == "date" and words[0][1:] and \
                all(a == "-u" for a in words[0][1:-1]) and DATE_FMT.fullmatch(words[0][-1]):
            return [SEGMENT]
        if len(words) == 1 and words[0][0] == "mktemp":
            return [value(("k", (self.tmpdir or "/tmp") + "/"), ("s",))]
        return [OPAQUE]

    @staticmethod
    def static_pipeline(tree):
        """The literal words of a one-pipeline list with no redirections, else None."""
        if len(tree[1]) != 1 or tree[1][0][0][0] != "andor" or tree[1][0][0][2]:
            return None
        out = []
        for cmd in tree[1][0][0][1][1]:
            if cmd[0] != "simple" or cmd[1] or cmd[3]:
                return None
            texts = [sh.plain(w) if sh.plain(w) is not None else
                     ("".join(p.text for p in w) if all(isinstance(p, sh.Lit) for p in w) else None) for w in cmd[2]]
            if None in texts:
                return None
            out.append(texts)
        return out

    def expand(self, word, state, where, depth, split):
        """A word's alternatives; each alternative is a list of fields (pieces, unquoted glob?)."""
        alternatives = [[]]   # each: list of (piece, splittable, globbable)
        for index, part in enumerate(word):
            if isinstance(part, sh.Bad):
                raise Unsupported(part.reason)
            if isinstance(part, sh.Lit):
                text = part.text
                if index == 0 and not part.quoted and text.startswith("~"):
                    if text == "~" or text.startswith("~/"):
                        options = [[(p, False, False) for p in v] + [(("k", text[1:]), False, not part.quoted)]
                                   for v in self.lookup("HOME", state)]
                        alternatives = [a + o for a in alternatives for o in options]
                        continue
                    raise Unsupported("~user")
                alternatives = [a + [(("k", text), False, not part.quoted)] for a in alternatives]
                continue
            if isinstance(part, sh.Param):   # ${NAME:-word}: the variable's value or the word's
                values = list(dict.fromkeys((state.vars.get(part.name) or []) + [
                    fields[0][0] if fields else KNOWN("") for fields in self.expand(part.default, state, where, depth, split=False)]))
            elif isinstance(part, sh.Var):
                values = self.lookup(part.name, state)
            else:
                values = self.substitution(part.tree, state, where, depth)
            alternatives = [a + [(p, not part.quoted, not part.quoted) for p in v] for a in alternatives for v in values]
            if len(alternatives) > MAX_ALTERNATIVES:
                raise Unsupported("more than %d alternatives in one word" % MAX_ALTERNATIVES)
        out = []
        for alt in alternatives:
            fields, current, globbed = [], [], False
            for piece, splittable, globbable in alt:
                if piece[0] == "k" and split and splittable and re.search(r"\s", piece[1]):
                    chunks = re.split(r"\s+", piece[1])
                    for n, chunk in enumerate(chunks):
                        if n:
                            if current:
                                fields.append((value(*current), globbed))
                            current, globbed = [], False
                        if chunk:
                            current.append(("k", chunk))
                            globbed = globbed or bool(globbable and UNQUOTED_GLOB.search(chunk))
                    continue
                current.append(piece)
                if piece[0] == "k" and globbable and UNQUOTED_GLOB.search(piece[1]):
                    globbed = True
            if current or not split:
                fields.append((value(*current), globbed))
            out.append(fields)
        return out

    @staticmethod
    def text_of(pieces):
        return "".join(p[1] if p[0] == "k" else SEG for p in pieces if p[0] != "o")

    # --- a simple command ---------------------------------------------------------------------------------------------
    def simple(self, cmd, state, where, depth):
        _, assigns, words, redirs = cmd
        self.commands += 1
        if self.commands > MAX_COMMANDS:
            raise Unsupported("more than %d commands" % MAX_COMMANDS)
        if not words:   # assignments in sequence replace the set
            for name, word in assigns:
                state.vars[name] = [fields[0][0] if fields else KNOWN("")
                                    for fields in self.expand(word, state, where, depth, split=False)]
            return self.redirections(redirs, state, where, depth, None)
        prefix = {}   # a prefix assignment applies to this command only: its environment (a nested shell's HOME, say)
        for name_, word in assigns:
            prefix[name_] = [fields[0][0] if fields else KNOWN("")
                             for fields in self.expand(word, state, where, depth, split=False)]
        first = self.expand(words[0], state, where, depth, split=True)
        if any(not alt or any(p[0] != "k" for p in alt[0][0]) for alt in first):
            raise Unsupported("generated command name")
        heads = list(dict.fromkeys((self.text_of(alt[0][0]), tuple(alt[1:])) for alt in first))   # (name, split-off args)
        bases = {os.path.basename(name) for name, _ in heads}
        for name, _ in heads:
            if "/" in name:   # a relative command path is checked against every cwd (R3)
                for cwd in state.cwds:
                    self.check_path(name, where, cwd)
        if bases & {"eval", "source", ".", "exec", "pushd", "popd"}:
            raise Unsupported(sorted(bases & {"eval", "source", ".", "exec", "pushd", "popd"})[0])
        stateful = {"export", "declare", "local", "typeset", "readonly", "cd", "read", "unset"}
        if len(bases) > 1 and bases & stateful:
            raise Unsupported("a generated name for a shell builtin")
        base = next(iter(bases))
        if base in ("export", "declare", "local", "typeset", "readonly"):
            for word in words[1:]:
                text = word[0].text if word and isinstance(word[0], sh.Lit) else ""
                m = sh.NAME_RE.match(text)
                if m and text[m.end():m.end() + 1] == "=":
                    rest = sh.merge(([sh.Lit(text[m.end() + 1:], False)] if text[m.end() + 1:] else []) + list(word[1:]))
                    state.vars[m.group(0)] = [f[0][0] if f else KNOWN("")
                                              for f in self.expand(rest, state, where, depth, split=False)]
            return self.redirections(redirs, state, where, depth, base)
        args_by_alt = [[]]   # every word expanded once; the argument lists are the product of their alternatives
        for word in words[1:]:
            options = self.expand(word, state, where, depth, split=True)
            args_by_alt = [a + o for a in args_by_alt for o in options]
            if len(args_by_alt) > MAX_ALTERNATIVES:
                raise Unsupported("more than %d alternatives in one command" % MAX_ALTERNATIVES)
        if base == "cd":
            self.change_dir(args_by_alt, state)
            return self.redirections(redirs, state, where, depth, base)
        if base in ("read", "unset"):
            for args in args_by_alt:
                for field in args:
                    text = self.text_of(field[0])
                    if sh.NAME_RE.fullmatch(text):
                        state.vars[text] = [OPAQUE]
            return self.redirections(redirs, state, where, depth, base)
        checked, consumers = set(), set()   # every command name and every argument list is classified (R2)
        for name, leading in heads:
            base = os.path.basename(name)
            consumers.add(base)
            for args in args_by_alt:
                self.dispatch(name, base, list(leading) + args, state, where, depth, prefix, checked)
        consumer = next((c for c in sorted(consumers) if INTERPRETERS.fullmatch(c)), base)
        return self.redirections(redirs, state, where, depth, consumer)

    def dispatch(self, name, base, args, state, where, depth, prefix, checked):
        """One command name with one argument list: the network, nested-shell, interpreter-code and argument rules."""
        texts = [self.text_of(f[0]) for f in args]
        code_index = None
        if base in ("curl", "wget") or (base == "git" and self.git_subcommand(texts)
                                        in ("clone", "fetch", "pull", "ls-remote", "push")):
            if ("network", name, tuple(texts)) not in checked:
                checked.add(("network", name, tuple(texts)))
                self.flag("network command", " ".join([name] + texts), "network", "network", where)
        if base in SHELLS:
            flag_at = next((i for i, a in enumerate(texts) if re.fullmatch(r"-[a-z]*c[a-z]*", a)), None)
            if flag_at is not None:
                if flag_at + 1 >= len(args) or any(p[0] != "k" for p in args[flag_at + 1][0]):
                    raise Unsupported("generated shell -c text")
                if depth + 1 > MAX_DEPTH:
                    raise Unsupported("nested shell deeper than %d" % MAX_DEPTH)
                inherited = {k: v for k, v in state.vars.items() if k in ("HOME", "TMPDIR")}
                inner = State({**inherited, **prefix}, list(state.cwds))
                self.run_list(sh.parse_command(texts[flag_at + 1]), inner, where, depth + 1)
                code_index = flag_at + 1
        elif INTERPRETERS.fullmatch(base):
            code_index = next((i + 1 for i, a in enumerate(texts) if a in ("-c", "-e")), None)
        for i, field in enumerate(args):
            key = ("code" if i == code_index else "arg", field)
            if key in checked:
                continue
            checked.add(key)
            if i == code_index:
                if base not in SHELLS:
                    for cwd in state.cwds:
                        self.check_code(self.text_of(field[0]).replace(SEG, "x"), where, cwd)
                continue
            self.check_argument(field, state, where)

    @staticmethod
    def git_subcommand(args):
        """git's subcommand after its global options (-C DIR, -c KEY=VALUE, --git-dir=…, --no-pager, …)."""
        skip = False
        for arg in args:
            if skip:
                skip = False
            elif arg in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--exec-path"):
                skip = True
            elif not arg.startswith("-"):
                return arg
        return ""

    def change_dir(self, args_by_alt, state):
        targets = []
        for args in args_by_alt:
            args = [a for a in args if self.text_of(a[0]) not in ("-L", "-P")]
            if not args:
                targets.append(None); continue
            if len(args) > 1 or any(p[0] != "k" for p in args[0][0]) or args[0][1] or self.text_of(args[0][0]) == "-":
                raise Unsupported("cd to a generated place")
            targets.append(self.text_of(args[0][0]))
        cwds = []
        for cwd in state.cwds:
            for target in targets:
                path = self.home if target is None else self.resolve(target, cwd)
                if self.verdict(path):
                    raise Unsupported("cd outside the allowed places")
                cwds.append(path)
        state.cwds = list(dict.fromkeys(cwds))

    def check_argument(self, field, state, where):
        pieces, globbed = field
        if any(p[0] == "o" for p in pieces):
            seen_opaque = False
            for p in pieces:
                if p[0] == "o":
                    seen_opaque = True
                elif seen_opaque and p[0] == "k" and "/" in p[1]:
                    raise Unsupported("generated path prefix")
            first = next(i for i, p in enumerate(pieces) if p[0] == "o")
            known = self.text_of(pieces[:first])
            text = "".join(p[1] if p[0] == "k" else SEG for p in pieces)
            if known.startswith("-") and "=" in known:   # --output=../x/$f: the value after "=" is the path (owner)
                known, text = known.split("=", 1)[1], text.split("=", 1)[1]
            if "/" in known:   # gate: a known prefix with an opaque suffix (../../paired/repo/$f):
                for cwd in state.cwds:   # the opaque part as a segment; only deny and corpus hits (not "/$x")
                    self.check_segment(text, where, cwd, only=CODE_RULES)
            return   # a whole-word opaque value: a residual (§2)
        text = self.text_of(pieces)
        if re.search(r"[\s;]", text):   # code or prose: only deny, other-case and corpus hits
            for cwd in state.cwds:
                self.check_code(text.replace(SEG, "x"), where, cwd)
            return
        if re.match(r"^[a-z][a-z0-9+.-]*://", text) and not text.startswith("file://"):
            return
        if text.startswith("-") and "=" in text:
            text = text.split("=", 1)[1]
            globbed = globbed and bool(UNQUOTED_GLOB.search(text))
            if not text:
                return
        elif text.startswith("-"):
            return
        segment = SEG in text
        exists = not segment and not globbed and any(os.path.lexists(os.path.join(c, text)) for c in state.cwds)
        if not ("/" in text or globbed or exists or text == ".."):
            return
        if globbed and segment:
            raise Unsupported("a glob with a generated component")
        for cwd in state.cwds:
            if globbed:
                self.check_pattern(text, where, cwd)
            elif segment:
                self.check_segment(text, where, cwd)
            else:
                self.check_path(text, where, cwd)

    def redirections(self, redirs, state, where, depth, consumer):
        for redir in redirs:
            if redir.op in ("<<", "<<-"):
                if redir.body is None:
                    raise Unsupported("heredoc without its delimiter")
                texts = [redir.body]
                if redir.expanding:   # every alternative (Var, Param, substitution), as for any word; opaque is dropped
                    texts = [self.text_of(fields[0][0]).replace(SEG, "x") for fields in
                             self.expand(sh.body_parts(redir.body), state, where, depth, split=False)]
                if consumer and INTERPRETERS.fullmatch(consumer):
                    for text in dict.fromkeys(texts):
                        for cwd in state.cwds:
                            self.check_code(text, where, cwd)
                continue
            fields = [f for alt in self.expand(redir.target, state, where, depth, split=False) for f in alt]
            for pieces, globbed in fields:
                if any(p[0] == "o" for p in pieces):
                    raise Unsupported("generated redirection target")
                text = self.text_of(pieces)
                if redir.op in (">&", "<&") and re.fullmatch(r"\d+|-", text):
                    continue
                for cwd in state.cwds:   # a redirection target is always a path (§3.4)
                    if SEG in text:
                        self.check_segment(text, where, cwd)
                    else:
                        self.check_path(text, where, cwd)

    # --- tools and streams --------------------------------------------------------------------------------------------
    def tool(self, name, data, where, tool_id=None):
        self.calls += 1
        self.tool_log.append(json.dumps({"artifact": where[0], "line": where[1], "tool": name, "input": data}))
        if name in self.ignore:
            return
        if name in SUBAGENT_TOOLS:
            self.subagents[tool_id] = where; return
        if name in NETWORK_TOOLS:
            return self.flag("network tool " + name, json.dumps(data), "network", "network", where)
        if name == "Skill":
            skill = data.get("skill") if isinstance(data.get("skill"), str) else ""
            if skill.partition(":")[0] not in self.namespaces or ":" not in skill:
                self.flag("skill outside the arm's pinned plugins", skill, "namespace", "namespace", where)
            return
        if name == "Bash":
            if not isinstance(data.get("command"), str):
                return self.fail("Bash call without a command", json.dumps(data), where)
            return self.check_shell(data["command"], where)
        if name not in PATH_TOOLS:
            return self.fail("unrecognised tool " + str(name), json.dumps(data), where)
        base = data.get("path") if isinstance(data.get("path"), str) and data.get("path") else None
        for field in PATH_TOOLS[name]:
            value_ = data.get(field)
            if field == "pattern" and isinstance(value_, str) and value_:   # relative to the tool's path (or the cwd)
                self.check_pattern(value_, where, self.resolve(base) if base else None)
            elif isinstance(value_, str) and value_:
                self.check_path(value_, where)

    def claude_stream(self, name, lines):
        pending, parents, self.subagents = {}, set(), {}
        for number, line in enumerate(lines, 1):
            try:
                event = json.loads(line)
            except ValueError:
                self.fail("unparseable transcript line", line[:200], (name, number)); continue
            if not isinstance(event, dict):
                self.fail("non-object transcript line", line[:200], (name, number)); continue
            if event.get("parent_tool_use_id"):
                parents.add(event["parent_tool_use_id"])
            block = (event.get("event") or {}).get("content_block") if event.get("type") == "stream_event" else None
            if isinstance(block, dict) and block.get("type") == "tool_use":
                pending.setdefault(block.get("id"), (name, number))   # partial: its input arrives in the assistant message
            if event.get("type") != "assistant":
                continue
            for item in (event.get("message") or {}).get("content") or []:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    pending.pop(item.get("id"), None)
                    data = item.get("input")
                    if not isinstance(data, dict):
                        self.fail("tool call without an input object", json.dumps(item), (name, number)); continue
                    self.tool(item.get("name"), data, (name, number), item.get("id"))
        for tool_id, where in pending.items():
            self.fail("tool call never completed in the transcript", str(tool_id), where)
        for tool_id, where in self.subagents.items():
            if tool_id not in parents:
                self.fail("subagent call without its subagent events in the stream", str(tool_id), where)

    def codex_events(self, name, lines):
        started = {}   # an item that started but never completed may already have run: fail closed
        for number, line in enumerate(lines, 1):
            try:
                event = json.loads(line)
            except ValueError:
                self.fail("unparseable event line", line[:200], (name, number)); continue
            if not isinstance(event, dict) or event.get("type") not in ("item.started", "item.completed"):
                continue
            item = event.get("item") or {}
            kind = item.get("type")
            if kind in IGNORE_ITEMS:
                continue
            if not isinstance(item.get("id"), str):
                self.fail("tool item without an id", json.dumps(item), (name, number)); continue
            if event["type"] == "item.started":
                started[item["id"]] = (json.dumps(item), (name, number)); continue
            started.pop(item["id"], None)
            self.calls += 1
            self.tool_log.append(json.dumps({"artifact": name, "line": number, "item": item}))
            if kind == "command_execution" and isinstance(item.get("command"), str):
                self.check_shell(item["command"], (name, number))
            elif kind == "file_change" and isinstance(item.get("changes"), list):
                for change in item["changes"]:
                    path = change.get("path") if isinstance(change, dict) else None
                    if isinstance(path, str):
                        self.check_path(path, (name, number))
                    else:
                        self.fail("file change without a path", json.dumps(change), (name, number))
            elif kind in ("web_search",):
                self.flag("network item " + kind, json.dumps(item), "network", "network", (name, number))
            else:
                self.fail("unrecognised item type " + str(kind), json.dumps(item), (name, number))
        for raw, where in started.values():
            self.fail("tool item started but never completed in the log", raw, where)


def changed_paths(repo):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    out = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"],
                         cwd=repo, check=True, capture_output=True,
                         env={**clean, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}).stdout
    return [p for p in out.decode().split("\0") if p]


def scan(policy, changed=None):
    """Scan every artifact the policy names; returns the scan record (status CLEAN or VIOLATION)."""
    if changed is None:
        changed = changed_paths(os.path.join(policy["case_dir"], "repo"))
    scanner, artifacts = Scanner(policy, changed), {}
    if not policy.get("artifacts"):
        raise SystemExit("policy names no artifact: nothing would be scanned")
    for art in policy["artifacts"]:
        data = Path(art["path"]).read_bytes()
        lines = data.decode("utf-8", "replace").splitlines()
        artifacts[art["name"]] = {"path": str(art["path"]), "sha256": sha(data), "lines": len(lines), "kind": art["kind"]}
        if art["kind"] == "claude-stream":
            scanner.claude_stream(art["name"], lines)
        elif art["kind"] == "codex-events":
            scanner.codex_events(art["name"], lines)
        else:
            raise SystemExit("unknown artifact kind: " + str(art["kind"]))
    tool_log = "\n".join(scanner.tool_log) + "\n"
    return {"kind": "m7-d-b1-scan", "version": 2, "case": policy["case"], "arm": policy["arm"], "base": policy["base"],
            "diff_sha256": policy["diff_sha256"], "status": "VIOLATION" if scanner.violations else "CLEAN",
            "tool_calls": scanner.calls, "changed_paths": changed, "artifacts": artifacts,
            "tool_log": {"content": tool_log, "sha256": sha(tool_log.encode())}, "violations": scanner.violations}


def exclusion_evidence(record):
    """m7_grade's exclusion_evidence[case] for a VIOLATION scan (reason "D-b1: ..."). The transcript bundle holds every
    scanned artifact's raw text with its file digest (re-read and checked against the scan); the tool log is the
    scanner's own per-call log the violations point into."""
    if record["status"] != "VIOLATION":
        raise SystemExit("a CLEAN scan voids nothing")
    bundle = {}
    for name, art in sorted(record["artifacts"].items()):
        data = Path(art["path"]).read_bytes()
        if sha(data) != art["sha256"]:
            raise SystemExit("artifact changed since the scan: " + name)
        bundle[name] = {"path": art["path"], "sha256": art["sha256"], "content": data.decode("utf-8", "replace")}
    transcript = json.dumps(bundle, sort_keys=True)
    first = record["violations"][0]
    return {"case": record["case"], "reason": "D-b1: " + first["cause"], "base": record["base"],
            "diff_sha256": record["diff_sha256"], "arm": record["arm"], "cause": first["cause"],
            "artifacts": {"transcript": {"content": transcript, "sha256": sha(transcript.encode())},
                          "tool_log": dict(record["tool_log"])},
            "violations": [{k: v[k] for k in ("cause", "raw_input", "resolution", "policy_rule", "artifact", "line")}
                           for v in record["violations"]]}


def census(paths):
    """The ungated construct census (§6): only the fail-closed causes of every Bash or Codex command in real transcripts
    (Claude stream-json or session logs, Codex events), with every path allowed; a transcript with one is 'excluded'."""
    import tempfile
    empty = tempfile.mkdtemp(prefix="m7-census-")   # the cwd and HOME: globs match nothing, so no disk walk
    policy = {"case_dir": empty, "case": "census", "arm": "census", "base": "0" * 40, "diff_sha256": "0" * 64,
              "keys_dir": "/nonexistent-keys", "operator_home": "/nonexistent-operator", "toolchain": ["/"],
              "skill_namespaces": [], "ignore_tools": list(IGNORE_TOOLS) + ["Skill"], "cwd": empty, "home": empty,
              "tmpdir": empty}
    report = {"transcripts": 0, "excluded": 0, "commands": 0, "fail_closed": {}, "examples": {}}
    class AllowAll(Scanner):   # constructs only: no place is denied, and no glob is matched on disk
        def verdict(self, path):
            return None

        @staticmethod
        def disk_matches(pattern):
            return []
    for path in paths:
        scanner = AllowAll(policy, [])
        lines = Path(path).read_text(errors="replace").splitlines()
        kind = "codex-events" if any('"item.completed"' in l for l in lines[:400]) else "claude-stream"
        (scanner.codex_events if kind == "codex-events" else scanner.claude_stream)(path, lines)
        causes = [v["cause"].split(":")[0] + (":" + v["cause"].split(":")[1] if ":" in v["cause"] else "")
                  for v in scanner.violations if v["policy_rule"] == "fail-closed"
                  and not v["cause"].startswith(("tool call never completed", "subagent call", "unrecognised"))]
        report["transcripts"] += 1
        report["commands"] += sum(1 for row in scanner.tool_log if '"Bash"' in row or '"command_execution"' in row)
        report["excluded"] += bool(causes)
        for cause in causes:
            report["fail_closed"][cause] = report["fail_closed"].get(cause, 0) + 1
        for v in scanner.violations:
            if v["policy_rule"] == "fail-closed" and len(report["examples"].setdefault(v["cause"], [])) < 3:
                report["examples"][v["cause"]].append(json.loads('"%s"' % v["raw_input"])[:160])
    os.rmdir(empty)
    return report


if __name__ == "__main__":
    if sys.argv[1:2] == ["--census"]:
        print(json.dumps(census(sys.argv[2:]), indent=1, sort_keys=True))
        sys.exit(0)
    record = scan(json.loads(Path(sys.argv[1]).read_text()))
    Path(sys.argv[2]).write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"status": record["status"], "tool_calls": record["tool_calls"], "violations": len(record["violations"])}))
    sys.exit(3 if record["violations"] else 0)
