from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union


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
class ClaudeCodeConfig:
    bin: str = "claude"
    model: str | None = None
    max_turns: int | None = None
    extra_args: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GenericConfig:
    command_template: str = ""
    output_mode: str = "file"  # "file" or "stdout"
    extra_args: list[str] = field(default_factory=list)


ProviderConfig = Union[CodexConfig, ClaudeCodeConfig, GenericConfig]


@dataclass(slots=True)
class StepResult:
    step_id: str
    attempt: int
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
