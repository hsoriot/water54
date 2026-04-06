from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
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
    workflow_path = Path(path).resolve()
    base_dir = workflow_path.parent
    raw = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
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
            parallel=_ensure_string_list(step_value.get("parallel", []), f"steps.{step_id}.parallel"),
            join=step_value.get("join"),
            branches=_ensure_string_map(step_value.get("branches", {}), f"steps.{step_id}.branches"),
            on_success=step_value.get("on_success"),
            on_failure=step_value.get("on_failure"),
            max_visits=step_value.get("max_visits"),
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
        source_path=str(workflow_path),
        workdir=_resolve_path(str(raw["workdir"]), base_dir),
        run_root=str(raw.get("run_root", ".runs")),
        max_steps=int(raw.get("max_steps", 50)),
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
    step_attempts: dict[str, int] = {}
    total_steps = 0
    status = "succeeded"

    while current_step != TERMINAL_ROUTE:
        if current_step not in workflow.steps:
            raise WorkflowError(f"unknown step '{current_step}'")
        total_steps += 1
        if total_steps > workflow.max_steps:
            raise WorkflowError(
                f"workflow exceeded max_steps={workflow.max_steps}; last attempted step was '{current_step}'"
            )

        step = workflow.steps[current_step]
        next_route, emitted_results, step_failed = _execute_step(
            workflow=workflow,
            step=step,
            run_dir=run_dir,
            previous_results=step_results,
            cli_vars=cli_vars,
            step_attempts=step_attempts,
        )
        step_results.extend(emitted_results)

        if step_failed:
            if next_route == TERMINAL_ROUTE:
                status = "failed"
            current_step = next_route
        else:
            current_step = next_route

        _write_run_manifest(workflow, run_dir, step_results, status)

        if step_failed and current_step == TERMINAL_ROUTE:
            break

    _write_run_manifest(workflow, run_dir, step_results, status)
    return RunResult(
        run_dir=run_dir,
        workflow_name=workflow.name,
        status=status,
        step_results=step_results,
    )


def _execute_step(
    workflow: WorkflowConfig,
    step: StepConfig,
    run_dir: Path,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    step_attempts: dict[str, int],
) -> tuple[str, list[StepResult], bool]:
    if step.parallel:
        return _run_parallel_step(
            workflow=workflow,
            step=step,
            run_dir=run_dir,
            previous_results=previous_results,
            cli_vars=cli_vars,
            step_attempts=step_attempts,
        )

    result = _run_codex_step(
        workflow=workflow,
        step=step,
        run_dir=run_dir,
        previous_results=previous_results,
        cli_vars=cli_vars,
        step_attempts=step_attempts,
    )
    if result.success:
        return _resolve_success_route(step, result.next_route), [result], False
    failure_route = step.on_failure or TERMINAL_ROUTE
    return failure_route, [result], True


def _run_codex_step(
    workflow: WorkflowConfig,
    step: StepConfig,
    run_dir: Path,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    step_attempts: dict[str, int],
    attempt: int | None = None,
) -> StepResult:
    if attempt is None:
        attempt = _reserve_attempt(step, step_attempts)
    step_dir = _make_step_dir(run_dir, step.id, attempt)

    output_path = step_dir / (step.output_file or "output.json")
    stdout_path = step_dir / "stdout.log"
    stderr_path = step_dir / "stderr.log"
    schema_path = step_dir / "schema.json"

    rendered_prompt = _load_and_render_prompt(
        workflow=workflow,
        step=step,
        previous_results=previous_results,
        cli_vars=cli_vars,
        run_dir=run_dir,
        attempt=attempt,
    )
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
        attempt=attempt,
        success=bool(payload["success"]),
        next_route=str(payload["next"]),
        output_path=output_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        payload=payload,
    )


def _run_parallel_step(
    workflow: WorkflowConfig,
    step: StepConfig,
    run_dir: Path,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    step_attempts: dict[str, int],
) -> tuple[str, list[StepResult], bool]:
    attempt = _reserve_attempt(step, step_attempts)
    step_dir = _make_step_dir(run_dir, step.id, attempt)
    rendered_prompt = _load_parallel_prompt(workflow, step, previous_results, cli_vars, run_dir, attempt)
    (step_dir / "prompt.txt").write_text(rendered_prompt, encoding="utf-8")
    (step_dir / "schema.json").write_text("{}", encoding="utf-8")

    child_attempts = {
        child_step_id: _reserve_attempt(workflow.steps[child_step_id], step_attempts) for child_step_id in step.parallel
    }
    child_results: list[StepResult] = []
    with ThreadPoolExecutor(max_workers=len(step.parallel)) as executor:
        futures = [
            executor.submit(
                _run_codex_step,
                workflow,
                workflow.steps[child_step_id],
                run_dir,
                previous_results,
                cli_vars,
                step_attempts,
                child_attempts[child_step_id],
            )
            for child_step_id in step.parallel
        ]
        for future in futures:
            child_results.append(future.result())
    all_success = all(result.success for result in child_results)
    block_payload = {
        "success": all_success,
        "next": step.join or TERMINAL_ROUTE,
        "parallel_steps": [result.step_id for result in child_results],
        "child_results": [
            {
                "step_id": result.step_id,
                "attempt": result.attempt,
                "success": result.success,
                "next": result.next_route,
                "output_path": str(result.output_path),
            }
            for result in child_results
        ],
    }
    output_path = step_dir / (step.output_file or "parallel.json")
    output_path.write_text(json.dumps(block_payload, indent=2), encoding="utf-8")
    stdout_path = step_dir / "stdout.log"
    stderr_path = step_dir / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    block_result = StepResult(
        step_id=step.id,
        attempt=attempt,
        success=all_success,
        next_route=step.join or TERMINAL_ROUTE,
        output_path=output_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        payload=block_payload,
    )
    emitted_results = [*child_results, block_result]
    if all_success:
        return _resolve_parallel_success_route(step), emitted_results, False
    failure_route = step.on_failure or TERMINAL_ROUTE
    block_result.next_route = failure_route
    block_payload["next"] = failure_route
    output_path.write_text(json.dumps(block_payload, indent=2), encoding="utf-8")
    return failure_route, emitted_results, True


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
    attempt: int,
) -> str:
    if step.prompt and step.prompt_file:
        raise WorkflowError(f"step '{step.id}' cannot set both prompt and prompt_file")
    if step.prompt_file:
        prompt = Path(_resolve_path(step.prompt_file, Path(workflow.source_path).parent)).read_text(encoding="utf-8")
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
        "current_step": {
            "id": step.id,
            "attempt": attempt,
        },
        "steps": {
            result.step_id: {
                "attempt": result.attempt,
                "output": result.payload,
                "output_path": str(result.output_path),
                "stdout_path": str(result.stdout_path),
                "stderr_path": str(result.stderr_path),
            }
            for result in previous_results
        },
    }
    return render_template(prompt, context)


def _load_parallel_prompt(
    workflow: WorkflowConfig,
    step: StepConfig,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    run_dir: Path,
    attempt: int,
) -> str:
    if step.prompt or step.prompt_file:
        return _load_and_render_prompt(
            workflow=workflow,
            step=step,
            previous_results=previous_results,
            cli_vars=cli_vars,
            run_dir=run_dir,
            attempt=attempt,
        )
    return f"Parallel block: {step.id}. Join target: {step.join or TERMINAL_ROUTE}. Children: {', '.join(step.parallel)}"


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


def _resolve_parallel_success_route(step: StepConfig) -> str:
    if not step.join:
        return TERMINAL_ROUTE
    return step.join


def _validate_step(step: StepConfig) -> None:
    if step.on_success and step.branches:
        raise WorkflowError(
            f"step '{step.id}' cannot define both branches and on_success; branches already decide success routing"
        )
    if step.parallel:
        if step.prompt and step.prompt_file:
            raise WorkflowError(f"parallel step '{step.id}' cannot set both prompt and prompt_file")
        if step.branches:
            raise WorkflowError(f"parallel step '{step.id}' cannot define branches")
        if step.on_success:
            raise WorkflowError(f"parallel step '{step.id}' cannot define on_success; use join")
    elif step.join:
        raise WorkflowError(f"step '{step.id}' cannot define join without parallel")
    if step.max_visits is not None and (not isinstance(step.max_visits, int) or step.max_visits < 1):
        raise WorkflowError(f"step '{step.id}' max_visits must be a positive integer")


def _validate_workflow(workflow: WorkflowConfig) -> None:
    if workflow.start_at not in workflow.steps:
        raise WorkflowError(f"workflow.start_at '{workflow.start_at}' is not a known step")
    if workflow.max_steps < 1:
        raise WorkflowError("workflow.max_steps must be a positive integer")
    for step in workflow.steps.values():
        targets = []
        if step.on_success:
            targets.append(step.on_success)
        if step.on_failure:
            targets.append(step.on_failure)
        targets.extend(step.branches.values())
        if step.join:
            targets.append(step.join)
        for parallel_step in step.parallel:
            if parallel_step not in workflow.steps:
                raise WorkflowError(f"parallel step '{step.id}' points to unknown child step '{parallel_step}'")
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
                "attempt": result.attempt,
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


def _resolve_path(value: str, base_dir: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


def _reserve_attempt(step: StepConfig, step_attempts: dict[str, int]) -> int:
    attempt = step_attempts.get(step.id, 0) + 1
    if step.max_visits is not None and attempt > step.max_visits:
        raise WorkflowError(f"step '{step.id}' exceeded max_visits={step.max_visits}")
    step_attempts[step.id] = attempt
    return attempt


def _make_step_dir(run_dir: Path, step_id: str, attempt: int) -> Path:
    suffix = "" if attempt == 1 else f"__{attempt:02d}"
    step_dir = run_dir / f"{step_id}{suffix}"
    step_dir.mkdir(parents=True, exist_ok=False)
    return step_dir
