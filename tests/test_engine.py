from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_workflow_automation.engine import load_workflow, run_workflow


class WorkflowEngineTests(unittest.TestCase):
    def _fake_codex_path(self) -> Path:
        return Path(__file__).with_name("fake_codex.py")

    def test_branching_workflow_uses_json_next_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow_file = root / "workflow.yaml"
            fake_codex = self._fake_codex_path()
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
    prompt: "done-step"
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
            fake_codex = self._fake_codex_path()
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

    def test_loop_revisits_step_until_branch_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow_file = root / "workflow.yaml"
            fake_codex = self._fake_codex_path()
            workflow_file.write_text(
                f"""
name: test-loop
workdir: {root}
run_root: runs
start_at: review
max_steps: 6
codex:
  bin: {fake_codex}
  approval: never
  sandbox: danger-full-access
steps:
  review:
    prompt: "loop-attempt={{{{ current_step.attempt }}}}"
    max_visits: 3
    branches:
      retry: review
      done: finish
  finish:
    prompt: "done-step"
""".strip(),
                encoding="utf-8",
            )

            result = run_workflow(load_workflow(str(workflow_file)))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual([step.step_id for step in result.step_results], ["review", "review", "finish"])
            self.assertEqual([step.attempt for step in result.step_results if step.step_id == "review"], [1, 2])
            second_review_path = result.run_dir / "review__02" / "output.json"
            self.assertTrue(second_review_path.exists())

    def test_parallel_block_runs_children_and_joins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            workflow_file = root / "workflow.yaml"
            fake_codex = self._fake_codex_path()
            workflow_file.write_text(
                f"""
name: test-parallel
workdir: {root}
run_root: runs
start_at: fanout
codex:
  bin: {fake_codex}
  approval: never
  sandbox: danger-full-access
steps:
  fanout:
    parallel:
      - worker_a
      - worker_b
    join: merge
  worker_a:
    prompt: "parallel-a"
  worker_b:
    prompt: "parallel-b"
  merge:
    prompt: "merge-a={{{{ steps.worker_a.output.worker }}}},b={{{{ steps.worker_b.output.worker }}}}"
""".strip(),
                encoding="utf-8",
            )

            result = run_workflow(load_workflow(str(workflow_file)))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(
                [step.step_id for step in result.step_results],
                ["worker_a", "worker_b", "fanout", "merge"],
            )
            fanout_payload = next(step.payload for step in result.step_results if step.step_id == "fanout")
            self.assertTrue(fanout_payload["success"])
            self.assertEqual(fanout_payload["parallel_steps"], ["worker_a", "worker_b"])
            merge_payload = result.step_results[-1].payload
            self.assertTrue(merge_payload["merged"])


if __name__ == "__main__":
    unittest.main()
