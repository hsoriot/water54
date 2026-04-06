from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ScaffoldError(RuntimeError):
    pass


@dataclass(slots=True)
class SharedFile:
    id: str
    path: str
    purpose: str = ""


@dataclass(slots=True)
class AgentBlueprint:
    id: str
    role: str | None = None
    uses_memory: bool = True
    uses_shared: list[str] = field(default_factory=list)
    next_options: list[str] = field(default_factory=list)
    prompt_path: str | None = None
    memory_path: str | None = None
    schema_path: str | None = None


@dataclass(slots=True)
class WorkflowBlueprint:
    name: str
    workdir: str
    template_type: str
    control_enabled: bool
    shared_files: list[SharedFile]
    agents: list[AgentBlueprint]
    start_at: str
    max_steps: int = 12
    run_root: str = ".runs"


def load_blueprint(path: str) -> WorkflowBlueprint:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ScaffoldError("blueprint file must be a YAML object")

    for key in ["name", "template_type", "workdir", "agents", "workflow"]:
        if key not in raw:
            raise ScaffoldError(f"blueprint missing required field: {key}")

    shared_raw = raw.get("shared", {}).get("files", [])
    shared_files: list[SharedFile] = []
    for item in shared_raw:
        if not isinstance(item, dict):
            raise ScaffoldError("shared.files entries must be mappings")
        if "id" not in item or "path" not in item:
            raise ScaffoldError("shared file entries require 'id' and 'path'")
        shared_files.append(
            SharedFile(
                id=str(item["id"]),
                path=str(item["path"]),
                purpose=str(item.get("purpose", "")),
            )
        )

    agents_raw = raw["agents"]
    if not isinstance(agents_raw, list) or not agents_raw:
        raise ScaffoldError("agents must be a non-empty list")
    agents: list[AgentBlueprint] = []
    for item in agents_raw:
        if not isinstance(item, dict) or "id" not in item:
            raise ScaffoldError("each agent must be a mapping with an 'id'")
        agent_id = str(item["id"])
        agents.append(
            AgentBlueprint(
                id=agent_id,
                role=str(item.get("role", agent_id)),
                uses_memory=bool(item.get("uses_memory", True)),
                uses_shared=_ensure_string_list(item.get("uses_shared", []), f"agents.{agent_id}.uses_shared"),
                next_options=_ensure_string_list(item.get("next_options", []), f"agents.{agent_id}.next_options"),
                prompt_path=item.get("prompt_path"),
                memory_path=item.get("memory_path"),
                schema_path=item.get("output_schema_path"),
            )
        )

    workflow_raw = raw["workflow"]
    if not isinstance(workflow_raw, dict) or "start_at" not in workflow_raw:
        raise ScaffoldError("workflow must be a mapping with 'start_at'")

    blueprint = WorkflowBlueprint(
        name=str(raw["name"]),
        workdir=str(raw["workdir"]),
        template_type=str(raw["template_type"]),
        control_enabled=bool(raw.get("control", {}).get("enabled", True)),
        shared_files=shared_files,
        agents=agents,
        start_at=str(workflow_raw["start_at"]),
        max_steps=int(workflow_raw.get("max_steps", 12)),
        run_root=str(workflow_raw.get("run_root", ".runs")),
    )
    _validate_blueprint(blueprint)
    return blueprint


def scaffold_blueprint(blueprint: WorkflowBlueprint, destination: str) -> Path:
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise ScaffoldError(f"destination '{root}' already exists and is not empty")
    root.mkdir(parents=True, exist_ok=True)

    _ensure_dir(root / "prompts")
    _ensure_dir(root / "schemas")
    _ensure_dir(root / "memory")
    _ensure_dir(root / "shared")
    _ensure_dir(root / "examples")

    (root / "README.md").write_text(_build_readme(blueprint), encoding="utf-8")
    if blueprint.control_enabled:
        (root / "control.yaml").write_text(_build_control_yaml(), encoding="utf-8")

    for shared in blueprint.shared_files:
        path = root / shared.path
        _ensure_dir(path.parent)
        path.write_text(_build_shared_file(shared), encoding="utf-8")

    workflow_yaml = _build_workflow_yaml(blueprint)
    (root / "workflow.yaml").write_text(workflow_yaml, encoding="utf-8")

    for agent in blueprint.agents:
        prompt_path = root / _agent_prompt_path(agent)
        schema_path = root / _agent_schema_path(agent)
        _ensure_dir(prompt_path.parent)
        _ensure_dir(schema_path.parent)
        prompt_path.write_text(_build_prompt(blueprint, agent), encoding="utf-8")
        schema_path.write_text(json.dumps(_build_schema(blueprint, agent), indent=2), encoding="utf-8")
        if agent.uses_memory:
            memory_path = root / _agent_memory_path(agent)
            _ensure_dir(memory_path.parent)
            memory_path.write_text(_build_memory(agent), encoding="utf-8")

    (root / "examples" / "sample-run-notes.md").write_text(_build_sample_run_notes(blueprint), encoding="utf-8")
    return root


def _build_readme(blueprint: WorkflowBlueprint) -> str:
    agent_ids = ", ".join(agent.id for agent in blueprint.agents)
    return f"""# {blueprint.name}

This workflow package was generated from a blueprint.

## Agents

- {agent_ids}

## How To Customize

1. Update `workflow.yaml`
2. Update files in `prompts/`
3. Update files in `schemas/`
4. Update files in `memory/`
5. Update files in `shared/`
6. Optionally update `control.yaml`

## How To Run

```bash
/Users/riot/riot/codex-workflow-automation/.venv/bin/codex-workflow run {rooted_path('workflow.yaml')}
```

If you moved this directory elsewhere, replace the path accordingly.
"""


def _build_control_yaml() -> str:
    return """paused: false
pause_after_step: ""
force_next: ""
review_required: false
human_note: ""
disable_steps: []
"""


def _build_shared_file(shared: SharedFile) -> str:
    title = shared.id.replace("-", " ").title()
    purpose = shared.purpose or "shared coordination state"
    return f"""# {title}

Purpose: {purpose}

## Open

## Closed

## Rejected

## Entry Template

### ITEM-001
- Status: open | closed | rejected
- Requester:
- Need:
- Why:
- Expected output:
- Notes:
"""


def _build_workflow_yaml(blueprint: WorkflowBlueprint) -> str:
    vars_map: dict[str, Any] = {"terminal": "inline"}
    if blueprint.control_enabled:
        vars_map["control_file"] = "control.yaml"
    for shared in blueprint.shared_files:
        vars_map[f"shared_{shared.id}"] = shared.path
    for agent in blueprint.agents:
        if agent.uses_memory:
            vars_map[f"{agent.id}_memory"] = _agent_memory_path(agent)

    steps: dict[str, Any] = {}
    for agent in blueprint.agents:
        branches = {option: ("__end__" if option == "finish" else option) for option in agent.next_options}
        steps[agent.id] = {
            "prompt_file": _agent_prompt_path(agent),
            "output_file": f"{agent.id}.json",
            "schema": _build_schema(blueprint, agent),
            "branches": branches,
        }

    payload = {
        "name": blueprint.name,
        "workdir": blueprint.workdir,
        "run_root": blueprint.run_root,
        "start_at": blueprint.start_at,
        "max_steps": blueprint.max_steps,
        "codex": {
            "bin": "codex",
            "approval": "never",
            "sandbox": "danger-full-access",
            "skip_git_repo_check": True,
        },
        "vars": vars_map,
        "steps": steps,
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _build_prompt(blueprint: WorkflowBlueprint, agent: AgentBlueprint) -> str:
    read_lines = []
    if agent.uses_memory:
        read_lines.append(f"- `{{{{ vars.{agent.id}_memory }}}}`")
    for shared_id in agent.uses_shared:
        read_lines.append(f"- `{{{{ vars.shared_{shared_id} }}}}`")
    if blueprint.control_enabled:
        read_lines.append("- `{{ vars.control_file }}`")
    read_lines.append("- recent successful run, if it exists")
    read_lines.append(
        "- prior orchestrator outputs from other steps, if they exist"
    )

    route_lines = []
    for option in agent.next_options:
        route_lines.append(f"- return `next=\"{option}\"` when that path is appropriate")

    memory_line = ""
    if agent.uses_memory:
        memory_line = f"- update `{{{{ vars.{agent.id}_memory }}}}` when long-term state changes\n"

    shared_line = ""
    if agent.uses_shared:
        shared_vars = ", ".join(f"`{{{{ vars.shared_{shared_id} }}}}`" for shared_id in agent.uses_shared)
        shared_line = f"- update shared file(s) {shared_vars} when coordination state changes\n"

    return f"""You are `{agent.id}`.

## Read First
{chr(10).join(read_lines)}

## Role
- {agent.role or agent.id}

## Fixed Workflow
1. Summarize what you learned from the required inputs.
2. Do the work for this step.
{memory_line}{shared_line}3. Decide the next route.

## Routing Rules
{chr(10).join(route_lines)}

## Output Requirement
Return exactly one JSON object that matches the schema.
"""


def _build_schema(blueprint: WorkflowBlueprint, agent: AgentBlueprint) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["success", "next"],
        "properties": {
            "success": {"type": "boolean"},
            "next": {
                "type": "string",
                "enum": [*agent.next_options, "__end__"],
            },
        },
        "additionalProperties": False,
    }


def _build_memory(agent: AgentBlueprint) -> str:
    title = agent.id.replace("-", " ").title()
    return f"""# {title} Memory

## Current Goal
- 

## What We Know
- 

## Recent Changes
- 

## Risks
- 

## Next Focus
- 
"""


def _build_sample_run_notes(blueprint: WorkflowBlueprint) -> str:
    first = blueprint.agents[0].id
    return f"""# Sample Run Notes

After a typical run, expect:

- `.runs/<timestamp>-{blueprint.name}/run_manifest.json`
- `.runs/<timestamp>-{blueprint.name}/{first}/prompt.txt`
- `.runs/<timestamp>-{blueprint.name}/{first}/output.json`
"""


def _validate_blueprint(blueprint: WorkflowBlueprint) -> None:
    if blueprint.template_type not in {"single-agent", "multi-agent"}:
        raise ScaffoldError("template_type must be 'single-agent' or 'multi-agent'")
    if blueprint.max_steps < 1:
        raise ScaffoldError("workflow.max_steps must be a positive integer")
    agent_ids = {agent.id for agent in blueprint.agents}
    if blueprint.start_at not in agent_ids:
        raise ScaffoldError("workflow.start_at must refer to an agent id")
    shared_ids = {item.id for item in blueprint.shared_files}
    for agent in blueprint.agents:
        for shared_id in agent.uses_shared:
            if shared_id not in shared_ids:
                raise ScaffoldError(f"agent '{agent.id}' references unknown shared id '{shared_id}'")
        if not agent.next_options:
            raise ScaffoldError(f"agent '{agent.id}' must define at least one next option")
        for option in agent.next_options:
            if option != "finish" and option not in agent_ids:
                raise ScaffoldError(
                    f"agent '{agent.id}' next option '{option}' must be another agent id or 'finish'"
                )
    if blueprint.template_type == "single-agent" and len(blueprint.agents) != 1:
        raise ScaffoldError("single-agent template_type requires exactly one agent")


def _ensure_string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ScaffoldError(f"{field_name} must be a list of strings")
    return list(value)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _agent_prompt_path(agent: AgentBlueprint) -> str:
    return agent.prompt_path or f"prompts/{agent.id}.md"


def _agent_memory_path(agent: AgentBlueprint) -> str:
    return agent.memory_path or f"memory/{agent.id}.md"


def _agent_schema_path(agent: AgentBlueprint) -> str:
    return agent.schema_path or f"schemas/{agent.id}-output.json"


def rooted_path(name: str) -> str:
    return f"/abs/path/to/{name}"
