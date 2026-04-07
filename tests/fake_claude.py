#!/usr/bin/env python3
"""Fake Claude Code CLI for testing.

Reads a prompt from stdin, ignores most flags, and writes a JSON envelope
to stdout in the format Claude Code uses with ``--output-format json``:

    {"type": "result", "result": "<json_string>"}

Uses the same prompt-matching logic as ``fake_codex.py``.
"""

from __future__ import annotations

import json
import re
import sys


def main() -> None:
    prompt = sys.stdin.read()
    payload = _decide(prompt)
    envelope = {"type": "result", "result": json.dumps(payload)}
    print(json.dumps(envelope))


def _decide(prompt: str) -> dict:
    branch_match = re.search(r"branch=(\S+)", prompt)
    if branch_match:
        return {"success": True, "next": branch_match.group(1), "source": "fake-claude"}

    render_match = re.search(r"render-previous=(\S+)", prompt)
    if render_match:
        return {"success": True, "next": "__end__", "saw_previous": render_match.group(1)}

    if "loop-attempt=1" in prompt:
        return {"success": True, "next": "review", "source": "fake-claude"}
    if "loop-attempt=2" in prompt:
        return {"success": True, "next": "done", "source": "fake-claude"}

    if "parallel-a" in prompt:
        return {"success": True, "next": "__end__", "worker": "a"}
    if "parallel-b" in prompt:
        return {"success": True, "next": "__end__", "worker": "b"}

    merge_match = re.search(r"merge-a=(\S+),b=(\S+)", prompt)
    if merge_match:
        return {"success": True, "next": "__end__", "merged": True}

    if "force-failure" in prompt:
        return {"success": False, "next": "__end__", "source": "fake-claude"}

    return {"success": True, "next": "__end__", "source": "fake-claude"}


if __name__ == "__main__":
    main()
