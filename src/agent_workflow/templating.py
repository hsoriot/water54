from __future__ import annotations

import re
from typing import Any


_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")


def render_template(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        expression = match.group(1)
        value = _resolve(expression, context)
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            raise ValueError(f"template expression '{expression}' resolved to a non-scalar value")
        return str(value)

    return _PATTERN.sub(replace, template)


def _resolve(expression: str, context: dict[str, Any]) -> Any:
    current: Any = context
    for part in expression.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return None
    return current
