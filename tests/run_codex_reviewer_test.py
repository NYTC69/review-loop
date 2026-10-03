"""Real subprocess transport tests; fake model process, no network calls."""
import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/run_codex_reviewer.py"
sys.path.insert(0, str(ROOT / "scripts"))
import run_codex_reviewer as wrapper
from reviewer_usage import UsageAccumulator

FAKE = r'''
import json, os, signal, sys, time
from pathlib import Path
args = sys.argv[1:]
prompt = sys.stdin.read()
Path(os.environ['CAPTURE']).write_text(json.dumps({'argv':args,'cwd':os.getcwd(),'prompt':prompt,'pid':os.getpid()}))
mode = os.environ.get('MODE','ok')
if mode == 'hang':
    time.sleep(30)
if mode == 'eof_hang':
    os.close(1)
    time.sleep(30)
if mode == 'rate':
    sys.stderr.write('usage limit reached\n')
    sys.exit(1)
if mode == 'failure':
    sys.exit(3)
if mode != 'no_tool':
    print(json.dumps({'type':'item.started','item':{'id':'tool-1','type':'command_execution'}}),flush=True)
if mode != 'missing':
    Path(args[args.index('-o')+1]).write_text('### VERDICT: APPROVE\n### Strengths\nInspected.\n')
if mode == 'malformed':
    print('not-json',flush=True)
else:
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':40,'output_tokens':10}}),flush=True)
if mode in ('result_hang', 'cancel'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    sys.stderr.write('CANCEL_DIAGNOSTIC\n')
    sys.stderr.flush()
    Path(os.environ['READY']).touch()
    time.sleep(30)
if mode == 'error_event':
    print(json.dumps({'type':'turn.failed','error':{'message':'failed'},'usage':{'input_tokens':429}}),flush=True)
if mode == 'stream_rate':
    print(json.dumps({'type':'error','message':"You've hit your usage limit. Try again later."}),flush=True)
    print(json.dumps({'type':'turn.failed','error':{'message':'Request failed with status code 429'}}),flush=True)
if mode == 'late_malformed':
    print('not-json',flush=True)
if mode == 'invalid_utf8':
    os.write(1,b'\xff\n')
if mode == 'late_error_unterminated':
    sys.stdout.write(json.dumps({'type':'turn.failed','error':{'message':'failed'}}))
if mode == 'write_failure':
    os.close(1)
    os.write(1,b'late bytes')
if mode == 'child':
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(1)
        Path(os.environ['CHILD_CANARY']).write_text('survived')
        os._exit(0)
'''


def run_fake(tmp_path, mode="ok", timeout=5, *, model="test-model", cancel_signal=None,
             prompt_text="PRIVATE original review request", missing_prompt=False,
             blocked_ledger=False):
    fixture = tmp_path / "workspace"
    fixture.mkdir(exist_ok=True)
    tmp = fixture / ".review-loop/tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    if not missing_prompt:
        (tmp / "s-reviewer-prompt.txt").write_text(prompt_text)
    (tmp / "s-reviewer-result.txt").write_text("STALE")
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    codex = binary / "codex"
    codex.write_text(f"#!{sys.executable}\n"+FAKE)
    codex.chmod(0o755)
    env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ.get("PATH", ""),
               MODE=mode, CAPTURE=str(tmp_path/"capture.json"),
               CHILD_CANARY=str(tmp_path/"child-canary"), READY=str(tmp_path/"ready"))
    argv = [sys.executable, str(WRAPPER), "--session-id", "s", "--timeout-seconds", str(timeout),
            "--heartbeat-seconds", "0.05"]
    if model is not None:
        argv += ["--model", model]
    if blocked_ledger:
        blocked = tmp_path / "blocked-ledger"
        blocked.write_text("not a directory")
        argv += ["--ledger-dir", str(blocked)]
    process = subprocess.Popen(argv, cwd=fixture, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if cancel_signal is not None:
            deadline = time.monotonic() + 3
            while not (tmp_path/"ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (tmp_path/"ready").exists()
            process.send_signal(cancel_signal)
        stdout, stderr = process.communicate(timeout=10)
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
            capture = tmp_path/"capture.json"
            if capture.is_file():
                try:
                    os.killpg(json.loads(capture.read_text())["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        process.wait(timeout=3)
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert "PRIVATE" not in result.stdout
    assert "STALE" not in result.stdout
    return result, summary, tmp, fixture


def test_isolated_readonly_invocation_and_usage(tmp_path):
    result, summary, tmp, fixture = run_fake(tmp_path)
    assert result.returncode == 0, result.stderr
    captured = json.loads((tmp_path/"capture.json").read_text())
    assert Path(captured["cwd"]) != fixture
    assert not Path(captured["cwd"]).exists()
    argv = captured["argv"]
    assert argv[argv.index("-s")+1] == "read-only"
    assert "--ignore-rules" in argv and "--ignore-user-config" in argv
    assert "features.hooks=false" in argv and "features.plugins=false" in argv
    assert str(fixture) in captured["prompt"]
    assert Path(summary["prompt_file"]).read_text() == captured["prompt"]
    assert "APPROVE" in Path(summary["result_file"]).read_text()
    ledger = json.loads(Path(summary["usage_file"]).read_text())
    assert ledger["status"] == "ok"
    assert summary["usage"] == ledger["usage"]
    assert summary["cleanup_ok"]
    assert summary["invocation_id"]


@pytest.mark.parametrize("mode,status,code", [
    ("hang", "timeout", 4), ("eof_hang", "timeout", 4),
    ("result_hang", "timeout", 4), ("missing", "missing_result", 3),
    ("malformed", "json_parsing", 2), ("failure", "command_execution", 1),
    ("rate", "rate_limited", 6), ("error_event", "command_execution", 1),
    ("stream_rate", "rate_limited", 6), ("late_malformed", "json_parsing", 2),
    ("invalid_utf8", "json_parsing", 2), ("late_error_unterminated", "command_execution", 1),
    ("write_failure", "command_execution", 1)])
def test_failures_never_accept_a_stale_or_partial_result(tmp_path, mode, status, code):
    result, summary, tmp, _ = run_fake(tmp_path, mode, timeout=1)
    assert result.returncode == code, result.stderr
    assert summary["status"] == status
    assert summary["result_file"] is None
    assert not (tmp/"s-reviewer-result.txt").exists()
    assert Path(summary["usage_file"]).is_file()
    ledger = json.loads(Path(summary["usage_file"]).read_text())
    assert summary["usage"] == ledger["usage"]
    assert ledger["usage"]["accounting_status"] != "complete"


def test_exited_leader_does_not_leave_background_child(tmp_path):
    result, summary, _, _ = run_fake(tmp_path, "child")
    assert result.returncode == 0, result.stderr
    time.sleep(1.2)
    assert not (tmp_path/"child-canary").exists()


@pytest.mark.parametrize("timeout", ["nan", "inf", "0", "-1"])
@pytest.mark.parametrize("option", ["--timeout-seconds", "--heartbeat-seconds"])
def test_invalid_deadlines_rejected_before_dispatch(timeout, option):
    result = subprocess.run([sys.executable, str(WRAPPER), "--session-id", "s",
                             option, timeout], capture_output=True)
    assert result.returncode == 2


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_cancellation_keeps_raw_evidence_and_partial_usage(tmp_path, sig):
    result, summary, tmp, _ = run_fake(tmp_path, "cancel", cancel_signal=sig)
    assert result.returncode == 5, result.stderr
    assert summary["status"] == "cancelled"
    assert summary["cancel_signal"] == sig
    assert summary["cleanup_ok"]
    assert summary["child_exit"] == -signal.SIGKILL
    assert not (tmp/"s-reviewer-result.txt").exists()
    assert Path(summary["stderr_file"]).read_text() == "CANCEL_DIAGNOSTIC\n"
    assert "turn.completed" in Path(summary["stream_file"]).read_text()
    ledger = json.loads(Path(summary["usage_file"]).read_text())
    assert ledger["status"] == "cancelled"
    assert summary["usage"] == ledger["usage"]
    assert ledger["usage"]["accounting_status"] == "partial"


def test_heartbeat_reports_phase_and_native_event_count_after_result(tmp_path):
    result, summary, _, _ = run_fake(tmp_path, "result_hang", timeout=0.5)
    assert result.returncode == 4
    beats = [json.loads(line) for line in result.stdout.splitlines()[:-1]]
    assert beats
    assert all(beat["phase"] == "reviewer" for beat in beats)
    assert any(beat["events"] == 2 and beat["last_event_type"] == "turn.completed" for beat in beats)
    assert summary["events"] == 2


def test_no_model_override_uses_native_default(tmp_path):
    result, summary, _, _ = run_fake(tmp_path, model=None)
    assert result.returncode == 0, result.stderr
    captured = json.loads((tmp_path/"capture.json").read_text())
    assert "-m" not in captured["argv"]
    assert json.loads(Path(summary["usage_file"]).read_text())["requested_model"] is None


def test_zero_tool_use_never_publishes_a_report(tmp_path):
    result, summary, _, _ = run_fake(tmp_path, "no_tool")
    assert result.returncode == 8
    assert summary["status"] == "tool_uses_zero"
    assert summary["tool_uses"] == 0
    assert summary["result_file"] is None


def test_tool_use_count_uses_unique_native_items():
    events = wrapper._Events(UsageAccumulator("codex"))
    events.consume(json.dumps({"type": "item.started", "item": {"id": "i1", "type": "command_execution"}}).encode())
    events.consume(json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "command_execution"}}).encode())
    events.consume(json.dumps({"type": "turn.completed", "usage": {}}).encode())
    assert len(events.tool_use_ids) == 1


def test_non_tool_errors_do_not_satisfy_tool_use_guard_and_unknown_items_fail_closed():
    events = wrapper._Events(UsageAccumulator("codex"))
    events.consume(json.dumps({"type": "item.completed", "item": {"id": "e1", "type": "error"}}).encode())
    assert not events.tool_use_ids
    assert not events.tool_use_count_unknown
    events.consume(json.dumps({"type": "item.completed", "item": {"id": "u1", "type": "future_kind"}}).encode())
    assert events.tool_use_count_unknown


def test_retry_preserves_exact_prompt_and_ledger_hashes(tmp_path):
    first, before, _, _ = run_fake(tmp_path, prompt_text="PRIVATE FIRST REQUEST")
    assert first.returncode == 0
    ledger_bytes = Path(before["usage_file"]).read_bytes()
    prompt_bytes = Path(before["prompt_file"]).read_bytes()
    second, after, tmp, _ = run_fake(tmp_path, "failure", prompt_text="PRIVATE SECOND REQUEST")
    assert second.returncode == 1
    assert before["invocation_id"] != after["invocation_id"]
    assert Path(before["prompt_file"]).read_bytes() == prompt_bytes
    assert Path(before["usage_file"]).read_bytes() == ledger_bytes
    assert "PRIVATE FIRST REQUEST" in prompt_bytes.decode()
    assert "PRIVATE SECOND REQUEST" in Path(after["prompt_file"]).read_text()
    ledger = json.loads(ledger_bytes)
    artifact = next(item for item in ledger["raw_artifacts"] if item["path"] == before["prompt_file"])
    assert artifact["sha256"] == hashlib.sha256(prompt_bytes).hexdigest()
    assert len(list((tmp/"usage").glob("*.json"))) == 2


def test_missing_prompt_is_recorded_without_accepting_stale_result(tmp_path):
    result, summary, tmp, _ = run_fake(tmp_path, missing_prompt=True)
    assert result.returncode == 1
    assert summary["status"] == "command_execution"
    assert summary["phase"] == "preparing"
    assert not (tmp/"s-reviewer-result.txt").exists()
    assert Path(summary["usage_file"]).is_file()


def test_usage_ledger_write_failure_rejects_result_and_preserves_evidence(tmp_path):
    result, summary, tmp, _ = run_fake(tmp_path, blocked_ledger=True)
    assert result.returncode == 1
    assert summary["failure_reason"] == "usage_record_failed"
    assert summary["usage_file"] is None
    assert summary["result_file"] is None
    assert not (tmp/"s-reviewer-result.txt").exists()
    assert "turn.completed" in Path(summary["stream_file"]).read_text()
    assert Path(summary["prompt_file"]).is_file()


def test_group_signal_failure_does_not_skip_bounded_reap(monkeypatch):
    process = Mock(pid=123)
    process.poll.return_value = -9
    monkeypatch.setattr(wrapper.os, "killpg", Mock(side_effect=PermissionError))
    assert not wrapper.stop_group(process)
    assert process.send_signal.call_count == 2
    assert [call.kwargs["timeout"] for call in process.wait.call_args_list] == [0.3, 2.0]


def test_oversized_stream_line_is_discarded_once_with_bounded_buffer(monkeypatch):
    events = wrapper._Events(wrapper.UsageAccumulator(runtime="codex"))
    monkeypatch.setattr(wrapper, "MAX_EVENT_BYTES", 32)
    events.feed(b"x" * 80)
    assert not events.pending
    events.feed(b"x" * 80 + b"\n")
    events.feed(b'{"type":"turn.completed"}\n')
    assert events.invalid_lines == 1
    assert events.completed
