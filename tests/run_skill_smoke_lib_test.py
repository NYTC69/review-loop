import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_skill_smoke_lib import finalize_stream_capture_artifact, select_primary_session_path


def write_session(root: Path, session_id: str, entry_point: str) -> Path:
    session_path = root / ".review-loop" / "sessions" / f"{session_id}.md"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        "## Session Metadata\n"
        f"- entry_point: {entry_point}\n",
        encoding="utf-8",
    )
    return session_path.resolve()


class SelectPrimarySessionPathTest(unittest.TestCase):
    def test_prefers_review_loop_session_over_later_stdout_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            umbrella = write_session(root, "11111111-1111-1111-1111-111111111111", "review-loop")
            downstream = write_session(root, "22222222-2222-2222-2222-222222222222", "execute-from-plan")
            stdout = "\n".join(
                [
                    f"umbrella: .review-loop/sessions/{umbrella.name}",
                    f"downstream: .review-loop/sessions/{downstream.name}",
                ]
            )

            selected = select_primary_session_path(
                stdout=stdout,
                root=root,
                before_sessions=set(),
                after_sessions={umbrella, downstream},
            )

            self.assertEqual(selected, umbrella)

    def test_fallback_prefers_review_loop_session_among_new_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            downstream = write_session(root, "33333333-3333-3333-3333-333333333333", "execute-from-plan")
            umbrella = write_session(root, "44444444-4444-4444-4444-444444444444", "review-loop")

            selected = select_primary_session_path(
                stdout="",
                root=root,
                before_sessions=set(),
                after_sessions={downstream, umbrella},
            )

            self.assertEqual(selected, umbrella)


class FinalizeStreamCaptureArtifactTest(unittest.TestCase):
    def test_writes_tool_events_from_partial_stream_without_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            artifact_path = root / "tool-use-events.json"
            text_path = root / "stdout.txt"
            artifact_path.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Read",
                                    "input": {"file_path": "docs/protocol/execution.md"},
                                }
                            ]
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            finalized = finalize_stream_capture_artifact(artifact_path, text_path)

            self.assertTrue(finalized)
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["events"], [{"tool": "Read", "target": "docs/protocol/execution.md"}])
            self.assertIn("no 'type=result' event observed", payload["schema_errors"][0])
            self.assertEqual(text_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
