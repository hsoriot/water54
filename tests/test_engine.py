from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_workflow_automation.engine import load_workflow, run_workflow


class WorkflowEngineTests(unittest.TestCase):
    def test_branching_workflow_uses_json_next_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow_file = root / "workflow.yaml"
            fake_codex = Path(__file__).with_name("fake_codex.py")
            workflow_file.write_text(
                f"""
name: test-branch
workdir: {root}
run_root: runs
start_at: classify
codex:
  bin: {fake_codex}
  approval: never
  sandbox: danger-full-access
steps:
  classify:
    prompt: "branch=fix"
    branches:
      fix: patch
      finish: done
  patch:
    prompt: "render-previous={{{{ steps.classify.output.next }}}}"
  done:
    prompt: "branch=finish"
""".strip(),
                encoding="utf-8",
            )

            result = run_workflow(load_workflow(str(workflow_file)))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual([step.step_id for step in result.step_results], ["classify", "patch"])
            patch_payload = result.step_results[-1].payload
            self.assertEqual(patch_payload["saw_previous"], "fix")

    def test_failure_stops_workflow_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow_file = root / "workflow.yaml"
            fake_codex = Path(__file__).with_name("fake_codex.py")
            workflow_file.write_text(
                f"""
name: test-failure
workdir: {root}
run_root: runs
start_at: first
codex:
  bin: {fake_codex}
  approval: never
  sandbox: danger-full-access
steps:
  first:
    prompt: "force-failure"
""".strip(),
                encoding="utf-8",
            )

            result = run_workflow(load_workflow(str(workflow_file)))

            self.assertEqual(result.status, "failed")
            self.assertEqual(len(result.step_results), 1)
            manifest_path = result.run_dir / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")


if __name__ == "__main__":
    unittest.main()
