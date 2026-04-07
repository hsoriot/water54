# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

YAML-driven orchestrator for multi-provider agent workflows. A single blueprint YAML defines multi-agent coordination with branching, looping, parallel execution, and provider selection. State is persisted to the filesystem (memory, shared files, run artifacts), not chat history.

Supports multiple CLI providers: **Codex**, **Claude Code**, and **generic** CLI tools.

Two CLI commands: `agent-workflow init` (generate template package from blueprint) and `agent-workflow run` (execute a workflow).

## Build & Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Requires Python 3.11+. Only runtime dependency is `PyYAML>=6.0`.

## Running Tests

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Run a single test:
```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_engine.WorkflowEngineTests.test_branching_workflow
```

Tests use stdlib `unittest` with mock CLIs:
- `tests/fake_codex.py` — simulates Codex CLI responses based on prompt content
- `tests/fake_claude.py` — simulates Claude Code CLI with JSON envelope output

## Architecture

**src-layout** under `src/agent_workflow/`:

- **`models.py`** — Dataclasses: `StepResult`, `RunResult`, `CodexConfig`, `ClaudeCodeConfig`, `GenericConfig`. The `ProviderConfig` type alias is `CodexConfig | ClaudeCodeConfig | GenericConfig`. The terminal route sentinel is `TERMINAL_ROUTE = "__end__"`.
- **`providers.py`** — Provider dispatch: `run_provider()` uses isinstance dispatch to route to `_run_codex()`, `_run_claude_code()`, or `_run_generic()`. Each provider writes output to `output_path` for consistency. Returns `ProviderResult(returncode, stdout, stderr, payload)`. All `json.loads` calls wrapped in try/except with `ProviderError`.
- **`engine.py`** — Core execution: `load_workflow()` delegates to `compile_blueprint()`, then validates. `run_workflow()` executes step-by-step with cursor-based resume. Parallel steps use `ThreadPoolExecutor`. All `json.loads` calls wrapped in try/except with `WorkflowError`.
- **`scaffold.py`** — Blueprint system: `WorkflowBlueprint` and `AgentBlueprint` are the primary config types. `load_blueprint()` / `parse_blueprint()` parse the blueprint format. `compile_blueprint()` enriches the blueprint with computed runtime fields (provider config, schema loading, branches, agents_by_id). `scaffold_blueprint()` generates a template package (prompts/, schemas/, memory/, shared/). `_load_or_build_schema()` reads schema from disk if file exists, otherwise generates a default. `_parse_provider_config()` handles provider type dispatch.
- **`templating.py`** — Minimal `{{ expression }}` template renderer with dot-notation path resolution against a context dict.
- **`cli.py`** — argparse CLI with `run` and `init` subcommands. Entry point registered as `agent-workflow` in pyproject.toml.

**Single config type**: The project uses one layer of config — `WorkflowBlueprint` / `AgentBlueprint` from `scaffold.py`. `compile_blueprint()` enriches these with runtime fields (`provider`, `agents_by_id`, `branches`, `schema`, `output_file`, `vars`). The engine uses these directly.

**Execution flow**: `load_workflow` → `compile_blueprint` → validate → check cursor for resume → create/reuse timestamped run dir (`.runs/<timestamp>-<name>/`) → loop from `start_at` (or cursor position) until `__end__` → each step writes prompt.txt, schema.json, output.json, stdout.log, stderr.log to its step dir → `run_manifest.json` updated after each step → cursor saved after each step → cursor deleted on completion.

**Cursor-based resume**: After each step, the engine writes `.cursor.yaml` next to the workflow YAML. If the workflow crashes or is interrupted, the next `run_workflow()` call resumes from where it stopped. The cursor records `run_dir`, `current_step`, `step_attempts`, `total_steps`, and `completed_steps`. Users can also edit `.cursor.yaml` to control the next step manually. The cursor is deleted when the workflow completes normally.

**Provider system** (from `provider:` key in blueprint YAML):
- `provider: { type: codex }` — uses Codex CLI with `--output-schema` and `-o` flags, stdin prompt
- `provider: { type: claude-code }` — uses Claude Code CLI with `--output-format json`, schema embedded in prompt, parses `{"type":"result","result":"..."}` envelope from stdout
- `provider: { type: generic }` — expands `command_template` with `{prompt_file}`, `{schema_file}`, `{output_file}`, `{workdir}` placeholders; supports `output_mode: file` or `stdout`

**Step routing protocol**: Every step must output `{"success": bool, "next": "step_id_or___end__"}`. Branching uses `next_options` (compiled to `branches` dict) to map symbolic `next` values to agent IDs. `"finish"` maps to `__end__`. Failure defaults to `on_failure` route or `__end__`.

**Loop control**: `max_steps` at workflow level, `max_visits` per agent. Revisited steps create suffixed dirs (`step__02/`, `step__03/`).

**Parallel execution**: Agent with `parallel: [child1, child2]` and `join: target` fans out children via ThreadPoolExecutor, all must succeed, then routes to `join` target.

**Schema loading**: `compile_blueprint()` calls `_load_or_build_schema()` per agent. If the agent defines `output_schema_path` and the file exists on disk, it is loaded. Otherwise a default schema is generated from `next_options`/`branches`. The engine validates `additionalProperties: false` at runtime.

## Key Conventions

- Schemas must set `additionalProperties: false` at root — enforced by validation.
- Relative paths in blueprint YAML (`workdir`, `prompt_path`) resolve from the YAML file's parent directory.
- The `--var key=value` CLI flag merges into `vars` namespace, accessible in templates as `{{ vars.key }}`.
- Agent `next_options` value `"finish"` maps to `__end__` during compilation.
- Agents can define `prompt` (inline text) or `prompt_path` (file reference), not both.
- Every provider writes its output to `output_path`, keeping the rest of the engine (manifest, step results) unchanged.
