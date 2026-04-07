from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from agent_workflow.models import (
    TERMINAL_ROUTE,
    RunResult,
    StepResult,
)
from agent_workflow.providers import ProviderError, run_provider
from agent_workflow.scaffold import (
    AgentBlueprint,
    ScaffoldError,
    WorkflowBlueprint,
    compile_blueprint,
)
from agent_workflow.templating import render_template

logger = logging.getLogger(__name__)


class WorkflowError(RuntimeError):
    pass


def load_workflow(path: str) -> WorkflowBlueprint:
    try:
        workflow = compile_blueprint(path)
    except ScaffoldError as exc:
        raise WorkflowError(str(exc)) from exc
    _validate_workflow(workflow)
    return workflow


def run_workflow(workflow: WorkflowBlueprint, cli_vars: dict[str, str] | None = None) -> RunResult:
    cli_vars = cli_vars or {}

    # --- cursor: resume from previous run if cursor exists ---
    cursor_path = _cursor_path(workflow)
    cursor = _load_cursor(cursor_path)
    if cursor:
        run_dir = Path(cursor["run_dir"])
        step_results: list[StepResult] = _rebuild_step_results(cursor)
        current_step = cursor["current_step"]
        raw_attempts = cursor.get("step_attempts") or {}
        step_attempts: dict[str, int] = {
            str(k): int(v) for k, v in raw_attempts.items()
        } if isinstance(raw_attempts, dict) else {}
        total_steps = cursor.get("total_steps", len(step_results))
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        run_dir = _create_run_dir(workflow)
        step_results = []
        current_step = workflow.start_at
        step_attempts = {}
        total_steps = 0

    status = "succeeded"

    while current_step != TERMINAL_ROUTE:
        if current_step not in workflow.agents_by_id:
            raise WorkflowError(f"unknown step '{current_step}'")
        total_steps += 1
        if total_steps > workflow.max_steps:
            raise WorkflowError(
                f"workflow exceeded max_steps={workflow.max_steps}; last attempted step was '{current_step}'"
            )

        agent = workflow.agents_by_id[current_step]
        next_route, emitted_results, step_failed = _execute_step(
            workflow=workflow,
            agent=agent,
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

        # Save cursor after each step so we can resume on crash.
        _save_cursor(cursor_path, workflow, run_dir, current_step, step_results, step_attempts, total_steps)

        if step_failed and current_step == TERMINAL_ROUTE:
            break

    _write_run_manifest(workflow, run_dir, step_results, status)
    # Workflow completed — remove cursor.
    if cursor_path.exists():
        cursor_path.unlink()
    return RunResult(
        run_dir=run_dir,
        workflow_name=workflow.name,
        status=status,
        step_results=step_results,
    )


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------

def _cursor_path(workflow: WorkflowBlueprint) -> Path:
    return Path(workflow.source_path).parent / ".cursor.yaml"


def _load_cursor(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        logger.warning("failed to load cursor file %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or "current_step" not in data or "run_dir" not in data:
        return None
    return data


def _save_cursor(
    path: Path,
    workflow: WorkflowBlueprint,
    run_dir: Path,
    current_step: str,
    step_results: list[StepResult],
    step_attempts: dict[str, int],
    total_steps: int,
) -> None:
    data: dict[str, Any] = {
        "workflow": workflow.name,
        "run_dir": str(run_dir),
        "current_step": current_step,
        "total_steps": total_steps,
        "step_attempts": dict(step_attempts),
        "completed_steps": [
            {"step_id": r.step_id, "attempt": r.attempt, "success": r.success, "next": r.next_route,
             "output_path": str(r.output_path), "stdout_path": str(r.stdout_path), "stderr_path": str(r.stderr_path)}
            for r in step_results
        ],
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _rebuild_step_results(cursor: dict[str, Any]) -> list[StepResult]:
    results: list[StepResult] = []
    for entry in cursor.get("completed_steps", []):
        output_path = Path(entry["output_path"])
        payload: dict[str, Any] = {}
        if output_path.exists():
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        results.append(StepResult(
            step_id=entry["step_id"],
            attempt=entry["attempt"],
            success=entry["success"],
            next_route=entry["next"],
            output_path=output_path,
            stdout_path=Path(entry["stdout_path"]),
            stderr_path=Path(entry["stderr_path"]),
            payload=payload,
        ))
    return results


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def _execute_step(
    workflow: WorkflowBlueprint,
    agent: AgentBlueprint,
    run_dir: Path,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    step_attempts: dict[str, int],
) -> tuple[str, list[StepResult], bool]:
    if agent.parallel:
        return _run_parallel_step(
            workflow=workflow,
            agent=agent,
            run_dir=run_dir,
            previous_results=previous_results,
            cli_vars=cli_vars,
            step_attempts=step_attempts,
        )

    result = _run_step(
        workflow=workflow,
        agent=agent,
        run_dir=run_dir,
        previous_results=previous_results,
        cli_vars=cli_vars,
        step_attempts=step_attempts,
    )
    if result.success:
        return _resolve_success_route(agent, result.next_route), [result], False
    failure_route = agent.on_failure or TERMINAL_ROUTE
    return failure_route, [result], True


def _run_step(
    workflow: WorkflowBlueprint,
    agent: AgentBlueprint,
    run_dir: Path,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    step_attempts: dict[str, int],
    attempt: int | None = None,
) -> StepResult:
    if attempt is None:
        attempt = _reserve_attempt(agent, step_attempts)
    step_dir = _make_step_dir(run_dir, agent.id, attempt)

    output_path = step_dir / (agent.output_file or "output.json")
    stdout_path = step_dir / "stdout.log"
    stderr_path = step_dir / "stderr.log"
    schema_path = step_dir / "schema.json"

    rendered_prompt = _load_and_render_prompt(
        workflow=workflow,
        agent=agent,
        previous_results=previous_results,
        cli_vars=cli_vars,
        run_dir=run_dir,
        attempt=attempt,
    )
    schema = agent.schema or {}
    if schema and schema.get("type") == "object" and schema.get("additionalProperties") is not False:
        raise WorkflowError(
            f"agent '{agent.id}' schema must set additionalProperties to false at the root object"
        )
    schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    (step_dir / "prompt.txt").write_text(rendered_prompt, encoding="utf-8")

    try:
        provider_result = run_provider(
            config=workflow.provider,
            prompt=rendered_prompt,
            workdir=workflow.workdir,
            schema_path=schema_path,
            output_path=output_path,
        )
    except ProviderError as exc:
        raise WorkflowError(str(exc)) from exc

    stdout_path.write_text(provider_result.stdout, encoding="utf-8")
    stderr_path.write_text(provider_result.stderr, encoding="utf-8")

    if provider_result.returncode != 0:
        raise WorkflowError(
            f"provider command failed for step '{agent.id}' with exit code {provider_result.returncode}; "
            f"see {stderr_path}"
        )

    payload = provider_result.payload
    if not payload and output_path.exists():
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"step '{agent.id}' output file contains invalid JSON: {exc}"
            ) from exc
    _validate_step_payload(agent.id, payload)

    return StepResult(
        step_id=agent.id,
        attempt=attempt,
        success=bool(payload["success"]),
        next_route=str(payload["next"]),
        output_path=output_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        payload=payload,
    )


def _run_parallel_step(
    workflow: WorkflowBlueprint,
    agent: AgentBlueprint,
    run_dir: Path,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    step_attempts: dict[str, int],
) -> tuple[str, list[StepResult], bool]:
    attempt = _reserve_attempt(agent, step_attempts)
    step_dir = _make_step_dir(run_dir, agent.id, attempt)
    rendered_prompt = _load_parallel_prompt(workflow, agent, previous_results, cli_vars, run_dir, attempt)
    (step_dir / "prompt.txt").write_text(rendered_prompt, encoding="utf-8")
    (step_dir / "schema.json").write_text("{}", encoding="utf-8")

    child_attempts = {
        child_id: _reserve_attempt(workflow.agents_by_id[child_id], step_attempts)
        for child_id in agent.parallel
    }
    child_results: list[StepResult] = []
    # Use a frozen copy of step_attempts for child threads to prevent concurrent
    # mutation.  Each child receives its pre-computed attempt number, so the copy
    # is only needed as a read-only fallback inside _run_step.
    frozen_attempts = dict(step_attempts)
    with ThreadPoolExecutor(max_workers=len(agent.parallel)) as executor:
        futures = [
            executor.submit(
                _run_step,
                workflow,
                workflow.agents_by_id[child_id],
                run_dir,
                previous_results,
                cli_vars,
                frozen_attempts,
                child_attempts[child_id],
            )
            for child_id in agent.parallel
        ]
        for future in futures:
            child_results.append(future.result())
    all_success = all(result.success for result in child_results)
    block_payload = {
        "success": all_success,
        "next": agent.join or TERMINAL_ROUTE,
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
    output_path = step_dir / (agent.output_file or "parallel.json")
    output_path.write_text(json.dumps(block_payload, indent=2), encoding="utf-8")
    stdout_path = step_dir / "stdout.log"
    stderr_path = step_dir / "stderr.log"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    block_result = StepResult(
        step_id=agent.id,
        attempt=attempt,
        success=all_success,
        next_route=agent.join or TERMINAL_ROUTE,
        output_path=output_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        payload=block_payload,
    )
    emitted_results = [*child_results, block_result]
    if all_success:
        return _resolve_parallel_success_route(agent), emitted_results, False
    failure_route = agent.on_failure or TERMINAL_ROUTE
    block_result.next_route = failure_route
    block_payload["next"] = failure_route
    output_path.write_text(json.dumps(block_payload, indent=2), encoding="utf-8")
    return failure_route, emitted_results, True


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _load_and_render_prompt(
    workflow: WorkflowBlueprint,
    agent: AgentBlueprint,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    run_dir: Path,
    attempt: int,
) -> str:
    if agent.prompt and agent.prompt_path:
        raise WorkflowError(f"agent '{agent.id}' cannot set both prompt and prompt_path")
    if agent.prompt_path:
        prompt = Path(_resolve_path(agent.prompt_path, Path(workflow.source_path).parent)).read_text(encoding="utf-8")
    elif agent.prompt:
        prompt = agent.prompt
    else:
        raise WorkflowError(f"agent '{agent.id}' must define prompt or prompt_path")

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
            "id": agent.id,
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
    try:
        return render_template(prompt, context)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


def _load_parallel_prompt(
    workflow: WorkflowBlueprint,
    agent: AgentBlueprint,
    previous_results: list[StepResult],
    cli_vars: dict[str, str],
    run_dir: Path,
    attempt: int,
) -> str:
    if agent.prompt or agent.prompt_path:
        return _load_and_render_prompt(
            workflow=workflow,
            agent=agent,
            previous_results=previous_results,
            cli_vars=cli_vars,
            run_dir=run_dir,
            attempt=attempt,
        )
    return f"Parallel block: {agent.id}. Join target: {agent.join or TERMINAL_ROUTE}. Children: {', '.join(agent.parallel)}"


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _resolve_success_route(agent: AgentBlueprint, next_route: str) -> str:
    if agent.branches:
        if next_route not in agent.branches and next_route != TERMINAL_ROUTE:
            raise WorkflowError(
                f"step '{agent.id}' returned next='{next_route}', which is not defined in branches"
            )
        return agent.branches.get(next_route, TERMINAL_ROUTE)
    if next_route and next_route != TERMINAL_ROUTE:
        return next_route
    return TERMINAL_ROUTE


def _resolve_parallel_success_route(agent: AgentBlueprint) -> str:
    if not agent.join:
        return TERMINAL_ROUTE
    return agent.join


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_agent(agent: AgentBlueprint) -> None:
    if agent.parallel:
        if agent.prompt and agent.prompt_path:
            raise WorkflowError(f"parallel agent '{agent.id}' cannot set both prompt and prompt_path")
        if agent.branches:
            raise WorkflowError(f"parallel agent '{agent.id}' cannot define branches")
    elif agent.join:
        raise WorkflowError(f"agent '{agent.id}' cannot define join without parallel")
    if agent.max_visits is not None and (not isinstance(agent.max_visits, int) or agent.max_visits < 1):
        raise WorkflowError(f"agent '{agent.id}' max_visits must be a positive integer")


def _validate_workflow(workflow: WorkflowBlueprint) -> None:
    if workflow.start_at not in workflow.agents_by_id:
        raise WorkflowError(f"workflow.start_at '{workflow.start_at}' is not a known agent")
    if workflow.max_steps < 1:
        raise WorkflowError("workflow.max_steps must be a positive integer")
    for agent in workflow.agents_by_id.values():
        _validate_agent(agent)
        targets = []
        if agent.on_failure:
            targets.append(agent.on_failure)
        targets.extend(agent.branches.values())
        if agent.join:
            targets.append(agent.join)
        for child in agent.parallel:
            if child not in workflow.agents_by_id:
                raise WorkflowError(f"parallel agent '{agent.id}' points to unknown child '{child}'")
        for target in targets:
            if target != TERMINAL_ROUTE and target not in workflow.agents_by_id:
                raise WorkflowError(f"agent '{agent.id}' points to unknown agent '{target}'")


def _validate_step_payload(step_id: str, payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise WorkflowError(f"step '{step_id}' output must be a JSON object")
    if "success" not in payload or "next" not in payload:
        raise WorkflowError(f"step '{step_id}' output must contain 'success' and 'next'")
    if not isinstance(payload["success"], bool):
        raise WorkflowError(f"step '{step_id}' output field 'success' must be boolean")
    if not isinstance(payload["next"], str):
        raise WorkflowError(f"step '{step_id}' output field 'next' must be string")


# ---------------------------------------------------------------------------
# Run directory / manifest
# ---------------------------------------------------------------------------

def _create_run_dir(workflow: WorkflowBlueprint) -> Path:
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
    workflow: WorkflowBlueprint,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _reserve_attempt(agent: AgentBlueprint, step_attempts: dict[str, int]) -> int:
    attempt = step_attempts.get(agent.id, 0) + 1
    if agent.max_visits is not None and attempt > agent.max_visits:
        raise WorkflowError(f"step '{agent.id}' exceeded max_visits={agent.max_visits}")
    step_attempts[agent.id] = attempt
    return attempt


def _make_step_dir(run_dir: Path, step_id: str, attempt: int) -> Path:
    suffix = "" if attempt == 1 else f"__{attempt:02d}"
    step_dir = run_dir / f"{step_id}{suffix}"
    step_dir.mkdir(parents=True, exist_ok=False)
    return step_dir
