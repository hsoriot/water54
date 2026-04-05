from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TERMINAL_ROUTE = "__end__"


@dataclass(slots=True)
class CodexConfig:
    bin: str = "codex"
    model: str | None = None
    approval: str = "never"
    sandbox: str = "danger-full-access"
    skip_git_repo_check: bool = True
    extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WorkflowConfig:
    name: str
    start_at: str
    workdir: str
    run_root: str = ".runs"
    vars: dict[str, Any] = field(default_factory=dict)
    codex: CodexConfig = field(default_factory=CodexConfig)
    steps: dict[str, "StepConfig"] = field(default_factory=dict)


@dataclass(slots=True)
class StepConfig:
    id: str
    prompt: str | None = None
    prompt_file: str | None = None
    output_file: str | None = None
    schema: dict[str, Any] | None = None
    branches: dict[str, str] = field(default_factory=dict)
    on_success: str | None = None
    on_failure: str | None = None
    model: str | None = None
    workdir: str | None = None
    codex_extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StepResult:
    step_id: str
    success: bool
    next_route: str
    output_path: Path
    stdout_path: Path
    stderr_path: Path
    payload: dict[str, Any]


@dataclass(slots=True)
class RunResult:
    run_dir: Path
    workflow_name: str
    status: str
    step_results: list[StepResult]

