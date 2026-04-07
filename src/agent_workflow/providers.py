from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_workflow.models import (
    ClaudeCodeConfig,
    CodexConfig,
    GenericConfig,
    ProviderConfig,
)


@dataclass(slots=True)
class ProviderResult:
    returncode: int
    stdout: str
    stderr: str
    payload: dict[str, Any]

    def __repr__(self) -> str:
        stdout_preview = self.stdout[:200] + "..." if len(self.stdout) > 200 else self.stdout
        stderr_preview = self.stderr[:200] + "..." if len(self.stderr) > 200 else self.stderr
        return (
            f"ProviderResult(returncode={self.returncode!r}, "
            f"stdout={stdout_preview!r}, stderr={stderr_preview!r}, "
            f"payload={self.payload!r})"
        )


class ProviderError(RuntimeError):
    pass


def run_provider(
    *,
    config: ProviderConfig,
    prompt: str,
    workdir: str,
    schema_path: Path,
    output_path: Path,
) -> ProviderResult:
    if isinstance(config, CodexConfig):
        return _run_codex(
            config=config,
            prompt=prompt,
            workdir=workdir,
            schema_path=schema_path,
            output_path=output_path,
        )
    if isinstance(config, ClaudeCodeConfig):
        return _run_claude_code(
            config=config,
            prompt=prompt,
            workdir=workdir,
            schema_path=schema_path,
            output_path=output_path,
        )
    if isinstance(config, GenericConfig):
        return _run_generic(
            config=config,
            prompt=prompt,
            workdir=workdir,
            schema_path=schema_path,
            output_path=output_path,
        )
    raise ProviderError(f"unsupported provider config type: {type(config).__name__}")


def _run_codex(
    *,
    config: CodexConfig,
    prompt: str,
    workdir: str,
    schema_path: Path,
    output_path: Path,
) -> ProviderResult:
    cmd = _build_codex_command(
        codex=config,
        workdir=workdir,
        schema_path=schema_path,
        output_path=output_path,
    )
    completed = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    payload: dict[str, Any] = {}
    if completed.returncode == 0 and output_path.exists():
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"codex output is not valid JSON: {exc}") from exc
    return ProviderResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        payload=payload,
    )


def _build_codex_command(
    *,
    codex: CodexConfig,
    workdir: str,
    schema_path: Path,
    output_path: Path,
) -> list[str]:
    command = [codex.bin]
    command.extend(codex.extra_args)
    if codex.model:
        command.extend(["-m", codex.model])
    command.extend(["-a", codex.approval, "exec"])
    if codex.skip_git_repo_check:
        command.append("--skip-git-repo-check")
    command.extend([
        "--sandbox", codex.sandbox,
        "-C", workdir,
        "--output-schema", str(schema_path),
        "-o", str(output_path),
    ])
    command.append("-")
    return command


def _run_claude_code(
    *,
    config: ClaudeCodeConfig,
    prompt: str,
    workdir: str,
    schema_path: Path,
    output_path: Path,
) -> ProviderResult:
    schema_json = schema_path.read_text(encoding="utf-8")
    full_prompt = (
        f"{prompt}\n\n"
        f"You MUST respond with exactly one JSON object matching this schema:\n"
        f"{schema_json}\n"
    )

    cmd = [config.bin, "-p", "--output-format", "json"]
    cmd.extend(config.extra_args)
    if config.model:
        cmd.extend(["--model", config.model])
    if config.max_turns is not None:
        cmd.extend(["--max-turns", str(config.max_turns)])

    completed = subprocess.run(
        cmd,
        input=full_prompt,
        text=True,
        capture_output=True,
        check=False,
        cwd=workdir,
    )

    payload: dict[str, Any] = {}
    if completed.returncode == 0 and completed.stdout.strip():
        payload = _parse_claude_code_output(completed.stdout)
        output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return ProviderResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        payload=payload,
    )


def _parse_claude_code_output(stdout: str) -> dict[str, Any]:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"claude-code stdout is not valid JSON: {exc}") from exc
    if isinstance(envelope, dict) and envelope.get("type") == "result":
        result_value = envelope.get("result", "")
        if isinstance(result_value, str):
            try:
                return json.loads(result_value)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"claude-code result field is not valid JSON: {exc}") from exc
        if isinstance(result_value, dict):
            return result_value
    if isinstance(envelope, dict):
        return envelope
    raise ProviderError(
        f"claude-code output must be a JSON object, got {type(envelope).__name__}; "
        f"raw output starts with: {stdout[:200]!r}"
    )


def _run_generic(
    *,
    config: GenericConfig,
    prompt: str,
    workdir: str,
    schema_path: Path,
    output_path: Path,
) -> ProviderResult:
    if not config.command_template:
        raise ProviderError("generic provider requires a non-empty command_template")

    cmd_str = (
        config.command_template
        .replace("{prompt_file}", str(schema_path.parent / "prompt.txt"))
        .replace("{schema_file}", str(schema_path))
        .replace("{output_file}", str(output_path))
        .replace("{workdir}", workdir)
    )
    cmd = shlex.split(cmd_str)
    cmd.extend(config.extra_args)

    completed = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
        cwd=workdir,
    )

    payload: dict[str, Any] = {}
    if completed.returncode == 0:
        if config.output_mode == "stdout":
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise ProviderError(f"generic provider stdout is not valid JSON: {exc}") from exc
            output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        else:
            if output_path.exists():
                try:
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"generic provider output file is not valid JSON: {exc}") from exc

    return ProviderResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        payload=payload,
    )
