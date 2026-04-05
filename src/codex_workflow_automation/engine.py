from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from codex_workflow_automation.models import (
    TERMINAL_ROUTE,
    CodexConfig,
    RunResult,
    StepConfig,
    StepResult,
    WorkflowConfig,
)
from codex_workflow_automation.templating import render_template


class WorkflowError(RuntimeError):
    pass


def load_workflow(path: str) -> WorkflowConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise WorkflowError("workflow file must be a YAML object")

    required = ["name", "start_at", "workdir", "steps"]
    for key in required:
        if key not in raw:
            raise WorkflowError(f"workflow missing required field: {key}")

    steps_raw = raw["steps"]
    if not isinstance(steps_raw, dict) or not steps_raw:
        raise WorkflowError("workflow.steps must be a non-empty mapping")

    codex_raw = raw.get("codex", {})
    if not isinstance(codex_raw, dict):
        raise WorkflowError("workflow.codex must be a mapping")

    steps: dict[str, StepConfig] = {}
    for step_id, step_value in steps_raw.items():
        if not isinstance(step_value, dict):
            raise WorkflowError(f"step '{step_id}' must be a mapping")
        steps[step_id] = StepConfig(
            id=step_id,
            prompt=step_value.get("prompt"),
            prompt_file=step_value.get("prompt_file"),
            output_file=step_value.get("output_file"),
            schema=step_value.get("schema"),
            branches=_ensure_string_map(step_value.get("branches", {}), f"steps.{step_id}.branches"),
            on_success=step_value.get("on_success"),
            on_failure=step_value.get("on_failure"),
            model=step_value.get("model"),
            workdir=step_value.get("workdir"),
            codex_extra_args=_ensure_string_list(
                step_value.get("codex_extra_args", []),
                f"steps.{step_id}.codex_extra_args",
            ),
        )
        _validate_step(steps[step_id])

    workflow = WorkflowConfig(
        name=str(raw["name"]),
        start_at=str(raw["start_at"]),
        workdir=str(raw["workdir"]),
        run_root=str(raw.get("run_root", ".runs")),
        vars=raw.get("vars", {}) or {},
        codex=CodexConfig(
            bin=str(codex_raw.get("bin", "codex")),
            model=codex_raw.get("model"),
            approval=str(codex_raw.get("approval", "never")),
            sandbox=str(codex_raw.get("sandbox", "danger-full-access")),
            skip_git_repo_check=bool(codex_raw.get("skip_git_repo_check", True)),
            extra_args=_ensure_string_list(codex_raw.get("extra_args", []), "codex.extra_args"),
        ),
        steps=steps,
    )
    _validate_workflow(workflow)
    return workflow


def run_workflow(workflow: WorkflowConfig, cli_vars: dict[str, str] | None = None) -> RunResult:
    cli_vars = cli_vars or {}
    run_dir = _create_run_dir(workflow)
    step_results: list[StepResult] = []
    current_step = workflow.start_at
    visited: set[str] = set()
    status = "succeeded"

    while current_step != TERMINAL_ROUTE:
        if current_step in visited:
            raise WorkflowError(f"workflow loop detected at step '{current_step}'")
        visited.add(current_step)

        if current_step not in workflow.steps:
            raise WorkflowError(f"unknown step '{current_step}'")

        step = workflow.steps[current_step]
        result = _run_step(workflow, step, run_dir, step_results, cli_vars)
        step_results.append(result)

        if result.success:
            current_step = _resolve_success_route(step, result.next_route)
        else:
            failure_route = step.on_failure or TERMINAL_ROUTE
            if failure_route == TERMINAL_ROUTE:
                status = "failed"
            current_step = failure_route

        _write_run_manifest(workflow, run_dir, step_results, status)

        if not result.success and current_step == TERMINAL_ROUTE:
            break

    _write_run_manifest(workflow, run_dir, step_results, status)
    return RunResult(
        run_dir=run_dir,
        workflow_name=workflow.name,
        status=status,
        step_results=step_results,
    )


def _run_step(
    workflow: WorkflowConfig,
    step: StepConfig,
    run_dir: Path,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
) -> StepResult:
    step_dir = run_dir / step.id
    step_dir.mkdir(parents=True, exist_ok=False)

    output_path = step_dir / (step.output_file or "output.json")
    stdout_path = step_dir / "stdout.log"
    stderr_path = step_dir / "stderr.log"
    schema_path = step_dir / "schema.json"

    rendered_prompt = _load_and_render_prompt(workflow, step, previous_results, cli_vars, run_dir)
    schema = _build_schema(step)
    schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    (step_dir / "prompt.txt").write_text(rendered_prompt, encoding="utf-8")

    cmd = _build_codex_command(
        codex=workflow.codex,
        step=step,
        workdir=step.workdir or workflow.workdir,
        schema_path=schema_path,
        output_path=output_path,
    )

    completed = subprocess.run(
        cmd,
        input=rendered_prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")

    if completed.returncode != 0:
        raise WorkflowError(
            f"codex command failed for step '{step.id}' with exit code {completed.returncode}; "
            f"see {stderr_path}"
        )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    _validate_step_payload(step.id, payload)

    return StepResult(
        step_id=step.id,
        success=bool(payload["success"]),
        next_route=str(payload["next"]),
        output_path=output_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        payload=payload,
    )


def _build_codex_command(
    *,
    codex: CodexConfig,
    step: StepConfig,
    workdir: str,
    schema_path: Path,
    output_path: Path,
) -> list[str]:
    command = [codex.bin]
    command.extend(codex.extra_args)
    if codex.model or step.model:
        command.extend(["-m", step.model or codex.model or ""])
    command.extend(["-a", codex.approval, "exec"])
    if codex.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    command.extend(["--sandbox", codex.sandbox, "-C", workdir, "--output-schema", str(schema_path), "-o", str(output_path)])
    command.extend(step.codex_extra_args)
    command.append("-")
    return command


def _load_and_render_prompt(
    workflow: WorkflowConfig,
    step: StepConfig,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    run_dir: Path,
) -> str:
    if step.prompt and step.prompt_file:
        raise WorkflowError(f"step '{step.id}' cannot set both prompt and prompt_file")
    if step.prompt_file:
        prompt = Path(step.prompt_file).read_text(encoding="utf-8")
    elif step.prompt:
        prompt = step.prompt
    else:
        raise WorkflowError(f"step '{step.id}' must define prompt or prompt_file")

    context = {
        "workflow": {
            "name": workflow.name,
            "workdir": workflow.workdir,
            "run_root": workflow.run_root,
        },
        "vars": {**workflow.vars, **cli_vars},
        "run": {
            "dir": str(run_dir),
        },
        "steps": {
            result.step_id: {
                "output": result.payload,
                "output_path": str(result.output_path),
                "stdout_path": str(result.stdout_path),
                "stderr_path": str(result.stderr_path),
            }
            for result in previous_results
        },
    }
    return render_template(prompt, context)


def _build_schema(step: StepConfig) -> dict[str, Any]:
    if step.schema:
        if step.schema.get("type") == "object" and step.schema.get("additionalProperties") is not False:
            raise WorkflowError(
                f"step '{step.id}' schema must set additionalProperties to false at the root object"
            )
        return step.schema
    enum_values = sorted(step.branches) if step.branches else None
    next_property: dict[str, Any] = {"type": "string"}
    if enum_values:
        next_property["enum"] = enum_values + [TERMINAL_ROUTE]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["success", "next"],
        "properties": {
            "success": {"type": "boolean"},
            "next": next_property,
        },
        "additionalProperties": False,
    }


def _resolve_success_route(step: StepConfig, next_route: str) -> str:
    if step.branches:
        if next_route not in step.branches and next_route != TERMINAL_ROUTE:
            raise WorkflowError(
                f"step '{step.id}' returned next='{next_route}', which is not defined in branches"
            )
        return step.branches.get(next_route, TERMINAL_ROUTE)
    if next_route and next_route != TERMINAL_ROUTE:
        return next_route
    if step.on_success:
        return step.on_success
    return TERMINAL_ROUTE


def _validate_step(step: StepConfig) -> None:
    if step.on_success and step.branches:
        raise WorkflowError(
            f"step '{step.id}' cannot define both branches and on_success; branches already decide success routing"
        )


def _validate_workflow(workflow: WorkflowConfig) -> None:
    if workflow.start_at not in workflow.steps:
        raise WorkflowError(f"workflow.start_at '{workflow.start_at}' is not a known step")
    for step in workflow.steps.values():
        targets = []
        if step.on_success:
            targets.append(step.on_success)
        if step.on_failure:
            targets.append(step.on_failure)
        targets.extend(step.branches.values())
        for target in targets:
            if target != TERMINAL_ROUTE and target not in workflow.steps:
                raise WorkflowError(f"step '{step.id}' points to unknown step '{target}'")


def _validate_step_payload(step_id: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise WorkflowError(f"step '{step_id}' output must be a JSON object")
    if "success" not in payload or "next" not in payload:
        raise WorkflowError(f"step '{step_id}' output must contain 'success' and 'next'")
    if not isinstance(payload["success"], bool):
        raise WorkflowError(f"step '{step_id}' output field 'success' must be boolean")
    if not isinstance(payload["next"], str):
        raise WorkflowError(f"step '{step_id}' output field 'next' must be string")


def _create_run_dir(workflow: WorkflowConfig) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = Path(workflow.run_root)
    if not run_root.is_absolute():
        run_root = Path(workflow.workdir) / run_root
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = run_root / f"{timestamp}-{_slugify(workflow.name)}"
    suffix = 1
    while run_dir.exists():
        run_dir = run_root / f"{timestamp}-{_slugify(workflow.name)}-{suffix:02d}"
        suffix += 1
    run_dir.mkdir(parents=False, exist_ok=False)
    return run_dir


def _write_run_manifest(
    workflow: WorkflowConfig,
    run_dir: Path,
    step_results: list[StepResult],
    status: str,
) -> None:
    manifest = {
        "workflow_name": workflow.name,
        "status": status,
        "start_at": workflow.start_at,
        "workdir": workflow.workdir,
        "run_dir": str(run_dir),
        "steps": [
            {
                "step_id": result.step_id,
                "success": result.success,
                "next": result.next_route,
                "output_path": str(result.output_path),
                "stdout_path": str(result.stdout_path),
                "stderr_path": str(result.stderr_path),
                "payload": result.payload,
            }
            for result in step_results
        ],
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _ensure_string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkflowError(f"{field_name} must be a list of strings")
    return list(value)


def _ensure_string_map(value: Any, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise WorkflowError(f"{field_name} must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise WorkflowError(f"{field_name} must map strings to strings")
        result[key] = item
    return result


def _slugify(value: str) -> str:
    characters = []
    for char in value.lower():
        if char.isalnum():
            characters.append(char)
        elif not characters or characters[-1] != "-":
            characters.append("-")
    slug = "".join(characters).strip("-")
    return slug or "workflow"
