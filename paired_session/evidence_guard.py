"""v297-eg: the typed-operation evidence guard (Dot task 19, DESIGN.md option (a), bounded structured admission).

Each observed model tool call becomes direct operations: a structured file read or write; a bounded search whose pattern stays text and
whose scope is checked recursively; a command whose argv0, words (and the paths embedded in them), redirection targets and cwd are its
direct operands; or UNKNOWN when the guard cannot establish them. Protected operands are checked first, and command membership never admits
anything. ALLOW only means that no protected direct target was found: it is no tool permission and says nothing about what a program reads
internally. The guard runs after the call (it cannot undo a read) and sees neither hardlinks nor races."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import unquote as _unquote

ALLOW, PROTECTED, UNKNOWN = 'ALLOW', 'PROTECTED', 'UNKNOWN'
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
OPS = ('&>>', '<<<', '&&', '||', ';;', '|&', '>>', '&>', '>|', '>&', '<&', '<>', '|', '&', ';', '>', '<', '(', ')')   # longest first
SEPARATORS, REDIRECTS = {'&&', '||', '|', '|&', ';', '&'}, {'>', '>>', '<', '&>', '&>>', '>|', '<>', '>&', '<&', '<<<'}
KEYWORDS = {'if', 'then', 'else', 'elif', 'fi', 'for', 'while', 'until', 'do', 'done', 'case', 'esac', 'select', 'function', '{', '}',
            '!', '[[', ']]', 'coproc'}
HEREDOC = re.compile(r"""<<(-?)[ \t]*(?:'([^'\n]*)'|"([^"\n]*)"|(\\?)([^\s;&|<>()'"]+))""")
OPAQUE = {'eval', 'source', '.', 'exec', 'xargs', 'parallel', 'sudo', 'doas', 'su', 'pushd', 'popd', 'watch', 'ssh', 'script', 'trap', 'alias'}
SCOPED_NAMES = {'export', 'unset', 'declare', 'typeset', 'readonly', 'local'}            # change the named variables only
MUTATING = SCOPED_NAMES | {'read', 'set', 'mapfile', 'readarray', 'getopts'}
WRAPPERS = {'nohup': set(), 'command': set(), 'builtin': set(), 'time': set(), 'nice': {'-n', '--adjustment'},
            'timeout': {'-s', '--signal', '-k', '--kill-after'}, 'stdbuf': {'-i', '-o', '-e'}}
LAUNCHERS = {'busybox': (), 'npx': (), 'bunx': (), 'uv': ('run',), 'poetry': ('run',), 'pipenv': ('run',), 'pnpm': ('exec', 'dlx'),
             'yarn': ('exec', 'dlx')}                                             # the words after them are a command of their own
# Interpreter families: (letters that run inline code, options that take the next word as a value).
INTERPRETERS = {'python': ('c', {'-W', '-X', '--check-hash-based-pycs'}), 'pypy': ('c', {'-W', '-X'}),
                'sh': ('c', {'-o', '-O', '+o', '+O', '--rcfile', '--init-file'}), 'bash': ('c', {'-o', '-O', '+o', '+O', '--rcfile', '--init-file'}),
                'zsh': ('c', {'-o', '+o'}), 'dash': ('c', {'-o', '+o'}), 'ksh': ('c', {'-o', '+o'}), 'fish': ('c', {'-C', '--init-command'}),
                'node': ('ep', {'-r', '--require', '--import', '--loader', '--experimental-loader', '-C', '--conditions', '--env-file', '--title'}),
                'deno': ('e', set()), 'bun': ('e', {'-r', '--preload'}),
                'perl': ('eE', {'-I', '-M', '-m'}), 'ruby': ('e', {'-I', '-r', '-E'}), 'php': ('rRBE', {'-d', '-c', '-z'}), 'lua': ('e', {'-l'}),
                'osascript': ('e', {'-l'}), 'Rscript': ('e', set()), 'rbash': ('c', {'-o', '-O', '+o', '+O', '--rcfile', '--init-file'}),
                **{shell: ('c', set()) for shell in ('csh', 'tcsh', 'ash', 'mksh', 'yash', 'posh')}}
LOADS_PROGRAM = {'node': {'-r', '--require', '--import', '--loader', '--experimental-loader'}, 'bun': {'-r', '--preload'},
                 'ruby': {'-r'}, 'php': {'-c', '-z'}}                         # value options whose file is code (php -c: an ini can prepend)
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh', 'rbash'}                         # a script file is fine; -c, -s, stdin and option values are opaque
RARE_SHELLS = {'csh', 'tcsh', 'ash', 'mksh', 'yash', 'posh', 'fish'}            # not parsed: opaque unless they only print version or help
PRINT_ONLY = {'python': ('--version', '-V', '--help', '-h'), 'pypy': ('--version', '-V', '--help', '-h'),
              'node': ('--version', '-v', '--help', '-h'), 'nodejs': ('--version', '-v', '--help', '-h')}   # else --version/--help only
SHELL_FLAGS, SHELL_LONG = set('euxvnfl'), {'--norc', '--noprofile', '--posix', '--login'}   # take no value in every POSIX shell here
INTERPRETER_FLAGS = {'python': set('uBOqIEsSbd'), 'pypy': set('uBOqIEsSbd'), 'deno': set('Aq'), 'bun': set(), 'perl': set('wWX'),
                     'ruby': set('wW')}   # short flags without values
INTERPRETER_LONG = {'node': {'--test', '--run', '--experimental-vm-modules', '--enable-source-maps', '--no-warnings', '--trace-warnings',
                             '--no-deprecation', '--expose-gc', '--trace-uncaught', '--version', '--help', '--inspect', '--inspect-brk',
                             '--max-old-space-size', '--stack-size', '--unhandled-rejections', '--experimental-specifier-resolution'},
                    'deno': {'--quiet', '--watch', '--no-check', '--unstable', '--version', '--help'},
                    'bun': {'--watch', '--hot', '--version', '--help'}}             # long flags, alone or as --flag=VALUE (the value is an operand)
INTERPRETER_LONG['nodejs'], INTERPRETERS['nodejs'] = INTERPRETER_LONG['node'], INTERPRETERS['node']
SEARCH_VALUES = {'grep': {'-A', '-B', '-C', '-m', '-d', '-D', '--after-context', '--before-context', '--context', '--max-count', '--label'},
                 'rg': {'-A', '-B', '-C', '-m', '-g', '-t', '-T', '-j', '-M', '-E', '-r', '--glob', '--iglob', '--type', '--type-not', '--max-count',
                        '--context', '--after-context', '--before-context', '--max-depth', '--threads', '--max-columns', '--encoding', '--sort',
                        '--sortr', '--replace', '--pre', '--pre-glob', '--type-add', '--max-filesize', '--engine'},
                 'ag': {'-A', '-B', '-C', '-m', '-G', '-p', '--ignore', '--ignore-dir', '--depth', '--path-to-ignore', '--file-search-regex'},
                 'ack': {'-A', '-B', '-C', '-m', '--type', '--ignore-dir', '--ignore-file'}}
NO_PATTERN = {'rg': {'--files', '--type-list'}, 'ag': {'-g', '-l'}, 'ack': {'-f', '-g'}}   # these modes take no search pattern word
NON_RECURSIVE = {'cd', 'stat', 'test', '[', 'mkdir', 'rmdir', 'realpath', 'readlink', 'file', 'basename', 'dirname', 'pwd', 'echo', 'printf',
                 'true', 'false', 'which', 'type', 'touch', 'cat', 'head', 'tail', 'less', 'more', 'wc', 'sort', 'uniq', 'cut', 'tr', 'sed',
                 'awk', 'gawk', 'jq', 'yq', 'cmp', 'comm', 'nl', 'od', 'xxd', 'hexdump', 'strings', 'sleep', 'date', 'tee', 'ln'}
RECURSIVE_FLAGS = {'ls': 'R', 'cp': 'rRa', 'rm': 'rR', 'chmod': 'R', 'chown': 'R', 'chgrp': 'R', 'diff': 'r', 'zip': 'r',
                   'grep': 'rR', 'egrep': 'rR', 'fgrep': 'rR'}                    # direct unless a flag makes them recurse
FOLLOW_FLAGS = {'grep': 'R', 'egrep': 'R', 'fgrep': 'R', 'find': 'L', 'rg': 'L', 'ag': 'f', 'tree': 'l', 'du': 'L', 'cp': 'L', 'rsync': 'Lk',
                'fd': 'L', 'fdfind': 'L', 'ls': 'L'}                              # these follow symlinks while they recurse
DEFAULT_SCOPE = {'rg', 'ag', 'ack', 'find', 'tree', 'du', 'fd', 'fdfind'}        # these search the cwd when given no path
FILTER_OPTS = {'--exclude', '--include', '--exclude-dir', '--include-dir'}   # in these commands only, the value is a filter pattern
FILTER_COMMANDS = {'rsync', 'tar', 'grep', 'egrep', 'fgrep', 'du', 'ag', 'ack'}   # (gcc --include, for one, reads the file)
DIR_OPTS = {'-C', '--directory', '--chdir', '--cwd', '--prefix', '--work-tree', '--git-dir', '--rootdir', '--package-path'}
TEXT_TOOLS = {'StructuredOutput', 'TodoWrite', 'Agent', 'Task', 'ExitPlanMode', 'BashOutput', 'KillShell', 'KillBash'}
PATH_TOOLS = {'Read': 'file_path', 'Write': 'file_path', 'Edit': 'file_path', 'MultiEdit': 'file_path', 'NotebookEdit': 'notebook_path',
              'NotebookRead': 'notebook_path', 'LS': 'path'}
CODE_MODE = re.compile(r'\s*const\s+(\w+)\s*=\s*await\s+tools\.exec_command\(')
CODE_MODE_KEYS = {'cmd', 'workdir', 'yield_time_ms', 'max_output_tokens', 'justification', 'timeout_ms'}


class Unresolved(Exception):
    """Input whose direct scope the guard cannot establish: UNKNOWN, which the call site turns into HOLD."""


class OpaqueProgram(Unresolved):
    """Inline interpreter code or a program read from stdin: UNKNOWN, unless the whole command is one the operator configured (option (a):
    admitted at configuration time, after its operands, cwd and redirections were checked like any other command's)."""


@dataclass(frozen=True)
class Context:
    evidence: Path                       # the protected roots: the run's evidence and rounds directories
    rounds: Path
    cwd: Optional[Path]                  # this invocation's cwd (the Popen cwd); None = unknown
    env: Mapping[str, str] = field(default_factory=dict)   # the env the CLI child got: the only trusted values for $NAME and ~
    writable_roots: tuple = ()           # where a model can plant a symlink; searched for aliases into a protected root
    configured: tuple = ()               # the exact configured test and reviewer commands (admitted at configuration time)


def first_violation(calls: list, ctx: Context) -> Optional[tuple]:
    """(PROTECTED, reason) if any call has a protected operand, else the first (UNKNOWN, reason), else None. Warm-up passes gather every
    cwd candidate and changed variable of the turn until nothing grows, so the order the stream lists the calls in does not matter."""
    found = [verdict for verdict in turn_verdicts(calls, ctx) if verdict[0] != ALLOW]
    return next((v for v in found if v[0] == PROTECTED), found[0] if found else None)


def turn_verdicts(calls: list, ctx: Context) -> list:
    """(verdict, reason) per call, after the warm-up passes of first_violation (v297-eg-wire: the hybrid needs each call's verdict)."""
    guard = TurnGuard(ctx)
    for _ in range(len(calls) + 2):
        before = (set(guard.cwds), set(guard.tainted))
        for call in calls: guard.classify(call)
        if (set(guard.cwds), set(guard.tainted)) == before: break
    return [guard.classify(call) for call in calls]


def code_mode_exec(code) -> Optional[dict]:
    """The one literal code-mode form: `const r = await tools.exec_command({JSON}); text(r | r.output | JSON.stringify(r));`."""
    match = CODE_MODE.match(code) if isinstance(code, str) else None
    if not match: return None
    rest = code[match.end():].lstrip()
    try: args, length = json.JSONDecoder().raw_decode(rest)
    except ValueError: return None
    var = re.escape(match.group(1))
    tail = r'\s*\);\s*text\((?:' + var + r'(?:\.output)?|JSON\.stringify\(' + var + r'\))\);\s*'
    ok = re.fullmatch(tail, rest[length:]) and isinstance(args, dict) and set(args) <= CODE_MODE_KEYS and isinstance(args.get('cmd'), str)
    return args if ok else None


def _canonical(path: Path) -> Path:
    try: return Path(os.path.realpath(path))
    except (OSError, ValueError) as exc: raise Unresolved(f'path {path!s}: {exc}') from exc


def _braces(text: str, limit: int = 64) -> list:
    """Bash brace expansion of a word with an unquoted `{` (word() calls it only then): the word for each alternative of its comma
    groups. A word that mixes quoted and unquoted brace characters never gets here (_tokens). A sequence other than numbers is UNKNOWN."""
    depth, start = 0, 0
    for i, c in enumerate(text):
        if c == '{': start, depth = (i if depth == 0 else start), depth + 1
        elif c == '}' and depth:
            depth -= 1
            if depth: continue
            inner, parts, level, last = text[start + 1:i], [], 0, 0
            for j, d in enumerate(inner):
                level += (d == '{') - (d == '}')
                if d == ',' and level == 0: parts.append(inner[last:j]); last = j + 1
            if parts:
                out = [w for part in parts + [inner[last:]] for w in _braces(text[:start] + part + text[i + 1:], limit)]
                if len(out) > limit: raise Unresolved('brace expansion too large')
                return out
            if '..' in inner and not re.fullmatch(r'-?\d+\.\.-?\d+(?:\.\.-?\d+)?', inner): raise Unresolved('brace sequence')
    return [text]


def _pieces(word: str) -> set:
    """The word and the paths it can embed: after `=`, after a leading `@`, and each `:` or `,` separated part."""
    pieces = {word, word.split('=', 1)[-1]}
    for piece in list(pieces): pieces.update(re.split(r'[:,]', piece))
    return {p[1:] if p.startswith('@') else p for p in pieces if p not in ('', '@')}


class TurnGuard:
    """One turn's calls: a Claude Bash `cd` may persist, so cwd candidates (a union, never narrowed) and changed variables carry over."""

    def __init__(self, ctx: Context):
        self.ctx, self.cwds, self.tainted, self._aliases, self.opaque_ok, self.partial = ctx, {ctx.cwd}, set(), None, False, None
        self.unresolved, self.current = set(), None   # ids of Bash calls that did not resolve; the call being classified
        self.roots = [(_canonical(ctx.evidence), 'evidence directory'), (_canonical(ctx.rounds), 'review output directory')]
        texts = {str(p).rstrip('/') for p, _ in self.roots} | {str(ctx.evidence).rstrip('/'), str(ctx.rounds).rstrip('/')}
        self.literal = re.compile('(?:' + '|'.join(map(re.escape, sorted(texts, key=len, reverse=True))) + r')(?![^/\s"\'`;:,=)|&<>])')

    def classify(self, call: dict) -> tuple:
        self.partial, self.current = None, id(call)
        try: return (PROTECTED, reason) if (reason := self._call(call)) else (ALLOW, None)
        except Unresolved as exc:   # a protected operand found before the unresolved part, or a literal root anywhere, still counts
            if self.partial: return PROTECTED, self.partial
            if self.literal.search(json.dumps(call.get('input'), ensure_ascii=False)): return PROTECTED, 'evidence or review output path in unresolved input'
            return UNKNOWN, str(exc)

    def scope(self) -> set:
        """The cwd candidates for this call: unknown (None) too once another Bash call of the turn did not resolve, since its unparsed
        rest may have run cd ($(echo cd) ../run, eval). The call's own failure does not count against itself."""
        return self.cwds | {None} if self.unresolved - {self.current} else set(self.cwds)

    def taint(self) -> set:
        """Changed variables for this call: every variable once another Bash call of the turn did not resolve (it may export)."""
        return self.tainted | {'*'} if self.unresolved - {self.current} else self.tainted

    # --- protected checks ---------------------------------------------------------------------------------------------------------------
    def aliases(self) -> set:
        """Symlinks under the writable roots that lead into, onto or above a protected root, directly or through other such symlinks
        (walked once per turn, never followed)."""
        if self._aliases is None:
            links = {}
            for root in dict.fromkeys(_canonical(Path(r)) for r in self.ctx.writable_roots):
                for dirpath, dirs, files in os.walk(root):
                    for link in (os.path.join(dirpath, name) for name in dirs + files):
                        if os.path.islink(link): links[Path(link)] = _canonical(Path(link))
            self._aliases = {link for link, target in links.items() if any(target == r or r in target.parents or target in r.parents for r, _ in self.roots)}
            while grown := {link for link, target in links.items() if link not in self._aliases and any(target == a or target in a.parents for a in self._aliases)}:
                self._aliases |= grown
        return self._aliases

    def protected(self, path: Path, recursive: bool, follow: bool = False) -> Optional[str]:
        p = _canonical(path)
        for root, label in self.roots:
            if p == root or root in p.parents:
                if label != 'evidence directory' and re.search(r'-(?:shadow|permission-probe)-', p.name): return 'shadow output'
                return 'adversarial output' if label != 'evidence directory' and '-adversarial-' in p.name else label
            if recursive and p in root.parents: return label
        if follow and any(p == alias or p in alias.parents for alias in self.aliases()): return 'evidence directory (through a symlink)'
        return None

    def operand(self, text: str, cwds, recursive: bool = False, glob: bool = False, follow: bool = False) -> Optional[str]:
        """A literal path; a glob is cut at its first wildcard component, and what is left bounds a recursive, link-following scope."""
        parts = Path(text).parts
        if glob:
            cut = next((i for i, part in enumerate(parts) if any(c in part for c in '*?[{')), len(parts))
            if '..' in parts[cut:]: raise Unresolved(f'.. after a wildcard in {text}')
            parts, recursive, follow = parts[:cut], True, True
        path = Path(*parts) if parts else Path('.')
        for base in ([path] if path.is_absolute() else cwds):
            if base is None: raise Unresolved(f'relative path {text} with an unknown cwd')
            if reason := self.protected(base if path.is_absolute() else base / path, recursive, follow): return reason
        return None

    def word(self, text: str, cwds, recursive: bool, glob: bool = False, follow: bool = False) -> Optional[str]:
        """A command word: every path it can embed, and a literal protected root anywhere in it. A whitespace-separated part (as in
        --filter='merge FILE') is checked as a direct operand only, so code or prose text never matches as an ancestor."""
        if glob and text.count('{') > 32: raise Unresolved('too many braces in one word')   # glob: an unquoted *?[ or { in the word
        if match := self.literal.search(text): reason = self.protected(Path(match.group(0)), True) or 'evidence directory'
        else:
            pieces = {p for alternative in {text, *(_braces(text) if glob else ())} for p in _pieces(alternative)}
            spaced = {q for p in pieces for q in p.split() if q not in pieces} if re.search(r'\s', text) else set()
            checks = [(p, recursive, glob, follow) for p in sorted(pieces)] + [(q, False, False, False) for q in sorted(spaced)]
            reason = next(filter(None, (self.operand(p, cwds, r, g, f) for p, r, g, f in checks)), None)
        self.partial = self.partial or reason                                     # kept if a later part of the call is unresolved
        return reason

    # --- tool adapters -----------------------------------------------------------------------------------------------------------------
    def _call(self, call: dict) -> Optional[str]:
        tool, given = call.get('tool'), call.get('input')
        if not isinstance(given, dict): raise Unresolved(f'{tool} input is not an object')
        if tool in TEXT_TOOLS: return None
        if tool in PATH_TOOLS: return self._structured(given.get(PATH_TOOLS[tool]), recursive=False)
        if tool == 'Grep': return self._structured(given.get('path', '.'), recursive=True)   # pattern, glob and type stay text
        if tool == 'Glob':
            base, pattern = given.get('path', '.'), given.get('pattern')
            if not isinstance(base, str) or not isinstance(pattern, str): raise Unresolved('Glob pattern or path is not a string')
            return self.operand(os.path.join(base, pattern), self.scope(), glob=True)   # a pattern can move the scope (../run/...)
        if tool == 'Bash': return self.command(given.get('command'), persistent=True)
        if tool == 'command_execution': return self.command(given.get('command'), persistent=False, workdir=given.get('workdir'))
        if tool == 'code_mode':
            if (cell := code_mode_exec(given.get('code'))) is None: raise Unresolved('code-mode cell is not one literal exec_command')
            return self.command(cell['cmd'], persistent=False, workdir=cell.get('workdir'))
        if tool == 'file_change':
            changes = given.get('changes')
            if not isinstance(changes, list) or not all(isinstance(c, dict) and isinstance(c.get('path'), str) for c in changes):
                raise Unresolved('file_change without literal paths')
            return next(filter(None, (self.operand(c['path'], self.scope()) for c in changes)), None)
        raise Unresolved(f'unknown tool {tool}')

    def _structured(self, value, recursive: bool) -> Optional[str]:
        """A structured path is literal ($NAME stays text); a leading ~ is checked both as text and as the home directory. The tool's
        traversal policy is not established, so a recursive scope counts symlinks it may follow."""
        if not isinstance(value, str) or not value: raise Unresolved('missing literal path')
        if (reason := self.operand(value, self.scope(), recursive, follow=recursive)) or not re.match(r'~(?:/|$)', value): return reason
        if 'HOME' not in self.ctx.env: raise Unresolved('~ with an unknown HOME')
        return self.operand(self.ctx.env['HOME'] + value[1:], self.scope(), recursive, follow=recursive)

    def command(self, text, persistent: bool, workdir=None) -> Optional[str]:
        """A Claude Bash command keeps its cwd and variables across calls; a Codex command starts fresh in its workdir."""
        if not isinstance(text, str) or not text.strip(): raise Unresolved('missing command text')
        self.opaque_ok = text.strip() in {c.strip() for c in self.ctx.configured}   # configured bytes, never a model-declared label
        cwds = self.scope() if persistent else {self.ctx.cwd}
        if workdir is not None:
            if not isinstance(workdir, str) or not workdir: raise Unresolved('workdir is not a literal path')
            cwds = {Path(workdir)} if Path(workdir).is_absolute() else {None if c is None else c / workdir for c in cwds}
        try: reason, cwds = self._run(text, cwds, {k: v for k, v in self.ctx.env.items() if k not in ('PWD', 'OLDPWD')})
        except Unresolved:   # the unparsed rest may cd or set variables ($(echo cd) ../run): see scope()
            if persistent and not self.unresolved - {self.current}: self.unresolved.add(self.current)   # only a call no other one poisoned
            raise
        if persistent: self.cwds |= cwds - {None}
        return reason

    def _run(self, text: str, cwds: set, env: dict):
        """A command string in the given cwd candidates: (reason, cwd candidates afterwards)."""
        for _, words in _simple_commands(_tokens(text, {}, set(), expand=False)):   # pass 1: the variables this command may change
            self._taint(words)
        reason = None
        for redirects, words in _simple_commands(_tokens(text, env, self.taint())):
            known = cwds - {None}                                                 # the known candidates first: a protected operand still counts
            for target, glob in redirects: reason = reason or self.word(target, known, False, glob)
            reason, known = self._simple(words, known, env, reason)
            if None in cwds:
                if reason: return reason, cwds
                raise Unresolved('command with an unknown cwd')
            cwds = known
        return reason, cwds

    def _taint(self, words: list) -> None:
        for word, _ in words:
            if not (match := re.match(r'([A-Za-z_]\w*)\+?=', word)): break
            self.tainted.add(match.group(1))
        words = _strip_prefix(words)
        name, args = (os.path.basename(words[0][0]), [w for w, _ in words[1:]]) if words else ('', [])
        if name in SCOPED_NAMES: self.tainted.update(re.match(r'[^=]*', w).group(0) for w in args if not w.startswith('-'))
        elif name == 'set' and '--' not in args and all(w[:1] in '-+' or (re.fullmatch(r'[a-z]+', w) and args[i - 1][:1] in '-+'
                                                                          and args[i - 1].endswith('o')) for i, w in enumerate(args)): return   # set -euo pipefail
        elif name in MUTATING: self.tainted.add('*')
        elif name == 'printf' and '-v' in args[:-1]: self.tainted.add(args[args.index('-v') + 1])

    def _simple(self, words: list, cwds: set, env: dict, reason):
        for cwd in cwds:
            if found := self.protected(cwd, False): return reason or found, cwds   # the command runs in a protected directory
        while words and (match := re.fullmatch(r'([A-Za-z_]\w*)\+?=(.*)', words[0][0], re.S)):   # NAME=value: the value is an operand
            reason, words = reason or (self.word(match.group(2), cwds, True, words[0][1]) if match.group(2) else None), words[1:]
        while words and os.path.basename(words[0][0]) in ('env', *WRAPPERS):
            name, words = os.path.basename(words[0][0]), words[1:]
            if name == 'env': words, reason = self._env(words, cwds, env, reason); continue
            if name == 'command' and words and words[0][0] in ('-v', '-V'): return reason, cwds        # prints a path, runs nothing
            while words and words[0][0].startswith('-'): words = words[2:] if words[0][0] in WRAPPERS[name] else words[1:]
            if words and name == 'timeout': words = words[1:]                      # its duration
        if not words: return reason, cwds
        argv0, args = words[0][0], words[1:]
        name = os.path.basename(argv0)
        if name in KEYWORDS or argv0 in KEYWORDS: raise Unresolved(f'shell keyword {argv0}')
        if name in OPAQUE: raise Unresolved(f'{name} runs input the guard cannot see')
        if '/' in argv0 or self.literal.search(argv0): reason = reason or self.word(argv0, cwds, False, words[0][1])   # the program itself
        if name in MUTATING:                                                      # export NAME=value: the value is an operand too
            for word, glob in args:
                if name in SCOPED_NAMES and re.match(r'[A-Za-z_]\w*\+?=.', word): reason = reason or self.word(word.split('=', 1)[1], cwds, True, glob)
            return reason, cwds
        if name == 'cd': return reason, self._cd(args, cwds, env)
        if name in LAUNCHERS:
            rest = args[1:] if args and args[0][0] in LAUNCHERS[name] else args
            while rest and rest[0][0].startswith('-'):                            # the launcher's own options, before the command
                if rest[0][0] in ('-c', '--call', '-e', '--eval'): raise Unresolved(f'{name} runs inline code')
                rest = rest[1:]
            return self._simple(rest, cwds, env, reason) if rest else (reason, cwds)
        if name == 'git':
            skip = False
            for word, _ in args:                                                  # global options, before the subcommand
                if skip: skip = False; continue
                if word == '-c' or word.startswith('--config-env') or (word.startswith('-c') and not word.startswith('--')):
                    raise Unresolved('git -c can run any program')
                if word in ('-C', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix'): skip = True; continue
                if not word.startswith('-'): break
        family = re.sub(r'[\d.]+$', '', name)
        if name == 'find':                                                        # -exec CMD ... ; runs a command of its own
            while (start := next((i for i, (w, _) in enumerate(args) if w in ('-exec', '-execdir', '-ok', '-okdir')), None)) is not None:
                end = next((i for i in range(start + 1, len(args)) if args[i][0] in (';', '+')), len(args))
                reason, _ = self._simple([w for w in args[start + 1:end] if w[0] != '{}'], cwds, env, reason)   # {}: files of the scope
                args = args[:start] + args[end + 1:]
        local = set(cwds)                                                         # -C DIR and the like: operands may resolve there too
        for i, (word, _) in enumerate(args):
            key, eq, value = word.partition('=')
            value = value if eq and key in DIR_OPTS else (args[i + 1][0] if word in DIR_OPTS and i + 1 < len(args) else '')
            if value: local |= {Path(value) if Path(value).is_absolute() else c / value for c in cwds}
        short = ''.join(w[1:] for w, _ in args if w.startswith('-') and not w.startswith('--'))
        longs = {w.split('=', 1)[0] for w, _ in args if w.startswith('--')}
        recursive = (name not in NON_RECURSIVE and name not in RECURSIVE_FLAGS) or bool(set(short) & set(RECURSIVE_FLAGS.get(name, ''))) \
            or bool(longs & {'--recursive', '--dereference-recursive', '--archive'})
        follow = recursive and (bool(set(short) & set(FOLLOW_FLAGS.get(name, ''))) or bool(longs & {'--follow', '--dereference-recursive', '--copy-links'}))
        texts = self._search_texts(name, args) if name in SEARCH_VALUES or name in ('egrep', 'fgrep') else set()
        operands, options_done = [], False
        filters = FILTER_OPTS if name in FILTER_COMMANDS else set()
        texts |= {i + 1 for i, (w, _) in enumerate(args) if w in filters}           # --exclude PATTERN: a filter pattern, never a path
        for i, (word, glob) in enumerate(args):
            if i in texts: continue                                               # a search pattern, or a value that is text
            if word == '--' and not options_done: options_done = True; continue
            if word.startswith('-') and word != '-' and not options_done:
                values = [word.split('=', 1)[1]] if '=' in word and word.split('=', 1)[0] not in filters else [] if word.startswith('--') \
                    else [word[k:] for k in range(2, len(word))]
                for value in values: reason = reason or self.word(value, local, recursive, glob, follow)   # --opt=PATH, -xPATH, -xfPATH
                if self.literal.search(word): reason = reason or 'evidence directory'
                continue
            operands.append((word, glob))
        if not operands and (name in DEFAULT_SCOPE or (recursive and name in ('grep', 'egrep', 'fgrep', 'ls'))):
            operands = [('.', False)]
        if name == 'ln' and len(operands) > 1:                                    # a link target resolves from the link's directory
            link = operands[-1][0]
            local |= {c / link for c in cwds} | {(c / link).parent for c in cwds}
            recursive = follow = True
        for word, glob in operands: reason = reason or self.word(word, local, recursive, glob, follow)
        if family in INTERPRETERS or family in RARE_SHELLS: self._interpreter(name, family, args, cwds)   # after the operands: protected wins
        return reason, cwds

    def _cd(self, args: list, cwds: set, env: dict) -> set:
        if len(args) > 1 or (args and (args[0][0].startswith('-') or args[0][1])): raise Unresolved('cd with options, - or a wildcard')
        if not args and ('HOME' not in env or {'HOME', '*'} & self.taint()): raise Unresolved('cd to an unknown HOME')
        target = Path(args[0][0] if args else env['HOME'])
        if args and not target.is_absolute() and not args[0][0].startswith(('./', '../')) and args[0][0] not in ('.', '..') \
                and ('CDPATH' in env or {'CDPATH', '*'} & self.taint()): raise Unresolved('cd may follow CDPATH')
        return cwds | ({_canonical(target)} if target.is_absolute() else {_canonical(c / target) for c in cwds})

    def _env(self, words: list, cwds: set, env: dict, reason):
        while words and (words[0][0].startswith('-') or '=' in words[0][0]):
            (flag, glob), words = words[0], words[1:]
            if flag in ('-C', '--chdir') or flag.startswith('--chdir='): raise Unresolved('env changes the directory')
            if flag in ('-S', '--split-string') or flag.startswith('--split-string='):
                inner = flag.split('=', 1)[1] if '=' in flag else (words.pop(0)[0] if words else '')
                parts = _simple_commands(_tokens(inner, env, self.taint()))
                if len(parts) != 1 or parts[0][0]: raise Unresolved('env -S string with shell syntax')
                words = parts[0][1] + words
            elif flag in ('-u', '--unset'): words = words[1:]
            elif not flag.startswith('-'): reason = reason or self.word(flag.split('=', 1)[1] or '.', cwds, True, glob)
        return words, reason

    @staticmethod
    def _stdin_path(word: str, cwds) -> bool:
        """`-`, or a script path that is (or resolves to) something under /dev or /proc, however it is spelled (/dev/./stdin,
        //dev/stdin, a relative path from /dev, a planted link): the program comes from stdin or a descriptor."""
        if word == '-': return True
        if word.startswith('-'): return False
        bases = [Path(word)] if word.startswith('/') else [c / word for c in cwds if c is not None]
        try: spellings = {s for b in bases for s in (re.sub(r'^/+', '/', os.path.normpath(str(b))), os.path.realpath(b))}
        except (OSError, ValueError): return True
        return any(re.match(r'/(?:dev|proc)(?:/|$)', s) for s in spellings)

    def _interpreter(self, name: str, family: str, args: list, cwds=()) -> None:
        """Inline code or a program read from stdin is an OpaqueProgram, allowed only inside a configured command. A POSIX shell is fine
        only as `shell [plain flags | -o NAME | -O NAME] script.sh`: any other option before the script (-c, -s, --rcfile, ...), `-` or no
        script is opaque; a rare shell is opaque unless it only prints its version or help. Other interpreters take bounded forms only:
        known flags, known value options, then the script, `-m module`, `node --test`/`--run` or a deno/bun run/test/task."""
        code, valued = INTERPRETERS.get(family, ('', set()))
        def opaque(why: str) -> None:
            if not self.opaque_ok: raise OpaqueProgram(f'{name} {why}')
        prints_only = bool(args) and all(w in PRINT_ONLY.get(family, ('--version', '--help')) for w, _ in args)   # prints and exits
        if family in RARE_SHELLS:
            if not prints_only: opaque('is a shell the guard does not parse')
            return
        if family in SHELLS:                                                      # only plain flags it knows take no value, then a script
            skip = False
            for index, (word, _) in enumerate(args):
                if skip: skip = False; continue
                letters = word[1:] if word[:1] in '-+' and not word.startswith('--') else ''
                if letters[-1:] in ('o', 'O') and set(letters[:-1]) <= SHELL_FLAGS and index + 1 < len(args) \
                        and re.fullmatch(r'[a-z_]+', args[index + 1][0]): skip = True; continue   # -o pipefail, -eo pipefail, -O extglob
                if word.startswith('--') and word not in SHELL_LONG and not prints_only or \
                        letters and not set(letters) <= SHELL_FLAGS or word == '+' or self._stdin_path(word, cwds):
                    return opaque('runs inline code, reads its program from stdin or takes an option value')
                if not word.startswith(('-', '+')): return                         # the script; later words are its arguments
            if not prints_only: opaque('reads its program from stdin')
            return
        if prints_only: return
        skip, test_mode, subcommand = False, False, False                         # bounded forms: interp [known flags] (script | -m module)
        for index, (word, _) in enumerate(args):
            if skip: skip = False; continue
            option, eq, value = word.partition('=')
            attached = not word.startswith('--') and len(word) > 2 and word[:2] in valued   # -Ilib, -Wignore, -Mstrict
            if option in valued or attached:                                      # a known value option and its value
                value = word[2:] if attached else value if eq else (args[index + 1][0] if index + 1 < len(args) else '')
                scheme = re.match(r'\s*([A-Za-z][A-Za-z0-9+.-]*):', value)          # a data: (or other) URL can carry the program itself
                if option in ('--import', '--loader', '--experimental-loader') and scheme and scheme.group(1).lower() not in ('node', 'file'):
                    return opaque('runs inline code')
                if family == 'perl' and word[:2] in ('-M', '-m') and not re.fullmatch(r'-?\w+(?:::\w+)*(?:=[\w,]*)?', value):
                    return opaque('runs inline code')                             # -M'Mod;CODE' becomes `use Mod;CODE`
                if family == 'php' and word[:2] == '-d' and re.match(r'\s*auto_(?:prepend|append)_file\s*=', value, re.I):
                    return opaque('runs inline code')                             # php://stdin or a data:// program
                if (word[:2] if attached else option) in LOADS_PROGRAM.get(family, ()) and \
                        self._stdin_path(re.sub(r'^file:(?://(?:localhost)?)?', '', _unquote(value), flags=re.I), cwds):
                    return opaque('reads its program from stdin')                 # ruby -r/dev/stdin, node --import=file:///dev/stdin
                skip = not eq and not attached; continue                          # _simple checks the value as an operand
            if option in ('--eval', '--print', 'eval') or self._stdin_path(word, cwds) or \
                    (word[:1] in '-+' and not word.startswith('--') and set(word[1:]) & set(code)):
                return opaque('runs inline code or reads its program from stdin')
            if family in ('python', 'pypy') and word == '-m': return
            if word.startswith('--'):                                             # a known no-value flag, or a known option=value
                if option in INTERPRETER_LONG.get(family, ()) or (family in ('deno', 'bun') and option.startswith('--allow-')):
                    test_mode = test_mode or option in ('--test', '--run'); continue
                return opaque(f'takes an option the guard does not know ({option})')
            if word[:1] in '-+':
                if set(word[1:]) <= INTERPRETER_FLAGS.get(family, set()): continue
                return opaque(f'takes an option the guard does not know ({word})')
            if family in ('deno', 'bun') and not subcommand:                     # run SCRIPT, test, task, or a script path; nothing else
                if word == 'run': subcommand = True; continue
                if word in ('test', 'task', 'fmt', 'lint', 'check', 'info'): return  # these run no inline program
                if '/' not in word and not re.search(r'\.(?:[cm]?[jt]sx?)$', word): return opaque(f'subcommand {word} is not one the guard knows')
            return                                                                # the script; later words are its arguments
        if not test_mode: opaque('reads its program from stdin')                 # node --test/--run: test files or a package script

    def _search_texts(self, name: str, args: list) -> set:
        """Indexes of the words a grep/rg/ag/ack call reads as text: its pattern word and the values of its value options."""
        tool = 'grep' if name in ('egrep', 'fgrep') else name
        given = any(w in ('-e', '-f', '--regexp', '--file') or w.startswith(('--regexp=', '--file=')) or re.match(r'-[A-Za-z]*[ef]', w)
                    for w, _ in args)                                             # -e/-f in any form, anywhere: no pattern word
        given = given or any(w in NO_PATTERN.get(tool, ()) for w, _ in args)
        texts, skip, options_done = set(), None, False
        for i, (word, _) in enumerate(args):
            if skip is not None: skip = None; continue
            if word == '--' and not options_done: options_done = True; continue
            if not options_done and word in ('-e', '--regexp') or word in SEARCH_VALUES.get(tool, ()): texts.add(i + 1); skip = i; continue
            if not options_done and word.startswith('--regexp='): texts.add(i); continue
            if not options_done and re.match(r'-[A-Za-z]*e.', word, re.S): texts.add(i); continue   # -eneedle: the attached value is text
            if not options_done and re.fullmatch(r'-[A-Za-z]*e', word): texts.add(i + 1); skip = i; continue   # -ne PATTERN
            if word.startswith('-') and not options_done: continue
            if not given: texts.add(i); given = True                               # the first positional word is the pattern
        return texts


def _strip_prefix(words: list) -> list:
    """The words after leading NAME=value assignments and wrappers (no operand checks: pass 1 only looks for variable changes)."""
    while words and re.match(r'[A-Za-z_]\w*\+?=', words[0][0]): words = words[1:]
    while words and os.path.basename(words[0][0]) in ('env', *WRAPPERS):
        words = words[1:]
        while words and (words[0][0].startswith('-') or '=' in words[0][0]): words = words[1:]
    return words


def _dollar(text: str, i: int, env, tainted, expand: bool, quoted: bool) -> tuple:
    if text.startswith('$((', i):                                                # arithmetic: a number, unless it hides a substitution
        end = text.find('))', i + 3)
        if end < 0 or '$(' in text[i + 3:end] or '`' in text[i + 3:end]: raise Unresolved('arithmetic with a command substitution')
        return '0', end + 2
    if text.startswith('$(', i): raise Unresolved('command substitution')
    if text.startswith('${', i):
        end = text.find('}', i)
        name, i = (text[i + 2:end], end + 1) if end > 0 else ('', len(text))
        if not NAME.fullmatch(name): raise Unresolved('parameter expansion')
    elif match := NAME.match(text, i + 1): name, i = match.group(0), match.end()
    elif text[i + 1:i + 2] in ('?', '#', '$', '!', '-'): return '0', i + 2       # status, counts, pids, flags: never a path
    elif text[i + 1:i + 2] and (text[i + 1].isdigit() or text[i + 1] in '@*'):
        if '*' in tainted: raise Unresolved('positional parameter after set')
        return ('bash' if text[i + 1] == '0' else ''), i + 2                      # a tool call has no positional parameters
    elif i + 1 >= len(text) or text[i + 1] in ' \t\n' or (quoted and text[i + 1] == '"'): return '$', i + 1
    else: raise Unresolved('$-quoting')
    if not expand: return '', i
    if name in tainted or '*' in tainted or name not in env: raise Unresolved(f'variable ${name} has no trusted value')
    return env[name], i


def _tokens(text: str, env, tainted, expand: bool = True) -> list:
    """Words ('w', text, has an unquoted wildcard) and operators ('op', text); anything beyond this grammar is Unresolved."""
    tokens, word, glob, active, i, n, heredocs, brace, quoted_brace = [], [], False, False, 0, len(text), [], False, False
    def flush():
        nonlocal word, glob, active, brace, quoted_brace
        if brace and quoted_brace: raise Unresolved('quoted or escaped brace characters in a brace expression')   # _braces sees no quotes
        if active: tokens.append(('w', ''.join(word), glob))
        word, glob, active, brace, quoted_brace = [], False, False, False, False
    while i < n:
        c = text[i]
        if c in ' \t': flush(); i += 1; continue
        if c == '\n':
            flush(); tokens.append(('op', ';')); i += 1
            for delim, quoted, strip in heredocs:                                 # here-document bodies: stdin text, not operands
                while i < n:
                    end = n if (j := text.find('\n', i)) < 0 else j
                    line, i = text[i:end], end + 1
                    if (line.lstrip('\t') if strip else line) == delim: break
                    if not quoted and ('$(' in line or '`' in line): raise Unresolved('command substitution in a here-document')
            heredocs = []
            continue
        if c == '#' and not active: i = n if (j := text.find('\n', i)) < 0 else j; continue
        if (match := HEREDOC.match(text, i)) and not text.startswith('<<<', i):
            flush()
            quoted = match.group(5) is None or match.group(4) == '\\'
            heredocs.append((next(g for g in match.group(2, 3, 5) if g is not None), quoted, match.group(1) == '-'))
            i = match.end(); continue
        if text.startswith('<<', i) and not text.startswith('<<<', i): raise Unresolved('here-document without a delimiter')
        if c in '|&;<>()':
            if c in '<>' and active and not glob and ''.join(word).isdigit(): word, active = [], False   # 2>: an fd number, no word
            flush()
            op = next(o for o in OPS if text.startswith(o, i))
            tokens.append(('op', op)); i += len(op); continue
        if c == '~' and active and brace and word[-1:] in (['{'], [',']): raise Unresolved('~ inside a brace expression')   # {~,x} is HOME
        if c == '~' and (not active or word[-1:] == ['=']):                    # a word-start or assignment tilde
            end = next((j for j in range(i + 1, n) if text[j] in '/ \t\n;|&<>()'), n)
            if end > i + 1: raise Unresolved('~user expansion')
            if expand and ('HOME' not in env or {'HOME', '*'} & tainted): raise Unresolved('~ with an unknown HOME')
            word.append(env.get('HOME', '') if expand else ''); active = True; i += 1; continue
        active = True
        if c == '\\':
            if i + 1 < n and text[i + 1] != '\n': word.append(text[i + 1]); quoted_brace = quoted_brace or text[i + 1] in '{},'
            i += 2; continue
        if c == "'":
            if (j := text.find("'", i + 1)) < 0: raise Unresolved('unbalanced quote')
            word.append(text[i + 1:j]); quoted_brace = quoted_brace or any(ch in '{},' for ch in text[i + 1:j]); i = j + 1; continue
        if c == '"':
            i += 1
            while True:
                if i >= n: raise Unresolved('unbalanced quote')
                d = text[i]
                if d == '"': i += 1; break
                if d == '`': raise Unresolved('command substitution')
                if d == '\\' and i + 1 < n and text[i + 1] in '$`"\\\n':
                    if text[i + 1] != '\n': word.append(text[i + 1])
                    i += 2; continue
                if d == '$': value, i = _dollar(text, i, env, tainted, expand, True); word.append(value)
                else: word.append(d); i += 1
                quoted_brace = quoted_brace or any(ch in '{},' for ch in word[-1])
            continue
        if c == '`': raise Unresolved('command substitution')
        if c == '$':
            value, i = _dollar(text, i, env, tainted, expand, False)
            if any(ch.isspace() or ch in '*?[{' for ch in value): raise Unresolved('unquoted expansion that the shell would split or glob')
            word.append(value); quoted_brace = quoted_brace or any(ch in '},' for ch in value); continue   # an expanded } or , is no syntax
        glob, brace = glob or c in '*?[{', brace or c == '{'
        word.append(c); i += 1
    flush()
    return tokens


def _simple_commands(tokens: list) -> list:
    """[(redirect targets [(text, glob)], words [(text, glob)])] for each simple command of the lists and pipelines."""
    out, redirects, words, i = [], [], [], 0
    while i < len(tokens):
        kind, value = tokens[i][0], tokens[i][1]
        if kind == 'w': words.append((value, tokens[i][2])); i += 1; continue
        if value in SEPARATORS: out.append((redirects, words)); redirects, words = [], []; i += 1; continue
        if value not in REDIRECTS: raise Unresolved(f'shell syntax {value}')    # (, ), ;; and <<: subshells, case arms, here-documents
        if i + 1 >= len(tokens) or tokens[i + 1][0] != 'w': raise Unresolved('redirection without a target')
        if not (value in ('>&', '<&') and re.fullmatch(r'\d+|-', tokens[i + 1][1])) and value != '<<<':   # 2>&1 and <<< WORD: no file
            redirects.append((tokens[i + 1][1], tokens[i + 1][2]))
        i += 2
    out.append((redirects, words))
    return [(r, w) for r, w in out if r or w]
