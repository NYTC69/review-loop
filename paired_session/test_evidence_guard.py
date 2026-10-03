"""v297-eg: the typed-operation evidence guard against the accepted Dot task 19 desired table (73 rows, base c211691,
`.compass/results/dot-deliveries/review-loop-dot-evidence-guard-compat-19/unpacked/matrix.py`), now as raw tool calls, plus the raw-input
adapter cases its DESIGN.md section 6 asks for. Fixture: ROOT/{workspace, run/{evidence, rounds}, home}; workspace/alias-private is a
symlink to run/evidence, workspace/alias-safe one to workspace/src. @X stands for a fixture root."""
import dataclasses
import tempfile
import unittest
from pathlib import Path

from paired_session import evidence_guard as eg

A, P, U = eg.ALLOW, eg.PROTECTED, eg.UNKNOWN
# (id, role, tool, input, cwd root or None, desired): the matrix rows, in the matrix order. The role is recorded; the decision does not
# depend on it (every role is denied the evidence and rounds roots, the author its own schema included).
ROWS = [row for r in ('author', 'reviewer', 'shadow', 'gate') for row in (
    (f'schema_{r}', r, 'Read', {'file_path': '@E/003-exec-author.schema.json'}, 'W', P), (f'rounds_{r}', r, 'Read', {'file_path': 'rounds/003-reviewer.md'}, 'R', P))] + [
    ('read_source', 'reviewer', 'Read', {'file_path': 'src/a.py'}, 'W', A),
    ('workspace_evidence', 'reviewer', 'Read', {'file_path': 'tests/evidence/a.txt'}, 'W', A),
    ('workspace_rounds', 'reviewer', 'Read', {'file_path': 'tests/rounds/a.txt'}, 'W', A),
    ('sibling_evidence', 'reviewer', 'Read', {'file_path': '@R/evidence-copy/a.txt'}, 'W', A),
    ('sibling_rounds', 'reviewer', 'Read', {'file_path': '@R/rounds-old/a.txt'}, 'W', A),
    ('quoted_space', 'reviewer', 'Read', {'file_path': 'tests/evidence/a b.txt'}, 'W', A),
    ('plain_pattern', 'reviewer', 'Grep', {'pattern': 'evidence/|rounds/', 'path': '@W/src'}, 'W', A),
    ('absolute_pattern', 'reviewer', 'Grep', {'pattern': '@E/003-exec-author.schema.json', 'path': '@W/src'}, 'W', A),
    ('glob_same_name', 'reviewer', 'Glob', {'pattern': '**/evidence/*.txt', 'path': '@W/tests'}, 'W', A),
    ('glob_rounds', 'reviewer', 'Glob', {'pattern': '**/rounds/*.txt', 'path': '@W/tests'}, 'W', A),
    ('search_protected', 'reviewer', 'Grep', {'pattern': 'needle', 'path': '@E'}, 'W', P),
    ('search_ancestor', 'reviewer', 'Grep', {'pattern': 'needle', 'path': '@ROOT'}, 'W', P),
    ('glob_escape', 'reviewer', 'Glob', {'pattern': '../run/evidence/*.json', 'path': '@W'}, 'W', P),
    ('alias_bad', 'reviewer', 'Read', {'file_path': '@W/alias-private/a.txt'}, 'W', P),
    ('alias_safe', 'reviewer', 'Read', {'file_path': '@W/alias-safe/a.py'}, 'W', A),
    ('dotdot', 'reviewer', 'Read', {'file_path': '@W/../run/evidence/a.txt'}, 'W', P),
    ('missing_leaf', 'reviewer', 'Read', {'file_path': '@W/alias-private/not-created.txt'}, 'W', P),
    ('read_unknown_cwd', 'reviewer', 'Read', {'file_path': 'evidence/a.txt'}, None, U),
    ('read_absolute_unknown_cwd', 'reviewer', 'Read', {'file_path': '@W/src/a.py'}, None, A),
    ('read_literal_variable', 'reviewer', 'Read', {'file_path': '$ROOT/evidence/a.txt'}, 'W', A),
    ('unknown_tool', 'reviewer', 'Mystery', {'value': 'src/a.py'}, 'W', U)] + [
    (i, 'reviewer', 'Bash', {'command': c}, 'W', A) for i, c in (
        ('test_bash', '/bin/bash @W/scripts/check.sh'), ('test_unittest', 'python3 -m unittest discover -s tests'),
        ('test_pytest', 'pytest -q tests/evidence/'), ('test_npm', 'npm test'), ('test_make', 'make test'),
        ('test_xcode', 'xcodebuild test -project App.xcodeproj -scheme App'), ('test_swift', 'swift test'),
        ('review_node', 'node verify-real-data.mjs'), ('review_python', 'python3 audit.py'), ('git_status', 'git status --short'),
        ('git_diff', 'git diff -- tests/evidence/a.txt'), ('git_show', 'git show HEAD:tests/evidence/a.txt'), ('git_log', 'git log -1 --oneline'),
        ('search_rg', "rg -n 'evidence/|rounds/' src"), ('search_grep', "grep -R 'evidence/' tests"),
        ('bash_quoted', '/bin/bash "@W/scripts/check space.sh"'), ('env_assignment', 'NODE_ENV=test python3 -m unittest'),
        ('env_wrapper', 'env NODE_ENV=test python3 -m unittest'), ('env_split', "env -S 'NODE_ENV=test python3 -m unittest'"),
        ('test_glob', 'pytest -q tests/evidence/test_*.py'))] + [
    ('known_variable', 'reviewer', 'Bash', {'command': 'cat "$WORKSPACE/tests/evidence/a.txt"'}, 'W', A),
    ('known_home', 'reviewer', 'Bash', {'command': 'cat ~/notes/a.txt'}, 'W', A),
    ('variable_protected', 'reviewer', 'Bash', {'command': 'cat "$RUN/evidence/a.txt"'}, 'W', P),
    ('unknown_variable', 'reviewer', 'Bash', {'command': 'cat "$UNBOUND/evidence/a.txt"'}, 'W', U),
    ('unknown_home', 'reviewer', 'Bash', {'command': 'cat ~/notes/a.txt'}, 'W', U),              # the context has no HOME (below)
    ('unknown_cwd_test', 'reviewer', 'Bash', {'command': 'npm test'}, None, U),
    ('literal_cat', 'reviewer', 'Bash', {'command': 'cat tests/evidence/a.txt'}, 'W', A),
    ('protected_command', 'reviewer', 'Bash', {'command': 'cat @E/003-exec-author.schema.json'}, 'W', P),
    ('registered_protected_script', 'reviewer', 'Bash', {'command': '/bin/bash @E/script.sh'}, 'W', P),
    ('registered_protected_cwd', 'reviewer', 'Bash', {'command': 'npm test'}, 'E', P),
    ('cd_then_read', 'reviewer', 'Bash', {'command': 'cd @R && cat rounds/003-reviewer.md'}, 'W', P),
    ('dynamic_code', 'reviewer', 'Bash', {'command': 'python3 -c "import os; open(os.environ[\'P\']).read()"'}, 'W', U),
    ('substitution', 'reviewer', 'Bash', {'command': 'cat "$(resolve_path)"'}, 'W', U),
    ('redirect_protected', 'reviewer', 'Bash', {'command': 'pytest -q tests > @E/output'}, 'W', P),
    ('pipeline_search', 'reviewer', 'Bash', {'command': "git ls-files | grep 'evidence/'"}, 'W', A),
    ('glob_protected_alias', 'reviewer', 'Glob', {'pattern': '**/*', 'path': '@W/alias-private'}, 'W', P),
    ('codex_command', 'reviewer', 'command_execution', {'command': 'python3 -m unittest -v'}, 'W', A),
    ('code_mode_literal', 'reviewer', 'code_mode', {'code': 'const r = await tools.exec_command({"cmd":"npm test","workdir":"@W"}); text(r);'}, 'W', A),
    ('code_mode_dynamic', 'reviewer', 'code_mode', {'code': 'const r = await tools.exec_command({cmd: choose()}); text(r);'}, 'W', U),
    ('git_not_allowlisted', 'reviewer', 'Bash', {'command': 'git status --short'}, 'W', A),       # ALLOW is no permission to run git
    ('git_protected_c', 'reviewer', 'Bash', {'command': 'git -C @E status'}, 'W', P),
    ('xcode_protected_output', 'reviewer', 'Bash', {'command': 'xcodebuild test -derivedDataPath @E'}, 'W', P),
    ('swift_protected_package', 'reviewer', 'Bash', {'command': 'swift test --package-path @E'}, 'W', P),
    ('recursive_workspace_alias', 'reviewer', 'Grep', {'pattern': 'needle', 'path': '@W'}, 'W', P)]


class EvidenceGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.roots = {'ROOT': root, 'W': root / 'workspace', 'R': root / 'run', 'HOME': root / 'home'}
        self.roots.update(E=self.roots['R'] / 'evidence', O=self.roots['R'] / 'rounds')
        w = self.roots['W']
        for path in (*self.roots.values(), w / 'src', w / 'tests/evidence', w / 'tests/rounds', w / 'scripts', self.roots['HOME'] / 'notes'):
            path.mkdir(parents=True, exist_ok=True)
        (w / 'src/a.py').write_text('# fake\n'); (self.roots['E'] / 'a.txt').write_text('synthetic protected\n')
        (w / 'alias-private').symlink_to(self.roots['E'], target_is_directory=True)
        (w / 'alias-safe').symlink_to(w / 'src', target_is_directory=True)

    def at(self, value):
        if isinstance(value, dict): return {k: self.at(v) for k, v in value.items()}
        if isinstance(value, list): return [self.at(v) for v in value]
        if not isinstance(value, str): return value
        for key in sorted(self.roots, key=len, reverse=True): value = value.replace('@' + key, str(self.roots[key]))
        return value

    def ctx(self, cwd='W', home=True, aliases=True):
        env = {'WORKSPACE': str(self.roots['W']), 'RUN': str(self.roots['R']), **({'HOME': str(self.roots['HOME'])} if home else {})}
        return eg.Context(evidence=self.roots['E'], rounds=self.roots['O'], cwd=self.roots[cwd] if cwd else None, env=env,
                          writable_roots=(self.roots['W'],) if aliases else ())

    def verdict(self, calls, **ctx):
        found = eg.first_violation([{'tool': t, 'input': self.at(i)} for t, i in calls], self.ctx(**ctx))
        return found[0] if found else A

    def test_the_73_row_desired_table(self):
        self.assertEqual((len(ROWS), len({r[0] for r in ROWS})), (73, 73))
        self.assertEqual({d: sum(r[5] == d for r in ROWS) for d in (A, P, U)}, {A: 40, P: 25, U: 8})   # DESIGN.md section 2
        for rid, role, tool, given, cwd, desired in ROWS:   # matrix.py has no per-row env: unknown_home is its only row without HOME
            with self.subTest(row=rid, role=role):
                self.assertEqual(self.verdict([(tool, given)], cwd=cwd, home=rid != 'unknown_home'), desired)

    def test_a_cd_persists_across_claude_bash_calls_in_either_stream_order_but_not_across_codex_commands(self):
        cd, read = ('Bash', {'command': 'cd @R'}), ('Read', {'file_path': 'rounds/003-reviewer.md'})
        self.assertEqual((self.verdict([cd, read]), self.verdict([read, cd])), (P, P))
        self.assertEqual(self.verdict([('Bash', {'command': 'cd @R'}), ('Bash', {'command': 'cat rounds/x.md'})]), P)
        self.assertEqual(self.verdict([('command_execution', {'command': 'cd @R'}), ('command_execution', {'command': 'cat rounds/x.md'})]), A)
        self.assertEqual(self.verdict([('command_execution', {'command': 'cat rounds/x.md', 'workdir': '@R'})]), P)   # its own workdir
        self.assertEqual(self.verdict([('Bash', {'command': 'cd "$UNBOUND"'}), read]), U)

    def test_a_variable_the_turn_assigns_has_no_trusted_value(self):
        self.assertEqual(self.verdict([('Bash', {'command': 'WORKSPACE=elsewhere'}), ('Bash', {'command': 'cat "$WORKSPACE/evidence/a.txt"'})]), U)
        self.assertEqual(self.verdict([('Bash', {'command': 'WORKSPACE=@R'})], aliases=False), P)     # a protected ancestor as a value
        self.assertEqual(self.verdict([('Bash', {'command': 'export WORKSPACE=@R; cat "$WORKSPACE/x"'})]), U)
        self.assertEqual(self.verdict([('Bash', {'command': 'export OTHER=1'}), ('Bash', {'command': 'cat "$WORKSPACE/src/a.py"'})]), A)
        self.assertEqual(self.verdict([('Bash', {'command': 'read X'}), ('Bash', {'command': 'cat "$WORKSPACE/src/a.py"'})]), U)
        self.assertEqual(self.verdict([('Bash', {'command': "cat '$RUN/evidence/a.txt'"})]), A)       # single quotes: literal text
        ctx = dataclasses.replace(self.ctx(), env={'SPACED': 'src x', 'STAR': 'src/*'})
        for command, desired in (('cat $SPACED', U), ('cat "$SPACED"', A), ('cat $STAR', U), ('cat "$STAR"', A)):   # the shell splits or globs
            with self.subTest(command=command):
                self.assertEqual((eg.first_violation([{'tool': 'Bash', 'input': {'command': command}}], ctx) or (A,))[0], desired)

    def test_a_command_without_operands_in_a_protected_cwd_is_protected(self):
        self.assertEqual((self.verdict([('Bash', {'command': 'ls'})], cwd='E'), self.verdict([('Bash', {'command': 'ls'})])), (P, A))
        self.assertEqual(self.verdict([('Bash', {'command': 'cd @E; ls'})]), P)

    def test_command_forms_beyond_the_table(self):
        for command, desired in (
                ('git -C @R diff --no-index evidence/a.txt /dev/null', P),   # -C moves where relative operands resolve
                ('PYTHONPATH=@E python3 a.py', P), ('env PYTHONPATH=@E python3 a.py', P),
                ('cat src/a.py 2>&1 > /dev/null', A), ('cat src/a.py >> @O/x.md', P),
                ('rg needle', A), ('rg -L needle', P), ('grep needle src/a.py', A), ('grep -rn needle', A), ('grep -Rn needle', P),   # the cwd
                ('find . -name "*.py"', A), ('find -L . -name x', P), ('ls', A), ('ls -R ..', P), ('cp -r @R /tmp/x', P),   # holds alias-private:
                # only a link-following search reaches it
                ('cat src/*/../../../run/evidence/a.txt', U), ('cat alias-*/a.txt', P),
                ('python3 -m pytest -c conf.ini tests', A), ('bash', U), ('node --version', A), ('node -e "1"', U), ('perl -ne print x', U),
                ('cat <<EOF\nx\nEOF', A),('(cat src/a.py)', U), ('for f in *; do cat $f; done', U), ('xargs cat < list', U),
                ('eval "cat x"', U), ('cat `which x`', U), ('cat "$1"', A), ('set -- @E/a.txt; cat $1', P), ('set -- x; cat $1', U), ('cat ~other/x', U), ('cat "unbalanced', U),
                ('timeout -s KILL 5 cat @E/a.txt', P), ('nice -n 5 cat src/a.py', A), ('cd && cat notes/a.txt', A),
                ("rg -e '@E' src", A), ("grep -f @E/a.txt src", P), ('cat src/a.py # @E/a.txt', A)):
            with self.subTest(command=command):
                self.assertEqual(self.verdict([('Bash', {'command': command})]), desired)

    def test_eg_a_r1_findings_without_the_fixture_alias(self):   # each would be ALLOW (or UNKNOWN) without its fix
        for command, desired in (
                ('@E/run.sh', P), ('../run/evidence/tool --help', P),                                        # argv0
                ('cat $"../run/evidence/a.txt"', U), ('cat $"@E/a.txt"', P),                                 # $"..": the literal still counts
                ('dd if=@E/a.txt', P), ('dd if=../run/evidence/a.txt', P), ('curl --data-binary @@E/a.txt x', P), ('curl -d@@E/a.txt x', P),
                ('grep -f@E/a.txt src', P), ('sort -o@E/x y', P), ('tar -xf@E/x.tar', P), ('tar -xf../run/evidence/x.tar', P),
                ('docker run -v @E:/d img cat /d/a.txt', P), ('PYTHONPATH=src:@E python3 a.py', P),
                ('bash -o pipefail -c x', U), ('python3 -W ignore -c x', U), ('perl -I lib -e x', U), ('node -r x -e y', U), ('php -d a=1 -r x', U),
                ("printf 'x' | python3 -", U), ('bash -', U), ('sh -s -- a', U), ('deno eval x', U), ('builtin eval x', U), ("trap 'x' EXIT", U),
                ("find src -exec sh -c 'x' \\;", U), ('stdbuf -oL sh -c x', U), ('uv run python -c x', U), ('busybox sh -c x', U),
                ("git -c alias.x='!cat y' x", U), ('git -C . -c core.pager=x log', U), ('git --config-env=core.pager=X log', U),
                ('npx jest -c config.js', A), ('python3 script.py -c conf', A), ('node --test', A), ('find src -exec cat {} \\;', A),
                ('grep -eneedle @E/a.txt', P), ('grep --regexp=x @E/a.txt', P), ('grep @E/a.txt -e x', P), ('grep -T needle @E/a.txt', P),
                ('ag -t x @R', P), ('ag -g x @R', P), ('rg --files @R', P), ('rg --regexp=@E src', A), ('grep -ne @E src', A),
                ('rm -rf @R', P), ('mv @R /tmp/x', P), ('cp -a @R /tmp/x', P), ('diff -r @R /tmp/e', P), ('git diff --no-index @R /tmp/e', P),
                ('fd . @R', P), ('stdbuf -oL rg x @R', P), ('ls -lart @R', A), ('tr / _', A), ('echo /', A), ('cat /etc/hosts', A),
                ('export HOME=@R; cd; cat evidence/a.txt', U), ('HOME=@R cd; cat evidence/a.txt', U),
                ('command export WORKSPACE=@R; cat "$WORKSPACE/evidence/a.txt"', U), ('printf -v WORKSPACE %s @R; cat "$WORKSPACE/evidence/a.txt"', U),
                ('export CDPATH=@R; cd evidence; cat a.txt', U), ('cat @E/a.txt; echo $(date)', P), ('exec 3<@E/a.txt', P),
                ('ln -s ../../run/evidence a/l', P), ("alias x='cat a'", U), ('cat -- -/../../run/evidence/a.txt', P),
                ('set -euo pipefail; cat "$WORKSPACE/src/a.py"', A), ('npm test; echo "exit=$?"', A), ('npm test &>> log', A),
                ('python3 -m unittest 2>&1; echo "EXIT_CODE: $?"', A), ('command -v node', A),
                ('git -C src diff --no-index ../../run/evidence/a.txt /dev/null', P), ('git diff --no-index ../../run/evidence/a.txt x', A),   # -C src
                ('time npm test', A), ('time -p cat @E/a.txt', P), ("cat > notes.txt <<'EOF'\nsome $(text) and `x` here\nEOF", A),   # R2: ordinary
                ('cat > n.txt <<EOF\n$(date)\nEOF', U), ('cat <<EOF > @E/x\nhi\nEOF', P), ('cat <<-EOF\n\tx\n\tEOF\ncat @E/a.txt', P),
                ('cat <<\\EOF\n$(x)\nEOF', A), ('cat <<', U), ('grep x <<< "some text"', A), ('echo $((1+2)) > out.txt', A),
                ('echo $(( $(cat x) ))', U), ('shopt -s globstar; cat "$WORKSPACE/src/a.py"', A)):
            with self.subTest(command=command):
                self.assertEqual(self.verdict([('Bash', {'command': command})], aliases=False), desired)
        calls = [('Bash', {'command': 'cat rounds/x.md'}), ('Bash', {'command': 'cd run'}), ('Bash', {'command': 'cd ..'})]   # cd .., cd run,
        self.assertEqual(self.verdict(calls, aliases=False), P)                                                              # cat: listed backwards
        self.assertIsNone(eg.code_mode_exec('const r = await tools.exec_command({"cmd":"npm test","shell":"/x"}); text(r);'))

    def test_an_alias_through_another_alias_is_followed(self):
        hop = self.roots['ROOT'] / 'hop'
        hop.mkdir()
        (hop / 'c').symlink_to(self.roots['E'], target_is_directory=True)
        (self.roots['W'] / 'b').symlink_to(hop, target_is_directory=True)
        (self.roots['W'] / 'alias-private').unlink()
        ctx = dataclasses.replace(self.ctx(), writable_roots=(self.roots['W'], hop))
        self.assertEqual(eg.first_violation([{'tool': 'Grep', 'input': {'pattern': 'x', 'path': str(self.roots['W'])}}], ctx)[0], P)
        self.assertIsNone(eg.first_violation([{'tool': 'Grep', 'input': {'pattern': 'x', 'path': str(self.roots['W'] / 'src')}}], ctx))

    def test_structured_tools(self):
        for tool, given, desired, ctx in (
                ('Read', {'file_path': '~/x'}, A, {}), ('Read', {'file_path': '~/x'}, U, {'home': False}),
                ('Write', {'file_path': '@E/003-exec-author.schema.json'}, P, {}), ('Edit', {'file_path': 'src/a.py'}, A, {}),
                ('Grep', {'pattern': 'x'}, P, {}), ('Grep', {'pattern': 'x'}, A, {'aliases': False}),   # no path: the cwd, alias included
                ('Glob', {'pattern': '@O/*.md'}, P, {}), ('Glob', {'pattern': 'src/**/../x'}, U, {}),
                ('StructuredOutput', {'status': 'APPROVE'}, A, {}), ('Read', {}, U, {}), ('Read', 'x', U, {}),
                ('file_change', {'changes': [{'path': '@W/src/a.py'}, {'path': '@E/x'}]}, P, {}), ('file_change', {'changes': 'x'}, U, {})):
            with self.subTest(tool=tool, given=given, ctx=ctx):
                self.assertEqual(self.verdict([(tool, given)], **ctx), desired)
        home = dict(self.roots, HOME=self.roots['R'])
        self.roots = home                                                         # ~ is the home directory too, not only text
        self.assertEqual(self.verdict([('Read', {'file_path': '~/evidence/a.txt'})]), P)

    def test_reasons_name_the_protected_output(self):
        guard = eg.TurnGuard(self.ctx())
        for path, reason in (('@E/a.txt', 'evidence directory'), ('@O/003-reviewer.md', 'review output directory'),
                             ('@O/07-shadow-approve.md', 'shadow output'), ('@O/11-adversarial-approve.md', 'adversarial output'),
                             ('@O/02-permission-probe-x.md', 'shadow output')):
            with self.subTest(path=path):
                self.assertEqual(guard.classify({'tool': 'Read', 'input': {'file_path': self.at(path)}}), (P, reason))
        self.assertEqual(eg.first_violation([{'tool': 'Mystery', 'input': {}}, {'tool': 'Read', 'input': {'file_path': self.at('@E/a.txt')}}],
                                            self.ctx()), (P, 'evidence directory'))   # a protected access outranks an unknown one

    def test_the_code_mode_adapter_accepts_only_the_literal_cell(self):
        for code, cmd in (('const r = await tools.exec_command({"cmd":"npm test"}); text(JSON.stringify(r));', 'npm test'),
                          ('const out = await tools.exec_command({"cmd":"ls","workdir":"/x"}); text(out.output);', 'ls'),
                          ('const r = await tools.exec_command({"cmd":"ls"}); text(r); other();', None),
                          ('const r = await tools.exec_command({cmd: "ls"}); text(r);', None), (None, None)):
            with self.subTest(code=code):
                self.assertEqual((eg.code_mode_exec(code) or {}).get('cmd'), cmd)


if __name__ == '__main__':
    unittest.main()
