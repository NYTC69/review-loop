"""v297-eg: the typed-operation evidence guard against the accepted Dot task 19 desired table (73 rows, base c211691,
`.compass/results/dot-deliveries/review-loop-dot-evidence-guard-compat-19/unpacked/matrix.py`), now as raw tool calls, plus the raw-input
adapter cases its DESIGN.md section 6 asks for. Fixture: ROOT/{workspace, run/{evidence, rounds}, home}; workspace/alias-private is a
symlink to run/evidence, workspace/alias-safe one to workspace/src. @X stands for a fixture root."""
import dataclasses
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        ctx = dataclasses.replace(self.ctx(), env={'SPACED': 'src x', 'STAR': 'src/*', 'CB': '}'})
        for command, desired in (('cat $SPACED', U), ('cat "$SPACED"', A), ('cat $STAR', U), ('cat "$STAR"', A),   # the shell splits or globs
                                 ('cat {$CB,../run/evidence}/a.txt', U)):                                       # an expanded } is no syntax
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
                ('bash -o pipefail -c "cat ../run/evidence/a.txt"', P), ('bash -c "npm test && true"', U),   # sh -c is opaque (eg-b2)
                ("sh -c 'cat $1' _ ../run/evidence/a.txt", P), ("bash -lc 'cd ../run; cat rounds/x.md'", U), ('bash -c', U),
                ("bash -c 'cd ../run; cat \"$1\"' _ evidence/a.txt", U), ("sh -c 'cat \"$@\"' _ a b", U), ("bash -c 'echo $0' name", U),
                ("find src -exec sh -c 'cd ../run && cat \"$1\"' _ evidence/a.txt \\;", U), ('cat ' + '{' * 40 + 'a,b' + '}' * 40, U),
                ("bash -c 'cd ../run && cat \"$2\"' _ {x,evidence}/a.txt", U), ("find evidence -exec sh -c 'cd ../run && cat \"$1\"' _ {} \\;", U),
                ("bash -c 'cat {$1,../run/evidence}/a.txt' _ '}'", U), ("bash -c -- '-n; cat ../run/evidence/a.txt' _", P),   # eg-b1 R2
                ("bash -eo pipefail -c 'cat ../run/evidence/a.txt'", P), ("bash -euo pipefail -c 'cat ../run/evidence/a.txt'", P),
                ("bash +eo pipefail -c 'cat ../run/evidence/a.txt'", P), ("rbash -O extglob -c 'cat ../run/evidence/a.txt'", P),
                ("mksh -T tty -c 'cat x'", U), ("bash -c 'echo hi'", U), ("bash -c 'echo $1'", U),
                ("zsh -O -c 'cat ../run/evidence/a.txt'", P), ("find evidence -exec sh -c 'cd ../run && cat \"$0\"' {} \\;", U),   # eg-b1 R3
                ("printf x | tcsh -s x", U), ("fish --command 'cat x'", U), ('tcsh --version', A), ('bash script.sh', A), ('bash -e script.sh', A),
                ('bash -o pipefail script.sh', A), ('cat x | python3', U), ('perl', U),
                ("tcsh -v -c 'cat ../run/evidence/a.txt'", P), ("printf 'cat x' | bash -v", U), ("printf x | python3 -v", U),   # eg-b2 R2
                ("printf x | zsh --emulate sh", U), ("printf x | ksh -R x", U), ('bash --version', A), ('printf x | bash /dev/stdin', U), ('printf x | sh /dev/fd/0', U), ('printf x | zsh /proc/self/fd/0', U),   # eg-b3 R2
                ('printf x | python3 /dev/stdin', U), ('gcc -E --include ../run/evidence/a.txt x.c', P), ('gcc -E --include=../run/evidence/a.txt x.c', P),
                ("grep -r --include=*.py x src", A), ("node --import 'DATA:text/javascript,x' app.js", U), ("node --import ' data:x' app.js", U),
                ('node --import=https://example.invalid/x.mjs app.js', U), ('node --import file:./x.mjs app.js', A), ('deno fmt', A),
                ('perl -w x.pl', A), ('ruby -w x.rb', A), ("bun exec 'cat ../run/evidence/a.txt'", P), ("deno repl --eval 'x'", U), ('printf x | deno repl', U), ('bun repl', U), ("bun exec 'cat x'", U),   # eg-b3 R1
                ('deno run main.ts', A), ('bun ./x.js', A), ('bun test', A),
                ("node --eval='require(\"fs\").readFileSync(\"../run/evidence/a.txt\")' x.js", U), ("node --print=1 x.js", U),
                ("node --import 'data:text/javascript,x' app.js", U), ("node --import=data:text/javascript,x app.js", U), ('node --import ./hooks.mjs app.js', A),
                ('node --inspect=9229 app.js', A), ('node --unknown-flag=1 app.js', U), ('bash -O extglob script.sh', A), ('bash -eo pipefail script.sh', A),
                ('bash -o -c script.sh', U), ('rsync -a --exclude "*.log" src/ dst/', A), ('tar -cf o.tar --exclude ../run src', A), ("rsync -a --filter='merge ../run/evidence/r' src/ dst/", P),
                ('git commit -m "use { ..Default::default() }"', A), ("printf '{...}'", A), ("curl -d '[{\"a\":1,\"b\":2},{\"a\":3}]' u", A), ("printf x | node --title --test", U), ("printf x | node -C --test", U), ("printf x | node --input-type module", U),   # eg-b2 R3
                ('ruby -C . x', U), ('osascript -s h x', U), ('deno run -', U), ('bun run -', U), ('deno run --allow-read main.ts', A),
                ('node --experimental-vm-modules node_modules/.bin/jest', A), ('node --test', A), ('node --test tests/', A),
                ('python3 -u -m pytest -q', A), ('python3 -W ignore script.py', A), ('perl -I lib x.pl', A), ('node --inspect app.js', A), ('tcsh --version -f', U), ('bash --version -x', U), ('python3 -V', A), ('node -v', A),
                ("find . -exec sh -c 'cd ../run && cat \"$@\"' _ {} +", U), ("bash -c 'cd ../run; cat \"$1\"' _ evid*/a.txt", U),
                ("yash --rcfile F -c 'cat x'", U), ("find . -exec sh -c 'cat \"$1\"' {} {} \\;", U), ("zsh -c -O 'cat x' x", U),
                ("printf x | zsh -O -s x", U), ("zsh --emulate sh -c 'cat x'", U), ('mksh -o pipefail /dev/stdin', U), ("yash --cmdline 'cat x'", U),
                ('rsync -a --exclude=/* src/ dst/', A), ('cat {~,x}/notes/a.txt', U), ('bash ../run/evidence/x.sh', P),
                ('python3 -W ignore -c x', U), ('perl -I lib -e x', U), ('node -r x -e y', U), ('php -d a=1 -r x', U),
                ("printf 'x' | python3 -", U), ('bash -', U), ('sh -s -- a', U), ('deno eval x', U), ('builtin eval x', U), ("trap 'x' EXIT", U),
                ("find src -exec sh -c 'cat ../run/evidence/a.txt' \\;", P), ('stdbuf -oL sh -c "cat ../run/evidence/a.txt"', P),
                ('uv run python -c x', U), ('busybox sh -c "cat ../run/evidence/a.txt"', P),
                ("git -c alias.x='!cat y' x", U), ('git -C . -c core.pager=x log', U), ('git --config-env=core.pager=X log', U),
                ('npx jest -c config.js', A), ('python3 script.py -c conf', A), ('node --test', A), ('find src -exec cat {} \\;', A),
                ('grep -eneedle @E/a.txt', P), ('grep --regexp=x @E/a.txt', P), ('grep @E/a.txt -e x', P), ('grep -T needle @E/a.txt', P),
                ('ag -t x @R', P), ('ag -g x @R', P), ('rg --files @R', P), ('rg --regexp=@E src', A), ('grep -ne @E src', A),
                ('rm -rf @R', P), ('mv @R /tmp/x', P), ('cp -a @R /tmp/x', P), ('diff -r @R /tmp/e', P), ('git diff --no-index @R /tmp/e', P),
                ('fd . @R', P), ('stdbuf -oL rg x @R', P), ('ls -lart @R', A), ('tr / _', A), ('echo /', A), ('cat /etc/hosts', A),
                ('export HOME=@R; cd; cat evidence/a.txt', P), ('export HOME=elsewhere; cd; cat evidence/a.txt', U),   # the value, then taint
                ('HOME=@R cd; cat evidence/a.txt', P),
                ('command export WORKSPACE=@R; cat "$WORKSPACE/evidence/a.txt"', U), ('printf -v WORKSPACE %s @R; cat "$WORKSPACE/evidence/a.txt"', U),
                ('export CDPATH=@R; cd evidence; cat a.txt', P), ('export CDPATH=elsewhere; cd evidence; cat a.txt', U), ('cat @E/a.txt; echo $(date)', P), ('exec 3<@E/a.txt', P),
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

    def test_no_spelling_of_a_protected_operand_in_any_supported_form_is_allowed(self):   # eg-b: a generated false-ALLOW corpus
        files = ['@E/a.txt', '../run/evidence/a.txt', '@W/../run/evidence/a.txt', 'src/../../run/evidence/a.txt', "'@E/a.txt'", '"@E/a.txt"',
                 "'@E'/a.txt", '"../run"/evidence/a.txt', '..\\/run/evidence/a.txt', '"$RUN/evidence/a.txt"', '$RUN/evidence/a.txt',
                 '{../run/evidence/a.txt,x}', '../run/{evidence,x}/a.txt', '{@E/a.txt,x}', '../run/evid*/a.txt', '../run/evidence/*',
                 '../run/evidence', '@E', '@O/003-reviewer.md', '../run/rounds/x.md', 'alias-private/a.txt']
        forms = ['cat {p}', 'cat < {p}', 'echo x > {p}', 'echo x >> {p}', 'cat 0<{p}', 'cat 2>{p}', 'cat &>{p}', 'cp {p} /tmp/x', 'head -n 1 {p}',
                 'grep needle {p}', 'grep -r needle {p}', 'rg needle {p}', 'grep -e needle {p}', 'grep -f {p} src', 'sort -o {p} x',
                 'sort -o{p} x', 'tool --out={p}', 'dd if={p}', 'curl -d@{p} u', 'X={p} cmd', 'env X={p} cmd', 'PYTHONPATH=src:{p} python3 a.py',
                 'time cat {p}', 'nohup cat {p}', 'nice -n 5 cat {p}', 'timeout 5 cat {p}', 'stdbuf -oL cat {p}', 'command cat {p}',
                 'npx cat {p}', 'uv run cat {p}', 'busybox cat {p}', 'find src -exec cat {p} \\;', 'true && cat {p}', 'true || cat {p}',
                 'true; cat {p}', 'true\ncat {p}', 'true & cat {p}', 'true | cat {p}', 'cat {p} | true', 'cat <<EOF\nx\nEOF\ncat {p}',
                 '/bin/cat {p}', 'python3 {p}', 'bash {p}', 'node {p}', '{p}', 'git diff --no-index {p} x', 'ls -R {p}', 'tar -cf out.tar {p}']
        found = []
        for p in files:
            for form in forms:
                command = form.replace('{p}', p)
                for tool, given in (('Bash', {'command': command}), ('command_execution', {'command': command})):
                    if self.verdict([(tool, given)], aliases=False) == A: found.append((tool, command))   # no fixture alias to mask a gap
            for form in ("bash -c 'cat {p}'", "bash -c -o pipefail 'cat {p}'", "bash -oc pipefail 'cat {p}'", "sh -c 'cat \"$1\"' _ {p}",
                         'cat {"x}",{p}}', 'cat {x\\},{p}}', 'export X={p}', 'tcsh -c "cat {p}"', "find src -exec sh -c 'cat {p}' \\;"):
                if "'" in p and "'" in form: continue                              # no single quotes inside a single-quoted script
                if self.verdict([('Bash', {'command': form.replace('{p}', p)})], aliases=False) == A: found.append(('Bash', form.replace('{p}', p)))
        for d in ('../run/evidence', '@E', '"../run"/evidence', '../run/{evidence,x}', '../run/evid*', '@O'):   # into a protected directory
            for form in ('cd {d} && cat a.txt', 'cd {d}; ls', 'ls {d}', 'git -C {d} status', 'make -C {d}', 'tar -C {d} -cf o.tar .', 'pushd {d}'):
                if self.verdict([('Bash', {'command': form.replace('{d}', d)})], aliases=False) == A: found.append(('Bash', form.replace('{d}', d)))
        for a in ('../run', '@R', '..', '"../run"', '../{run,x}', '../ru*', '/'):   # an ancestor, under a recursive or moving command
            for form in ('rg x {a}', 'grep -r x {a}', 'grep -R x {a}', 'find {a}', 'du {a}', 'cp -r {a} /tmp/x', 'rm -rf {a}', 'mv {a} /tmp/x',
                         'tar -cf o.tar {a}', 'zip -r o.zip {a}', 'rsync -a {a} /tmp/x', 'ls -R {a}', 'tree {a}', 'git diff --no-index {a} x',
                         'diff -r {a} x', 'fd x {a}', 'chmod -R 700 {a}', 'cd {a} && rg x', 'cd {a} && grep -r x .'):
                command = form.replace('{a}', a)
                if self.verdict([('Bash', {'command': command})], aliases=False) == A: found.append(('Bash', command))
            if '"' not in a and '{' not in a and '*' not in a:
                for tool, given in (('Grep', {'pattern': 'x', 'path': a}), ('Glob', {'pattern': a + '/**/*'})):
                    if self.verdict([(tool, given)], aliases=False) == A: found.append((tool, a))
        for p in [f for f in files if not re.search(r'[\'"$\\{*]', f)]:   # structured fields are literal
            for tool, given in (('Read', {'file_path': p}), ('Write', {'file_path': p}), ('MultiEdit', {'file_path': p, 'edits': []}),
                                ('Grep', {'pattern': 'x', 'path': p}), ('Glob', {'pattern': p}), ('file_change', {'changes': [{'path': p}]})):
                if self.verdict([(tool, given)]) == A: found.append((tool, p))
        self.assertEqual(found, [])

    def test_a_configured_command_admits_only_its_opaque_program_and_only_by_its_exact_bytes(self):   # option (a)
        ctx = dataclasses.replace(self.ctx(aliases=False), configured=('python3 -c pass', "bash -c 'python3 -c \"print(1)\"'"))
        def verdict(command, cwd=None):
            found = eg.first_violation([{'tool': 'Bash', 'input': {'command': command}}], ctx if cwd is None else dataclasses.replace(ctx, cwd=cwd))
            return found[0] if found else A
        for command, desired in (('python3 -c pass', A), ('  python3 -c pass ', A), ("bash -c 'python3 -c \"print(1)\"'", A),
                                 ('python3 -c pass2', U), ('python3 -c pass > ../run/evidence/x', P), ('python3 -c pass > @E/x', P),
                                 ('cat ../run/evidence/a.txt; echo $(date)', U)):   # tokenizing fails first: UNKNOWN (still a HOLD)
            with self.subTest(command=command):
                self.assertEqual(verdict(self.at(command)), desired)
        self.assertEqual(verdict('python3 -c pass', cwd=self.roots['E']), P)        # its cwd is still checked first
        self.assertEqual((eg.first_violation([{'tool': 'command_execution', 'input': {'command': 'python3 -c pass'}}], ctx) or (A,))[0], A)

    def test_an_eval_option_with_a_value_is_reported_as_inline_code(self):   # eg-b3 R1: matched by option name, not by the word
        guard = eg.TurnGuard(self.ctx(aliases=False))
        for command in ('node --eval=1 x.js', 'node --print=1 x.js', 'bun --eval=1'):
            with self.subTest(command=command):
                self.assertEqual(guard.classify({'tool': 'Bash', 'input': {'command': command}})[0], U)
                self.assertIn('inline code', guard.classify({'tool': 'Bash', 'input': {'command': command}})[1])

    def test_eg_b3_r3_attached_option_values_and_stdin_spellings(self):   # v297-eg-wire: the two eg-b3 R3 MEDIUMs
        (self.roots['W'] / 'devlink').symlink_to('/dev', target_is_directory=True)
        for command, desired in (('ruby -Ilib x.rb', A), ('perl -Ilib x.pl', A), ('perl -Mstrict x.pl', A), ('python3 -Wignore x.py', A),
                                 ('python3 -Wignore::DeprecationWarning x.py', A), ('php -dmemory_limit=1G x.php', A),
                                 ('perl -MList::Util=sum,max x.pl', A), ('perl -M-warnings x.pl', A),
                                 ("perl '-Mstrict;print 1' x.pl", U), ("perl -M 'strict;print 1' x.pl", U), ('perl -mPOSIX=() x.pl', U),
                                 ("node '--import=data:text/javascript,1' x.js", U), ('php -dauto_prepend_file=php://stdin x.php', U),
                                 ("php -d 'auto_append_file=data://text/plain,x' x.php", U), ('php -dAUTO_PREPEND_FILE=/dev/stdin x.php', U),
                                 ('printf x | ruby -r/dev/stdin x.rb', U), ('printf x | ruby -r /dev/./stdin x.rb', U),   # eg-wire R2 LOW
                                 ('printf x | node -r /dev/fd/0 x.js', U), ('printf x | node --require=/dev/stdin x.js', U),
                                 ('printf x | node --import=file:///dev/stdin x.js', U), ('printf x | node --import file:///%64ev/stdin x.js', U),
                                 ('printf x | bun --preload /dev/stdin x.ts', U), ('printf x | php -c /dev/stdin x.php', U),
                                 ('node -r ./setup.js x.js', A), ('node --import=file:///opt/loader.mjs x.js', A), ('ruby -rjson x.rb', A),
                                 ('php -c php.ini x.php', A),
                                 ('ruby -I../run/evidence x.rb', P),          # the attached value is still an operand
                                 ('printf x | bash /dev/./stdin', U), ('printf x | bash //dev/stdin', U), ('printf x | bash /dev/fd/../fd/0', U),
                                 ('cd /dev && bash stdin', U), ('printf x | python3 /dev/./stdin', U), ('printf x | bash devlink/stdin', U),
                                 ('printf x | python3 ' + '../' * 40 + 'dev/stdin', U), ('bash scripts/check.sh', A), ('tcsh x.csh', U)):
            with self.subTest(command=command):
                self.assertEqual(self.verdict([('Bash', {'command': command})], aliases=False), desired)
        with mock.patch('os.path.realpath', lambda path: '/home/u/input.txt'):   # Linux: /dev/stdin -> /proc/self/fd/0 -> a regular file
            self.assertTrue(eg.TurnGuard._stdin_path('//dev/stdin', ()))
            self.assertTrue(eg.TurnGuard._stdin_path('../' * 40 + 'dev/stdin', (self.roots['W'],)))
        with mock.patch('os.path.realpath', side_effect=OSError('loop')):         # unresolvable: treated as stdin
            self.assertTrue(eg.TurnGuard._stdin_path('scripts/check.sh', (self.roots['W'],)))
        self.assertFalse(eg.TurnGuard._stdin_path('scripts/check.sh', (self.roots['W'],)))

    def test_an_unresolved_bash_call_drops_the_cwd_and_variables(self):   # v297-eg-wire: $(echo cd) ../run may run cd, eval may export
        for first, second in (('cd ../run && ls $(echo .)', 'cat evidence/a.txt'), ('export HOME=../run; $(true)', 'cat ~/evidence/a.txt'),
                              ('eval "cd ../run"', 'cat evidence/a.txt'), ("python3 -c 'pass'; cd ../run", 'cat evidence/a.txt'),
                              ('ls $(echo .)', 'cat "$WORKSPACE/src/a.py"')):
            with self.subTest(first=first):
                calls = [{'tool': 'Bash', 'input': {'command': c}} for c in (first, second)]
                self.assertEqual([v for v, _ in eg.turn_verdicts(calls, self.ctx())], [U, U])
                self.assertEqual([v for v, _ in eg.turn_verdicts(calls[1:], self.ctx())], [A])   # alone, it resolves
        calls = [{'tool': 'Bash', 'input': {'command': c}} for c in ('export RUN=/elsewhere; $(true)', 'cat "$RUN/evidence/a.txt"')]
        self.assertEqual([v for v, _ in eg.turn_verdicts(calls, self.ctx())], [U, U])            # $RUN may have changed
        protected = 'cd ../run && cat evidence/a.txt && python3 -c pass2'                        # PROTECTED, with an unresolved tail
        calls = [{'tool': 'Bash', 'input': {'command': c}} for c in ('cat src/a.py', protected)]
        self.assertEqual([v for v, _ in eg.turn_verdicts(calls, self.ctx())], [U, P])            # the benign call does not poison it back
        self.assertEqual([v for v, _ in eg.turn_verdicts(calls[::-1], self.ctx())], [P, U])
        calls = [{'tool': 'Bash', 'input': {'command': 'ls $(echo .)'}}, {'tool': 'Read', 'input': {'file_path': self.at('@W/src/a.py')}}]
        self.assertEqual([v for v, _ in eg.turn_verdicts(calls, self.ctx())], [U, A])   # an absolute path needs no cwd
        calls = [{'tool': 'command_execution', 'input': {'command': c}} for c in ('ls $(echo .)', 'cat src/a.py')]
        self.assertEqual([v for v, _ in eg.turn_verdicts(calls, self.ctx())], [U, A])   # a Codex command starts fresh

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
