from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_workflow.models import ClaudeCodeConfig, CodexConfig, GenericConfig
from agent_workflow.providers import ProviderError, _parse_claude_code_output, run_provider


class CodexProviderTests(unittest.TestCase):
    def _fake_codex_path(self) -> Path:
        return Path(__file__).with_name("fake_codex.py")

    def test_run_codex_writes_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            schema_path = root / "schema.json"
            output_path = root / "output.json"
            schema_path.write_text(json.dumps({
                "type": "object",
                "required": ["success", "next"],
                "properties": {"success": {"type": "boolean"}, "next": {"type": "string"}},
                "additionalProperties": False,
            }), encoding="utf-8")

            config = CodexConfig(bin=str(self._fake_codex_path()))
            result = run_provider(
                config=config,
                prompt="done-step",
                workdir=str(root),
                schema_path=schema_path,
                output_path=output_path,
            )

            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.payload["success"])
            self.assertTrue(output_path.exists())


class ClaudeCodeProviderTests(unittest.TestCase):
    def _fake_claude_path(self) -> Path:
        return Path(__file__).with_name("fake_claude.py")

    def test_run_claude_code_parses_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            schema_path = root / "schema.json"
            output_path = root / "output.json"
            schema_path.write_text(json.dumps({
                "type": "object",
                "required": ["success", "next"],
                "properties": {"success": {"type": "boolean"}, "next": {"type": "string"}},
                "additionalProperties": False,
            }), encoding="utf-8")

            config = ClaudeCodeConfig(bin=str(self._fake_claude_path()), model="sonnet")
            result = run_provider(
                config=config,
                prompt="done-step",
                workdir=str(root),
                schema_path=schema_path,
                output_path=output_path,
            )

            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.payload["success"])
            self.assertEqual(result.payload["source"], "fake-claude")
            self.assertTrue(output_path.exists())

    def test_run_claude_code_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            schema_path = root / "schema.json"
            output_path = root / "output.json"
            schema_path.write_text("{}", encoding="utf-8")

            config = ClaudeCodeConfig(bin=str(self._fake_claude_path()))
            result = run_provider(
                config=config,
                prompt="branch=fix",
                workdir=str(root),
                schema_path=schema_path,
                output_path=output_path,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.payload["next"], "fix")


class ParseClaudeCodeOutputTests(unittest.TestCase):
    def test_parses_string_result(self) -> None:
        inner = {"success": True, "next": "__end__"}
        envelope = {"type": "result", "result": json.dumps(inner)}
        result = _parse_claude_code_output(json.dumps(envelope))
        self.assertEqual(result, inner)

    def test_parses_dict_result(self) -> None:
        inner = {"success": True, "next": "__end__"}
        envelope = {"type": "result", "result": inner}
        result = _parse_claude_code_output(json.dumps(envelope))
        self.assertEqual(result, inner)

    def test_passthrough_plain_dict(self) -> None:
        plain = {"success": True, "next": "__end__"}
        result = _parse_claude_code_output(json.dumps(plain))
        self.assertEqual(result, plain)


class GenericProviderTests(unittest.TestCase):
    def test_generic_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            # Create a script that writes to the output file
            script = root / "tool.sh"
            script.write_text(
                '#!/bin/sh\necho \'{"success": true, "next": "__end__"}\' > "$3"\n',
                encoding="utf-8",
            )
            script.chmod(0o755)

            schema_path = root / "schema.json"
            output_path = root / "output.json"
            (root / "prompt.txt").write_text("test prompt", encoding="utf-8")
            schema_path.write_text("{}", encoding="utf-8")

            config = GenericConfig(
                command_template=f"{script} {{prompt_file}} {{schema_file}} {{output_file}}",
                output_mode="file",
            )
            result = run_provider(
                config=config,
                prompt="test prompt",
                workdir=str(root),
                schema_path=schema_path,
                output_path=output_path,
            )

            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.payload["success"])

    def test_generic_stdout_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            script = root / "tool.sh"
            script.write_text(
                '#!/bin/sh\necho \'{"success": true, "next": "__end__"}\'\n',
                encoding="utf-8",
            )
            script.chmod(0o755)

            schema_path = root / "schema.json"
            output_path = root / "output.json"
            (root / "prompt.txt").write_text("test prompt", encoding="utf-8")
            schema_path.write_text("{}", encoding="utf-8")

            config = GenericConfig(
                command_template=f"{script} {{prompt_file}} {{schema_file}} {{output_file}}",
                output_mode="stdout",
            )
            result = run_provider(
                config=config,
                prompt="test prompt",
                workdir=str(root),
                schema_path=schema_path,
                output_path=output_path,
            )

            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.payload["success"])
            # stdout mode should also write the file for consistency
            self.assertTrue(output_path.exists())

    def test_generic_empty_template_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            schema_path = root / "schema.json"
            output_path = root / "output.json"
            (root / "prompt.txt").write_text("test", encoding="utf-8")
            schema_path.write_text("{}", encoding="utf-8")

            config = GenericConfig(command_template="", output_mode="file")
            with self.assertRaises(ProviderError):
                run_provider(
                    config=config,
                    prompt="test",
                    workdir=str(root),
                    schema_path=schema_path,
                    output_path=output_path,
                )


if __name__ == "__main__":
    unittest.main()
