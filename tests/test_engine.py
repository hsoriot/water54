from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from agent_workflow.engine import load_workflow, run_workflow


class WorkflowEngineTests(unittest.TestCase):
    def _fake_codex_path(self) -> Path:
        return Path(__file__).with_name("fake_codex.py")

    def _fake_claude_path(self) -> Path:
        return Path(__file__).with_name("fake_claude.py")

    def _write_blueprint(self, root: Path, content: str) -> Path:
        path = root / "workflow.yaml"
        path.write_text(dedent(content).strip(), encoding="utf-8")
        return path

    def test_branching_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_codex = self._fake_codex_path()
            wf = self._write_blueprint(root, f"""
                name: test-branch
                template_type: multi-agent
                workdir: {root}
                provider:
                  type: codex
                  bin: {fake_codex}
                agents:
                  - id: classify
                    uses_memory: false
                    prompt: "branch=patch"
                    next_options: [patch, finish]
                  - id: patch
                    uses_memory: false
                    prompt: "render-previous={{{{ steps.classify.output.next }}}}"
                    next_options: [finish]
                workflow:
                  start_at: classify
                  run_root: runs
            """)

            result = run_workflow(load_workflow(str(wf)))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual([s.step_id for s in result.step_results], ["classify", "patch"])
            self.assertEqual(result.step_results[-1].payload["saw_previous"], "patch")

    def test_failure_stops_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_codex = self._fake_codex_path()
            wf = self._write_blueprint(root, f"""
                name: test-failure
                template_type: single-agent
                workdir: {root}
                provider:
                  type: codex
                  bin: {fake_codex}
                agents:
                  - id: first
                    uses_memory: false
                    prompt: "force-failure"
                    next_options: [finish]
                workflow:
                  start_at: first
                  run_root: runs
            """)

            result = run_workflow(load_workflow(str(wf)))

            self.assertEqual(result.status, "failed")
            self.assertEqual(len(result.step_results), 1)
            manifest = json.loads((result.run_dir / "run_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")

    def test_loop_with_max_visits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_codex = self._fake_codex_path()
            wf = self._write_blueprint(root, f"""
                name: test-loop
                template_type: multi-agent
                workdir: {root}
                provider:
                  type: codex
                  bin: {fake_codex}
                agents:
                  - id: review
                    uses_memory: false
                    prompt: "loop-attempt={{{{ current_step.attempt }}}}"
                    max_visits: 3
                    next_options: [review, done, finish]
                  - id: done
                    uses_memory: false
                    prompt: "done-step"
                    next_options: [finish]
                workflow:
                  start_at: review
                  max_steps: 6
                  run_root: runs
            """)

            result = run_workflow(load_workflow(str(wf)))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual([s.step_id for s in result.step_results], ["review", "review", "done"])
            self.assertEqual([s.attempt for s in result.step_results if s.step_id == "review"], [1, 2])
            self.assertTrue((result.run_dir / "review__02" / "review.json").exists())

    def test_parallel_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_codex = self._fake_codex_path()
            wf = self._write_blueprint(root, f"""
                name: test-parallel
                template_type: multi-agent
                workdir: {root}
                provider:
                  type: codex
                  bin: {fake_codex}
                agents:
                  - id: fanout
                    parallel: [worker_a, worker_b]
                    join: merge
                  - id: worker_a
                    uses_memory: false
                    prompt: "parallel-a"
                    next_options: [finish]
                  - id: worker_b
                    uses_memory: false
                    prompt: "parallel-b"
                    next_options: [finish]
                  - id: merge
                    uses_memory: false
                    prompt: "merge-a={{{{ steps.worker_a.output.worker }}}},b={{{{ steps.worker_b.output.worker }}}}"
                    next_options: [finish]
                workflow:
                  start_at: fanout
                  run_root: runs
            """)

            result = run_workflow(load_workflow(str(wf)))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual(
                [s.step_id for s in result.step_results],
                ["worker_a", "worker_b", "fanout", "merge"],
            )
            fanout_payload = next(s.payload for s in result.step_results if s.step_id == "fanout")
            self.assertTrue(fanout_payload["success"])
            self.assertTrue(result.step_results[-1].payload["merged"])

    def test_relative_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            prompts_dir = root / "wf" / "prompts"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "first.md").write_text("done-step", encoding="utf-8")
            fake_codex = self._fake_codex_path()
            wf = root / "wf" / "workflow.yaml"
            wf.write_text(dedent(f"""
                name: test-relative
                template_type: single-agent
                workdir: ..
                provider:
                  type: codex
                  bin: {fake_codex}
                agents:
                  - id: first
                    uses_memory: false
                    prompt_path: prompts/first.md
                    next_options: [finish]
                workflow:
                  start_at: first
                  run_root: runs
            """).strip(), encoding="utf-8")

            workflow = load_workflow(str(wf))
            self.assertEqual(workflow.workdir, str(root.resolve()))
            result = run_workflow(workflow)
            self.assertEqual(result.status, "succeeded")

    def test_claude_code_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_claude = self._fake_claude_path()
            wf = self._write_blueprint(root, f"""
                name: test-claude
                template_type: multi-agent
                workdir: {root}
                provider:
                  type: claude-code
                  bin: {fake_claude}
                  model: sonnet
                  max_turns: 1
                agents:
                  - id: analyze
                    uses_memory: false
                    prompt: "branch=patch"
                    next_options: [patch, finish]
                  - id: patch
                    uses_memory: false
                    prompt: "render-previous={{{{ steps.analyze.output.next }}}}"
                    next_options: [finish]
                workflow:
                  start_at: analyze
                  run_root: runs
            """)

            result = run_workflow(load_workflow(str(wf)))

            self.assertEqual(result.status, "succeeded")
            self.assertEqual([s.step_id for s in result.step_results], ["analyze", "patch"])
            self.assertEqual(result.step_results[-1].payload["saw_previous"], "patch")


    def test_cursor_deleted_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_codex = self._fake_codex_path()
            wf = self._write_blueprint(root, f"""
                name: test-cursor-clean
                template_type: single-agent
                workdir: {root}
                provider:
                  type: codex
                  bin: {fake_codex}
                agents:
                  - id: first
                    uses_memory: false
                    prompt: "done-step"
                    next_options: [finish]
                workflow:
                  start_at: first
                  run_root: runs
            """)

            result = run_workflow(load_workflow(str(wf)))
            self.assertEqual(result.status, "succeeded")
            cursor_path = root / ".cursor.yaml"
            self.assertFalse(cursor_path.exists(), "cursor should be deleted after successful run")

    def test_cursor_resume(self) -> None:
        """Simulate a crash by writing a cursor, then verify resume."""
        import yaml
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_codex = self._fake_codex_path()
            wf = self._write_blueprint(root, f"""
                name: test-cursor-resume
                template_type: multi-agent
                workdir: {root}
                provider:
                  type: codex
                  bin: {fake_codex}
                agents:
                  - id: step_a
                    uses_memory: false
                    prompt: "done-step"
                    next_options: [step_b, finish]
                  - id: step_b
                    uses_memory: false
                    prompt: "done-step"
                    next_options: [finish]
                workflow:
                  start_at: step_a
                  run_root: runs
            """)

            # First run — completes normally, creates a run dir
            workflow = load_workflow(str(wf))
            result1 = run_workflow(workflow)
            self.assertEqual(result1.status, "succeeded")
            run_dir = result1.run_dir

            # Now fake a cursor that says we're at step_b, reusing the same run_dir
            cursor_path = root / ".cursor.yaml"
            cursor_data = {
                "workflow": "test-cursor-resume",
                "run_dir": str(run_dir),
                "current_step": "step_b",
                "total_steps": 1,
                "step_attempts": {"step_a": 1},
                "completed_steps": [],
            }
            cursor_path.write_text(yaml.safe_dump(cursor_data), encoding="utf-8")

            # Second run — should resume from step_b
            workflow2 = load_workflow(str(wf))
            result2 = run_workflow(workflow2)
            self.assertEqual(result2.status, "succeeded")
            # Should have run only step_b since cursor said start there
            self.assertEqual([s.step_id for s in result2.step_results], ["step_b"])
            # Cursor should be cleaned up
            self.assertFalse(cursor_path.exists())

    def test_cursor_manual_redirect(self) -> None:
        """User edits cursor to skip to a different step."""
        import yaml
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            fake_codex = self._fake_codex_path()
            wf = self._write_blueprint(root, f"""
                name: test-cursor-redirect
                template_type: multi-agent
                workdir: {root}
                provider:
                  type: codex
                  bin: {fake_codex}
                agents:
                  - id: alpha
                    uses_memory: false
                    prompt: "done-step"
                    next_options: [beta, finish]
                  - id: beta
                    uses_memory: false
                    prompt: "done-step"
                    next_options: [finish]
                workflow:
                  start_at: alpha
                  run_root: runs
            """)

            # Create a run dir manually
            runs_dir = root / "runs"
            runs_dir.mkdir()
            run_dir = runs_dir / "manual-run"
            run_dir.mkdir()

            # Write cursor pointing directly at beta (skip alpha)
            cursor_path = root / ".cursor.yaml"
            cursor_data = {
                "workflow": "test-cursor-redirect",
                "run_dir": str(run_dir),
                "current_step": "beta",
                "total_steps": 1,
                "step_attempts": {"alpha": 1},
                "completed_steps": [],
            }
            cursor_path.write_text(yaml.safe_dump(cursor_data), encoding="utf-8")

            workflow = load_workflow(str(wf))
            result = run_workflow(workflow)
            self.assertEqual(result.status, "succeeded")
            self.assertEqual([s.step_id for s in result.step_results], ["beta"])


if __name__ == "__main__":
    unittest.main()
