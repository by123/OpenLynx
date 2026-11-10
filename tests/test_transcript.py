import _hermetic  # noqa: F401  (must precede lynx_memory imports)
import unittest

from lynx_memory.transcript import find_last_turn, find_last_turn_codex


class ClaudeTranscriptTest(unittest.TestCase):
    def test_claude_turn_keeps_edit_content_with_assistant_text(self):
        msgs = [
            {
                "type": "user",
                "uuid": "user-1",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "fix the greeting"}],
                },
            },
            {
                "type": "assistant",
                "uuid": "assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I will update the component."},
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {
                                "file_path": "/tmp/App.tsx",
                                "old_string": "return <h1>Hello</h1>;",
                                "new_string": "return <h1>Hello, Ada</h1>;",
                            },
                        },
                    ],
                },
            },
        ]

        user_text, user_uuid, asst_text, asst_uuid, had_prose = find_last_turn(msgs)

        self.assertEqual(user_text, "fix the greeting")
        self.assertEqual(user_uuid, "user-1")
        self.assertEqual(asst_uuid, "assistant-1")
        self.assertTrue(had_prose)
        self.assertIn("I will update the component.", asst_text)
        self.assertIn("**Tool: Edit**", asst_text)
        self.assertIn("File: `/tmp/App.tsx`", asst_text)
        self.assertIn("```diff", asst_text)
        self.assertIn("-return <h1>Hello</h1>;", asst_text)
        self.assertIn("+return <h1>Hello, Ada</h1>;", asst_text)

    def test_claude_turn_keeps_write_and_multiedit_content(self):
        msgs = [
            {
                "type": "user",
                "uuid": "user-2",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "add config files"}],
                },
            },
            {
                "type": "assistant",
                "uuid": "assistant-2",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": "/tmp/settings.json",
                                "content": '{\n  "theme": "dark"\n}',
                            },
                        },
                        {
                            "type": "tool_use",
                            "name": "MultiEdit",
                            "input": {
                                "file_path": "/tmp/main.py",
                                "edits": [
                                    {
                                        "old_string": "name = 'old'",
                                        "new_string": "name = 'new'",
                                    }
                                ],
                            },
                        },
                    ],
                },
            },
        ]

        user_text, user_uuid, asst_text, asst_uuid, had_prose = find_last_turn(msgs)

        self.assertEqual(user_text, "add config files")
        self.assertEqual(user_uuid, "user-2")
        self.assertEqual(asst_uuid, "assistant-2")
        self.assertFalse(had_prose)
        self.assertIn("**Tool: Write**", asst_text)
        self.assertIn('+  "theme": "dark"', asst_text)
        self.assertIn("**Tool: MultiEdit**", asst_text)
        self.assertIn("-name = 'old'", asst_text)
        self.assertIn("+name = 'new'", asst_text)


class CodexTranscriptTest(unittest.TestCase):
    def test_codex_turn_keeps_apply_patch_content_with_assistant_text(self):
        msgs = [
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "fix the cell"}],
                },
            },
            {"type": "event_msg", "payload": {"turn_id": "turn-1"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I will patch it."}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "input": (
                        "*** Begin Patch\n"
                        "*** Update File: VideoContentCollectionViewCell.swift\n"
                        "@@\n"
                        "-    guard let videoPlayerView, videoPlayerView.superview != containerView else { return }\n"
                        "+    guard let videoPlayerView else { return }\n"
                        "*** End Patch"
                    ),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Patched."}],
                },
            },
        ]

        user_text, user_uuid, asst_text, asst_uuid, had_prose = find_last_turn_codex(msgs)

        self.assertEqual(user_text, "fix the cell")
        self.assertEqual(user_uuid, "turn-1:user")
        self.assertEqual(asst_uuid, "turn-1:assistant")
        self.assertTrue(had_prose)
        self.assertIn("I will patch it.", asst_text)
        self.assertIn("**Tool: apply_patch**", asst_text)
        self.assertIn("```diff", asst_text)
        self.assertIn("*** Update File: VideoContentCollectionViewCell.swift", asst_text)
        self.assertIn("+    guard let videoPlayerView else { return }", asst_text)
        self.assertTrue(asst_text.rstrip().endswith("```"))
        self.assertIn("Patched.", asst_text)


if __name__ == "__main__":
    unittest.main()
