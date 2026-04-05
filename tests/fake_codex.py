#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    args = sys.argv[1:]
    output_file = None
    prompt = sys.stdin.read()

    idx = 0
    while idx < len(args):
        if args[idx] == "-o":
            output_file = args[idx + 1]
            idx += 2
            continue
        idx += 1

    if output_file is None:
        raise SystemExit("missing -o")

    if "branch=fix" in prompt:
        payload = {"success": True, "next": "fix", "source": "fake"}
    elif "branch=finish" in prompt:
        payload = {"success": True, "next": "finish", "source": "fake"}
    elif "loop-attempt=1" in prompt:
        payload = {"success": True, "next": "retry", "source": "fake"}
    elif "loop-attempt=2" in prompt:
        payload = {"success": True, "next": "done", "source": "fake"}
    elif "parallel-a" in prompt:
        payload = {"success": True, "next": "__end__", "worker": "a"}
    elif "parallel-b" in prompt:
        payload = {"success": True, "next": "__end__", "worker": "b"}
    elif "merge-a=a,b=b" in prompt:
        payload = {"success": True, "next": "__end__", "merged": True}
    elif "force-failure" in prompt:
        payload = {"success": False, "next": "__end__", "source": "fake"}
    elif "render-previous=fix" in prompt:
        payload = {"success": True, "next": "__end__", "saw_previous": "fix"}
    else:
        payload = {"success": True, "next": "__end__", "source": "fake"}

    Path(output_file).write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
