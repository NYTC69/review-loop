"""V321-SUB: a Claude author with subagents forwards subagent text, so text-only subagent requests reach usage.json."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from paired_session import test_real_coordinator as harness

rc = harness.rc
FLAG = '--forward-subagent-text'
MAIN_MODEL, SUB_MODEL = 'claude-opus-5-5', 'claude-haiku-5-5'


def usage(input_tokens, output_tokens):
    return {'input_tokens': input_tokens, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0,
            'output_tokens': output_tokens}


def assistant(message_id, blocks, output, parent=None, model=MAIN_MODEL):
    return {'type': 'assistant', 'parent_tool_use_id': parent,
            'message': {'id': message_id, 'model': model, 'content': blocks, 'usage': usage(100, output)}}


def user(content, parent=None):
    return {'type': 'user', 'parent_tool_use_id': parent, 'message': {'role': 'user', 'content': content}}


def claude_rows(session='claude-session'):
    """Rows shaped as Claude Code 2.1.303 emits them with --forward-subagent-text (checked on the real CLI)."""
    start = lambda output: {'type': 'stream_event', 'parent_tool_use_id': None, 'event': {
        'type': 'message_start', 'message': {'model': MAIN_MODEL, 'usage': usage(35, output)}}}
    delta = lambda output: {'type': 'stream_event', 'parent_tool_use_id': None, 'event': {
        'type': 'message_delta', 'usage': {'output_tokens': output}}}
    agent = {'type': 'tool_use', 'id': 'toolu_agent', 'name': 'Agent', 'input': {'prompt': 'check'}}
    bash = {'type': 'tool_use', 'id': 'toolu_bash', 'name': 'Bash', 'input': {'command': 'git status'}}
    return [
        {'type': 'system', 'subtype': 'init', 'model': MAIN_MODEL, 'session_id': session},
        start(1), assistant('msg_main_1', [agent], 5),
        # A forwarded prompt: string content and a text block list; neither is a tool call.
        user('Check the tree and report.', 'toolu_agent'),
        user([{'type': 'text', 'text': 'Check the tree and report.'}], 'toolu_agent'),
        # One subagent request with text and a tool_use: the forwarded text row has the same message id.
        assistant('msg_sub_1', [{'type': 'text', 'text': 'Looking.'}], 4, 'toolu_agent', SUB_MODEL),
        assistant('msg_sub_1', [bash], 11, 'toolu_agent', SUB_MODEL),
        user([{'type': 'tool_result', 'tool_use_id': 'toolu_bash', 'content': 'clean', 'is_error': False}], 'toolu_agent'),
        # A text-only subagent request (its final report): before V321-SUB the CLI emitted no row for it.
        assistant('msg_sub_2', [{'type': 'thinking', 'thinking': 'done'}], 6, 'toolu_agent', SUB_MODEL),
        assistant('msg_sub_2', [{'type': 'text', 'text': '{"status": "WRONG", "body": "subagent text"}'}], 8,
                  'toolu_agent', SUB_MODEL),
        user([{'type': 'tool_result', 'tool_use_id': 'toolu_agent', 'content': 'tree is clean'}]),
        delta(7), start(2),
        assistant('msg_main_2', [{'type': 'text', 'text': 'plan'}], 3), delta(3),
        {'type': 'result', 'is_error': False, 'session_id': session,
         'structured_output': {'status': 'READY', 'body': 'plan'}, 'result': '',
         'subagent_stats': {'spawned': 1, 'started_in_background': 0, 'completed': 1}},
    ]


class ForwardSubagentTextTests(unittest.TestCase):
    # Use the existing offline CLI fixture; do not inherit its hundreds of test methods.
    locals().update({name: getattr(harness.RealCoordinatorTests, name) for name in (
        'setUp', 'tearDown', 'coordinator', 'fake_codex_cli', 'fake_claude_cli',
        '_guarded_test_popen', '_assert_no_real_provider_cli')})

    def all_claude(self, *extra):
        self.run_dir = self.root / ('run' + str(len(list(self.root.iterdir()))))
        return self.coordinator('--author-vendor', 'claude', '--reviewer-vendor', 'claude',
                                '--gate-vendor', 'claude', *extra)

    def test_flag_is_passed_only_to_the_claude_author_with_subagents(self):
        schema = self.root / 'schema.json'
        rc.atomic_json(schema, rc.review_schema())
        on = self.all_claude()
        self.assertEqual(on.args.author_subagents, 'on')
        self.assertIn(FLAG, on.command('author', schema, False))
        self.assertIn(FLAG, on.author_flags()['claude_author_surface'])   # a new permission probe runs the same argv
        for role in ('reviewer', 'shadow', 'gate', 'probe', 'gate-probe'):
            with self.subTest(role=role):
                self.assertNotIn(FLAG, on._claude_command(role, schema, True))
        off = self.all_claude('--author-subagents', 'off')
        self.assertNotIn(FLAG, off.command('author', schema, False))

    def test_text_only_subagent_request_is_counted_once_per_message(self):
        path = self.root / 'stream.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in claude_rows()))
        answer, session, requests, count, commands, calls = self.all_claude()._collect('claude', path)
        self.assertEqual(answer, {'status': 'READY', 'body': 'plan'})      # never the subagent's text
        self.assertEqual(session, 'claude-session')
        self.assertEqual(count, 4)
        subs = [row for row in requests if row['source'] == 'subagent-message']
        self.assertEqual([(row['message_id'], row['output']) for row in subs], [('msg_sub_1', 11), ('msg_sub_2', 8)])
        self.assertEqual([row['output'] for row in requests if row['source'] == 'message_start'], [7, 3])
        # Tool calls keep their pre-V321 meaning: forwarded text and prompts add none.
        self.assertEqual([call['tool'] for call in calls], ['Bash', 'Agent'])
        self.assertEqual([row['command'] for row in commands], ['git status'])
        self.assertEqual(rc.reported_provider_model('claude', path), (MAIN_MODEL, 'session-init'))

        incremental, receipt, offset, buffered, active = self.root / 'incremental.jsonl', {'vendor': 'claude'}, 0, b'', None
        for row in claude_rows():
            with incremental.open('a') as stream:
                stream.write(json.dumps(row) + '\n')
            offset, buffered, active, _ = rc.Coordinator._drain_usage_stream(incremental, offset, buffered, active, receipt)
        self.assertEqual(receipt['model_requests'], 4)
        self.assertEqual([(row['source'], row['output']) for row in receipt['usage_requests']],
                         [('stream_event', 7), ('subagent-message', 11), ('subagent-message', 8), ('stream_event', 3)])

    def test_author_turn_writes_the_text_only_request_to_usage_json(self):
        co = self.all_claude('--reviewer-vendor', 'codex', '--gate-vendor', 'codex')
        rows = claude_rows(co.state['sessions']['author'])

        class Child:
            pid, returncode = 123456, 0
            def communicate(self, *args, **kwargs): return None
            def wait(self): return 0

        real_popen = rc.subprocess.Popen
        def spawn(*args, **kwargs):
            if not hasattr(kwargs.get('stdout'), 'write'):
                return real_popen(*args, **kwargs)
            self.assertIn(FLAG, args[0])
            kwargs['stdout'].write(''.join(json.dumps(row) + '\n' for row in rows).encode())
            kwargs['stdout'].flush()
            return Child()

        with patch.object(rc.subprocess, 'Popen', spawn):
            result = co._invoke_once('author', 'PLAN', 'Role: persistent. Phase: PLAN.', rc.author_schema())
        self.assertEqual(result['answer']['body'], 'plan')
        receipt = co.state['turns'][-1]
        self.assertEqual(receipt['model_requests'], 4)
        self.assertEqual([row.get('message_id') for row in receipt['usage_requests'] if row['source'] == 'subagent-message'],
                         ['msg_sub_1', 'msg_sub_2'])
        co.write_usage()
        overall = json.loads((co.run_dir / 'usage.json').read_text())['overall']
        self.assertEqual((overall['input_tokens'], overall['output_tokens']), (2 * 35 + 2 * 100, 7 + 3 + 11 + 8))

    def test_a_run_frozen_before_the_flag_still_verifies_on_resume(self):
        co = self.all_claude()
        real = rc.Coordinator._claude_command
        def before_v321(self, *args, **kwargs):
            return [value for value in real(self, *args, **kwargs) if value != FLAG]
        with patch.object(rc.Coordinator, '_claude_command', before_v321):
            co._freeze_role_dispatch()
            frozen = co.state['role_dispatch_manifest']
        self.assertEqual(frozen, co._role_dispatch_manifest())   # the manifest holds no argv
        co.state['lifecycle'] = rc.worktree_lifecycle.initial(co.state['item_uuid'], co.state['base_commit'])   # ADR-11 TMP
        co._verify_frozen_role_dispatch()


if __name__ == '__main__':
    unittest.main()
