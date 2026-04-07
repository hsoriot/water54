from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from agent_workflow.engine import run_workflow
from agent_workflow.models import ClaudeCodeConfig, CodexConfig, GenericConfig
from agent_workflow.scaffold import (
    ScaffoldError,
    _parse_provider_config,
    compile_blueprint,
    load_blueprint,
    scaffold_blueprint,
)


_SAMPLE_BLUEPRINT = {
    "name": "sample-scaffold",
    "template_type": "multi-agent",
    "workdir": "/abs/path/to/your/project",
    "shared": {
        "files": [
            {
                "id": "handoff",
                "path": "shared/handoff.md",
                "purpose": "request-and-handoff",
            }
        ]
    },
    "agents": [
        {
            "id": "planner",
            "role": "high-level planning and routing",
            "uses_memory": True,
            "uses_shared": ["handoff"],
            "next_options": ["executor", "finish"],
        },
        {
            "id": "executor",
            "role": "implementation and validation",
            "uses_memory": True,
            "uses_shared": ["handoff"],
            "next_options": ["planner", "finish"],
        },
    ],
    "workflow": {
        "start_at": "planner",
        "max_steps": 12,
        "run_root": ".runs",
    },
}


class ScaffoldTests(unittest.TestCase):
    def test_scaffold_generates_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            blueprint_file = root / "blueprint.yaml"
            blueprint_file.write_text(
                yaml.safe_dump(
                    {
                        "name": "demo",
                        "template_type": "multi-agent",
                        "workdir": "/tmp/project",
                        "shared": {
                            "files": [
                                {
                                    "id": "handoff",
                                    "path": "shared/handoff.md",
                                    "purpose": "request-and-handoff",
                                }
                            ]
                        },
                        "agents": [
                            {
                                "id": "planner",
                                "uses_memory": True,
                                "uses_shared": ["handoff"],
                                "next_options": ["executor", "finish"],
                            },
                            {
                                "id": "executor",
                                "uses_memory": True,
                                "uses_shared": ["handoff"],
                                "next_options": ["planner", "finish"],
                            },
                        ],
                        "workflow": {
                            "start_at": "planner",
                            "max_steps": 10,
                            "run_root": ".runs",
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            out_dir = root / "generated"
            scaffold_blueprint(load_blueprint(str(blueprint_file)), str(out_dir))

            self.assertTrue((out_dir / "workflow.yaml").exists())
            self.assertTrue((out_dir / "prompts" / "planner.md").exists())
            self.assertTrue((out_dir / "schemas" / "planner-output.json").exists())
            self.assertTrue((out_dir / "memory" / "planner.md").exists())
            self.assertTrue((out_dir / "shared" / "handoff.md").exists())
            self.assertFalse((out_dir / "README.md").exists())
            self.assertFalse((out_dir / "examples").exists())

            workflow = yaml.safe_load((out_dir / "workflow.yaml").read_text(encoding="utf-8"))
            self.assertEqual(workflow["workflow"]["start_at"], "planner")
            self.assertEqual(workflow["agents"][0]["uses_shared"], ["handoff"])

            compiled = compile_blueprint(str(out_dir / "workflow.yaml"))
            self.assertEqual(compiled.start_at, "planner")
            self.assertEqual(compiled.vars["shared_handoff"], "shared/handoff.md")
            self.assertEqual(compiled.agents_by_id["planner"].branches["executor"], "executor")

    def test_runner_can_execute_blueprint_yaml_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            generated = root / "generated"

            blueprint_file = root / "blueprint.yaml"
            blueprint_file.write_text(
                yaml.safe_dump(_SAMPLE_BLUEPRINT, sort_keys=False),
                encoding="utf-8",
            )
            scaffold_blueprint(load_blueprint(str(blueprint_file)), str(generated))

            fake_codex = Path(__file__).with_name("fake_codex.py")
            workflow_path = generated / "workflow.yaml"
            workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
            workflow["workdir"] = str(generated)
            workflow["agents"][0]["prompt_path"] = "prompts/planner.md"
            workflow["agents"][1]["prompt_path"] = "prompts/executor.md"
            workflow["workflow"]["run_root"] = "runs"
            workflow_path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")

            compiled = compile_blueprint(str(workflow_path))
            compiled.provider.bin = str(fake_codex)
            result = run_workflow(compiled)

            self.assertEqual(result.status, "succeeded")
            self.assertGreaterEqual(len(result.step_results), 1)

    def test_scaffold_schema_has_enum(self) -> None:
        """#16: scaffold-generated schemas should include next enum values."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            blueprint_file = root / "blueprint.yaml"
            blueprint_file.write_text(
                yaml.safe_dump(
                    {
                        "name": "enum-test",
                        "template_type": "multi-agent",
                        "workdir": "/tmp/project",
                        "agents": [
                            {
                                "id": "alpha",
                                "uses_memory": False,
                                "next_options": ["beta", "finish"],
                            },
                            {
                                "id": "beta",
                                "uses_memory": False,
                                "next_options": ["finish"],
                            },
                        ],
                        "workflow": {"start_at": "alpha", "run_root": ".runs"},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            out_dir = root / "generated"
            scaffold_blueprint(load_blueprint(str(blueprint_file)), str(out_dir))

            schema = json.loads(
                (out_dir / "schemas" / "alpha-output.json").read_text(encoding="utf-8")
            )
            enum_values = schema["properties"]["next"].get("enum")
            self.assertIsNotNone(enum_values, "scaffold schema should include enum for next")
            self.assertIn("beta", enum_values)
            self.assertIn("__end__", enum_values)


class SchemaLoadingTests(unittest.TestCase):
    def test_compile_loads_custom_schema_from_disk(self) -> None:
        """#20: compile_blueprint should load user schema files when they exist."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            schemas_dir = root / "schemas"
            schemas_dir.mkdir()

            custom_schema = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["success", "next", "summary"],
                "properties": {
                    "success": {"type": "boolean"},
                    "next": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "additionalProperties": False,
            }
            (schemas_dir / "worker-output.json").write_text(
                json.dumps(custom_schema, indent=2), encoding="utf-8"
            )

            blueprint_file = root / "workflow.yaml"
            blueprint_file.write_text(
                yaml.safe_dump(
                    {
                        "name": "schema-test",
                        "template_type": "single-agent",
                        "workdir": str(root),
                        "agents": [
                            {
                                "id": "worker",
                                "uses_memory": False,
                                "next_options": ["finish"],
                                "output_schema_path": "schemas/worker-output.json",
                            },
                        ],
                        "workflow": {"start_at": "worker", "run_root": ".runs"},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            compiled = compile_blueprint(str(blueprint_file))
            agent_schema = compiled.agents_by_id["worker"].schema
            self.assertIn("summary", agent_schema["required"])
            self.assertIn("summary", agent_schema["properties"])

    def test_compile_falls_back_to_default_schema(self) -> None:
        """When no schema file on disk, compile_blueprint auto-generates one."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            blueprint_file = root / "workflow.yaml"
            blueprint_file.write_text(
                yaml.safe_dump(
                    {
                        "name": "default-schema-test",
                        "template_type": "single-agent",
                        "workdir": str(root),
                        "agents": [
                            {
                                "id": "worker",
                                "uses_memory": False,
                                "next_options": ["finish"],
                            },
                        ],
                        "workflow": {"start_at": "worker", "run_root": ".runs"},
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            compiled = compile_blueprint(str(blueprint_file))
            schema = compiled.agents_by_id["worker"].schema
            self.assertEqual(schema["type"], "object")
            self.assertIn("success", schema["properties"])
            self.assertIn("next", schema["properties"])
            self.assertFalse(schema["additionalProperties"])


class ProviderConfigTests(unittest.TestCase):
    def test_parse_codex_config(self) -> None:
        config = _parse_provider_config({"type": "codex", "model": "gpt-4", "approval": "always"})
        self.assertIsInstance(config, CodexConfig)
        self.assertEqual(config.model, "gpt-4")
        self.assertEqual(config.approval, "always")

    def test_parse_claude_code_config(self) -> None:
        config = _parse_provider_config({"type": "claude-code", "model": "sonnet", "max_turns": 5})
        self.assertIsInstance(config, ClaudeCodeConfig)
        self.assertEqual(config.model, "sonnet")
        self.assertEqual(config.max_turns, 5)

    def test_parse_generic_config(self) -> None:
        config = _parse_provider_config({
            "type": "generic",
            "command_template": "my-tool {prompt_file}",
            "output_mode": "stdout",
        })
        self.assertIsInstance(config, GenericConfig)
        self.assertEqual(config.command_template, "my-tool {prompt_file}")
        self.assertEqual(config.output_mode, "stdout")

    def test_parse_empty_defaults_to_codex(self) -> None:
        config = _parse_provider_config({})
        self.assertIsInstance(config, CodexConfig)

    def test_parse_unknown_type_raises(self) -> None:
        with self.assertRaises(ScaffoldError):
            _parse_provider_config({"type": "unknown-provider"})


if __name__ == "__main__":
    unittest.main()
