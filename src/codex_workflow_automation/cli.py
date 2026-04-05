from __future__ import annotations

import argparse
import json
import sys

from codex_workflow_automation.engine import WorkflowError, load_workflow, run_workflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a YAML-defined Codex workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a workflow file")
    run_parser.add_argument("workflow_file")
    run_parser.add_argument(
        "--var",
        action="append",
        default=[],
        help="Override workflow vars as key=value pairs",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run":
        _run_command(args.workflow_file, args.var)


def _run_command(workflow_file: str, raw_vars: list[str]) -> None:
    try:
        workflow = load_workflow(workflow_file)
        result = run_workflow(workflow, cli_vars=_parse_vars(raw_vars))
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(
        json.dumps(
            {
                "workflow_name": result.workflow_name,
                "status": result.status,
                "run_dir": str(result.run_dir),
                "steps": [
                    {
                        "step_id": step.step_id,
                        "success": step.success,
                        "next": step.next_route,
                        "output_path": str(step.output_path),
                    }
                    for step in result.step_results
                ],
            },
            indent=2,
        )
    )


def _parse_vars(raw_vars: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw_vars:
        if "=" not in item:
            raise WorkflowError(f"invalid --var '{item}', expected key=value")
        key, value = item.split("=", 1)
        parsed[key] = value
    return parsed
