from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from codex_workflow_automation.engine import run_workflow
from codex_workflow_automation.scaffold import compile_blueprint_to_workflow, load_blueprint, scaffold_blueprint


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
                        "control": {"enabled": True},
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
            self.assertTrue((out_dir / "control.yaml").exists())
            self.assertTrue((out_dir / "prompts" / "planner.md").exists())
            self.assertTrue((out_dir / "schemas" / "planner-output.json").exists())
            self.assertTrue((out_dir / "memory" / "planner.md").exists())
            self.assertTrue((out_dir / "shared" / "handoff.md").exists())

            workflow = yaml.safe_load((out_dir / "workflow.yaml").read_text(encoding="utf-8"))
            self.assertEqual(workflow["workflow"]["start_at"], "planner")
            self.assertEqual(workflow["agents"][0]["uses_shared"], ["handoff"])

            compiled = compile_blueprint_to_workflow(str(out_dir / "workflow.yaml"))
            self.assertEqual(compiled.start_at, "planner")
            self.assertEqual(compiled.vars["shared_handoff"], "shared/handoff.md")
            self.assertEqual(compiled.steps["planner"].branches["executor"], "executor")

    def test_runner_can_execute_blueprint_yaml_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            generated = root / "generated"
            scaffold_blueprint(load_blueprint("/Users/riot/riot/codex-workflow-automation/examples/scaffold-blueprint.yaml"), str(generated))

            fake_codex = Path("/Users/riot/riot/codex-workflow-automation/tests/fake_codex.py")
            workflow_path = generated / "workflow.yaml"
            workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
            workflow["workdir"] = str(generated)
            workflow["agents"][0]["prompt_path"] = "prompts/planner.md"
            workflow["agents"][1]["prompt_path"] = "prompts/executor.md"
            workflow["workflow"]["run_root"] = "runs"
            workflow_path.write_text(yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8")

            compiled = compile_blueprint_to_workflow(str(workflow_path))
            compiled.codex.bin = str(fake_codex)
            result = run_workflow(compiled)

            self.assertEqual(result.status, "succeeded")
            self.assertGreaterEqual(len(result.step_results), 1)


if __name__ == "__main__":
    unittest.main()
