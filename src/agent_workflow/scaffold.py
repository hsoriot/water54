from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_workflow.models import (
    ClaudeCodeConfig,
    CodexConfig,
    GenericConfig,
    ProviderConfig,
)


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
    prompt: str | None = None
    prompt_path: str | None = None
    memory_path: str | None = None
    schema_path: str | None = None
    max_visits: int | None = None
    on_failure: str | None = None
    parallel: list[str] = field(default_factory=list)
    join: str | None = None
    # --- computed at load time ---
    branches: dict[str, str] = field(default_factory=dict)
    schema: dict[str, Any] | None = None
    output_file: str = ""


@dataclass(slots=True)
class WorkflowBlueprint:
    name: str
    workdir: str
    template_type: str
    shared_files: list[SharedFile]
    agents: list[AgentBlueprint]
    start_at: str
    max_steps: int = 12
    run_root: str = ".runs"
    provider_raw: dict[str, Any] = field(default_factory=dict)
    # --- computed at load time ---
    source_path: str = ""
    provider: ProviderConfig = field(default_factory=CodexConfig)
    agents_by_id: dict[str, AgentBlueprint] = field(default_factory=dict)
    vars: dict[str, Any] = field(default_factory=dict)


def load_blueprint(path: str) -> WorkflowBlueprint:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return parse_blueprint(raw)


def parse_blueprint(raw: Any) -> WorkflowBlueprint:
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
                prompt=item.get("prompt"),
                prompt_path=item.get("prompt_path"),
                memory_path=item.get("memory_path"),
                schema_path=item.get("output_schema_path"),
                max_visits=item.get("max_visits"),
                on_failure=item.get("on_failure"),
                parallel=_ensure_string_list(item.get("parallel", []), f"agents.{agent_id}.parallel"),
                join=item.get("join"),
            )
        )

    workflow_raw = raw["workflow"]
    if not isinstance(workflow_raw, dict) or "start_at" not in workflow_raw:
        raise ScaffoldError("workflow must be a mapping with 'start_at'")

    provider_raw = raw.get("provider", raw.get("codex", {}))
    if not isinstance(provider_raw, dict):
        provider_raw = {}

    blueprint = WorkflowBlueprint(
        name=str(raw["name"]),
        workdir=str(raw["workdir"]),
        template_type=str(raw["template_type"]),
        shared_files=shared_files,
        agents=agents,
        start_at=str(workflow_raw["start_at"]),
        max_steps=int(workflow_raw.get("max_steps", 12)),
        run_root=str(workflow_raw.get("run_root", ".runs")),
        provider_raw=provider_raw,
    )
    _validate_blueprint(blueprint)
    return blueprint


def compile_blueprint(path: str) -> WorkflowBlueprint:
    """Load blueprint from *path* and enrich with runtime fields."""
    source_path = Path(path).resolve()
    base_dir = source_path.parent
    blueprint = load_blueprint(path)

    blueprint.source_path = str(source_path)
    blueprint.workdir = _resolve_path(blueprint.workdir, base_dir)
    blueprint.provider = _parse_provider_config(blueprint.provider_raw)

    # Build agents_by_id + computed per-agent fields.
    for agent in blueprint.agents:
        agent.output_file = f"{agent.id}.json"
        if agent.parallel:
            agent.on_failure = _compile_route(agent.on_failure)
            if agent.join and agent.join == "finish":
                agent.join = None  # parallel join "finish" → terminal
        else:
            agent.branches = {
                opt: ("__end__" if opt == "finish" else opt)
                for opt in agent.next_options
            }
            agent.on_failure = _compile_route(agent.on_failure)
            if not agent.prompt and not agent.prompt_path:
                agent.prompt_path = _agent_prompt_path(agent)
            agent.schema = _load_or_build_schema(agent, base_dir)
        blueprint.agents_by_id[agent.id] = agent

    # vars: only shared/memory path aliases for template rendering
    vars_map: dict[str, Any] = {}
    for shared in blueprint.shared_files:
        vars_map[f"shared_{shared.id}"] = shared.path
    for agent in blueprint.agents:
        if agent.uses_memory:
            vars_map[f"{agent.id}_memory"] = _agent_memory_path(agent)
    blueprint.vars = vars_map

    return blueprint


def _compile_route(value: str | None) -> str | None:
    if value is None:
        return None
    if value == "finish":
        return "__end__"
    return value


def _load_or_build_schema(agent: AgentBlueprint, base_dir: Path) -> dict[str, Any]:
    """Load schema from disk if file exists; otherwise generate a default."""
    if agent.schema_path:
        resolved = _resolve_path(agent.schema_path, base_dir)
        schema_file = Path(resolved)
        if schema_file.exists():
            try:
                return json.loads(schema_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ScaffoldError(
                    f"agent '{agent.id}' schema file '{agent.schema_path}' contains invalid JSON: {exc}"
                ) from exc
    return _build_default_schema(agent)


def _build_default_schema(agent: AgentBlueprint) -> dict[str, Any]:
    """Generate a minimal schema from next_options / branches."""
    enum_values = sorted(agent.branches) if agent.branches else None
    next_property: dict[str, Any] = {"type": "string"}
    if enum_values:
        next_property["enum"] = enum_values + ["__end__"]
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


def _parse_provider_config(raw: dict[str, Any]) -> ProviderConfig:
    if not raw:
        return CodexConfig()
    provider_type = str(raw.get("type", "codex"))

    if provider_type == "codex":
        return CodexConfig(
            bin=str(raw.get("bin", "codex")),
            model=raw.get("model"),
            approval=str(raw.get("approval", "never")),
            sandbox=str(raw.get("sandbox", "danger-full-access")),
            skip_git_repo_check=bool(raw.get("skip_git_repo_check", True)),
            extra_args=_ensure_string_list(raw.get("extra_args", []), "provider.extra_args"),
        )
    if provider_type == "claude-code":
        return ClaudeCodeConfig(
            bin=str(raw.get("bin", "claude")),
            model=raw.get("model"),
            max_turns=raw.get("max_turns"),
            extra_args=_ensure_string_list(raw.get("extra_args", []), "provider.extra_args"),
        )
    if provider_type == "generic":
        return GenericConfig(
            command_template=str(raw.get("command_template", "")),
            output_mode=str(raw.get("output_mode", "file")),
            extra_args=_ensure_string_list(raw.get("extra_args", []), "provider.extra_args"),
        )
    raise ScaffoldError(f"unknown provider type: '{provider_type}' (expected codex, claude-code, or generic)")


def scaffold_blueprint(blueprint: WorkflowBlueprint, destination: str) -> Path:
    root = Path(destination).resolve()
    if root.exists() and any(root.iterdir()):
        raise ScaffoldError(f"destination '{root}' already exists and is not empty")
    root.mkdir(parents=True, exist_ok=True)

    _ensure_dir(root / "prompts")
    _ensure_dir(root / "schemas")
    _ensure_dir(root / "memory")
    _ensure_dir(root / "shared")

    for shared in blueprint.shared_files:
        path = root / shared.path
        _ensure_dir(path.parent)
        path.write_text(_build_shared_file(shared), encoding="utf-8")

    workflow_yaml = _build_blueprint_yaml(blueprint)
    (root / "workflow.yaml").write_text(workflow_yaml, encoding="utf-8")

    for agent in blueprint.agents:
        if agent.parallel:
            continue
        # Compute branches so _build_default_schema can generate correct enum
        # values.  This mirrors the logic in compile_blueprint.
        if not agent.branches:
            agent.branches = {
                opt: ("__end__" if opt == "finish" else opt)
                for opt in agent.next_options
            }
        prompt_path = root / _agent_prompt_path(agent)
        schema_path = root / _agent_schema_path(agent)
        _ensure_dir(prompt_path.parent)
        _ensure_dir(schema_path.parent)
        prompt_content = agent.prompt or _build_prompt(blueprint, agent)
        prompt_path.write_text(prompt_content, encoding="utf-8")
        schema_path.write_text(json.dumps(_build_default_schema(agent), indent=2), encoding="utf-8")
        if agent.uses_memory:
            memory_path = root / _agent_memory_path(agent)
            _ensure_dir(memory_path.parent)
            memory_path.write_text(_build_memory(agent), encoding="utf-8")

    return root


def _build_shared_file(shared: SharedFile) -> str:
    title = shared.id.replace("-", " ").title()
    purpose = shared.purpose or "shared coordination state"
    return f"""# {title}

用途：{purpose}

## 待处理

## 已完成

## 已拒绝

## 条目模板

### ITEM-001
- 状态：待处理 | 已完成 | 已拒绝
- 发起人：
- 需求：
- 原因：
- 期望产出：
- 备注：
"""


def _build_blueprint_yaml(blueprint: WorkflowBlueprint) -> str:
    payload: dict[str, Any] = {
        "name": blueprint.name,
        "template_type": blueprint.template_type,
        "workdir": blueprint.workdir,
        "shared": {
            "files": [
                {
                    "id": shared.id,
                    "path": shared.path,
                    "purpose": shared.purpose,
                }
                for shared in blueprint.shared_files
            ]
        },
        "agents": [
            _build_agent_yaml(agent)
            for agent in blueprint.agents
        ],
        "workflow": {
            "start_at": blueprint.start_at,
            "max_steps": blueprint.max_steps,
            "run_root": blueprint.run_root,
        },
    }
    if blueprint.provider_raw:
        payload["provider"] = blueprint.provider_raw
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _build_agent_yaml(agent: AgentBlueprint) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": agent.id,
        "role": agent.role,
    }
    if agent.parallel:
        data["parallel"] = agent.parallel
        if agent.join:
            data["join"] = agent.join
    else:
        data["uses_memory"] = agent.uses_memory
        data["uses_shared"] = agent.uses_shared
        data["next_options"] = agent.next_options
        if agent.prompt:
            data["prompt"] = agent.prompt
        else:
            data["prompt_path"] = _agent_prompt_path(agent)
        data["memory_path"] = _agent_memory_path(agent) if agent.uses_memory else None
        data["output_schema_path"] = _agent_schema_path(agent)
    if agent.max_visits is not None:
        data["max_visits"] = agent.max_visits
    if agent.on_failure is not None:
        data["on_failure"] = agent.on_failure
    return data


def _build_prompt(blueprint: WorkflowBlueprint, agent: AgentBlueprint) -> str:
    NL = "\n"
    # --- 必读文件 ---
    read_lines = []
    memory_path = _agent_memory_path(agent) if agent.uses_memory else None
    if memory_path:
        read_lines.append(f"- `{memory_path}` — 你的长期记忆")

    shared_paths: list[tuple[str, str]] = []
    for shared_id in agent.uses_shared:
        shared = next((s for s in blueprint.shared_files if s.id == shared_id), None)
        if shared:
            shared_paths.append((shared.path, shared.purpose or "共享状态"))
            read_lines.append(f"- `{shared.path}` — {shared.purpose or '共享状态'}")

    read_lines.append("- 上一次运行的输出（如果存在）")

    # --- 路由规则 ---
    route_lines = []
    for option in agent.next_options:
        if option == "finish":
            route_lines.append('- 返回 `next: "finish"` — 结束整个流程')
        else:
            target_agent = next((a for a in blueprint.agents if a.id == option), None)
            target_desc = target_agent.role if target_agent and target_agent.role else option
            route_lines.append(f'- 返回 `next: "{option}"` — 交给 {target_desc}')

    # --- 写操作说明 ---
    write_lines = []
    if memory_path:
        write_lines.append(f"""### 更新记忆文件

当你获得新的结论、发现风险或改变判断时，把变更写入 `{memory_path}`。
保留文件原有的 markdown 结构，只更新对应小节的内容。不要删除历史条目，追加在对应小节末尾。""")

    if shared_paths:
        for spath, spurpose in shared_paths:
            write_lines.append(f"""### 更新共享文件

当协作状态发生变化时（如新增请求、关闭条目、交接备注），更新 `{spath}`。
读取文件现有内容，在对应小节下追加或修改条目，保留其他 agent 写入的内容不变。""")

    write_section = ""
    if write_lines:
        write_section = "\n\n".join(write_lines)

    example_next = agent.next_options[0] if agent.next_options else "__end__"

    return f"""你是 `{agent.id}`。

## 角色

{agent.role or agent.id}

## 必读文件

执行任何操作之前，先完整读取以下文件：

{NL.join(read_lines)}

## 工作流程

1. 读取上面列出的所有必读文件，总结当前状态。
2. 根据角色职责完成本步骤的工作。
{f"3. 按需更新记忆和共享文件（见下方说明）。" if write_lines else ""}
{"4" if write_lines else "3"}. 决定下一步路由并输出 JSON。

{write_section}

## 路由规则

{NL.join(route_lines)}

## 输出要求

返回且仅返回一个符合 schema 的 JSON 对象，例如：

```json
{{"success": true, "next": "{example_next}"}}
```
"""


def _build_memory(agent: AgentBlueprint) -> str:
    title = agent.id.replace("-", " ").title()
    return f"""# {title} 记忆

## 当前目标
-

## 已知信息
-

## 近期变更
-

## 风险
-

## 下一步关注
-
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
        if agent.prompt and agent.prompt_path:
            raise ScaffoldError(f"agent '{agent.id}' cannot set both prompt and prompt_path")
        if agent.parallel:
            if agent.next_options:
                raise ScaffoldError(f"parallel agent '{agent.id}' cannot define next_options; use join")
            if not agent.join:
                raise ScaffoldError(f"parallel agent '{agent.id}' must define join")
            for child in agent.parallel:
                if child not in agent_ids:
                    raise ScaffoldError(f"parallel agent '{agent.id}' references unknown child '{child}'")
            if agent.join != "finish" and agent.join not in agent_ids:
                raise ScaffoldError(f"parallel agent '{agent.id}' join target '{agent.join}' is not a known agent")
        else:
            if not agent.next_options:
                raise ScaffoldError(f"agent '{agent.id}' must define at least one next option")
            for option in agent.next_options:
                if option != "finish" and option not in agent_ids:
                    raise ScaffoldError(
                        f"agent '{agent.id}' next option '{option}' must be another agent id or 'finish'"
                    )
        if agent.max_visits is not None and (not isinstance(agent.max_visits, int) or agent.max_visits < 1):
            raise ScaffoldError(f"agent '{agent.id}' max_visits must be a positive integer")
        if agent.on_failure is not None:
            if agent.on_failure != "finish" and agent.on_failure not in agent_ids:
                raise ScaffoldError(f"agent '{agent.id}' on_failure '{agent.on_failure}' is not a known agent or 'finish'")
    non_parallel = [a for a in blueprint.agents if not a.parallel]
    if blueprint.template_type == "single-agent" and len(non_parallel) != 1:
        raise ScaffoldError("single-agent template_type requires exactly one non-parallel agent")


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


def _resolve_path(value: str, base_dir: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())
