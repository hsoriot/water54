from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from codex_workflow_automation.scaffold import load_blueprint, scaffold_blueprint


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
            self.assertEqual(workflow["start_at"], "planner")
            self.assertEqual(workflow["vars"]["shared_handoff"], "shared/handoff.md")
            self.assertEqual(workflow["steps"]["planner"]["branches"]["executor"], "executor")

            schema = json.loads((out_dir / "schemas" / "planner-output.json").read_text(encoding="utf-8"))
            self.assertEqual(schema["properties"]["next"]["enum"], ["executor", "finish", "__end__"])


if __name__ == "__main__":
    unittest.main()
