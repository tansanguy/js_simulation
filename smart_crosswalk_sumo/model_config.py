from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_MODEL_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "model_assumptions.yaml"


def _coerce_scalar(text: str) -> Any:
    raw = text.strip()
    if raw in {"true", "True"}:
        return True
    if raw in {"false", "False"}:
        return False
    if raw in {"null", "None", "~"}:
        return None
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _strip_inline_comment(line: str) -> str:
    in_single = False
    in_double = False
    out: list[str] = []
    for ch in line:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            break
        out.append(ch)
    return "".join(out).rstrip()


def _simple_yaml_load(path: Path) -> dict[str, Any]:
    """Load a constrained YAML subset used by project configs.

    Supported:
    - indentation-based nested dicts
    - scalar values
    - quoted strings
    - inline comments outside quotes
    """

    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line = _strip_inline_comment(raw_line)
        if not line.strip():
            continue

        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            continue

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce_scalar(value)

    return root


def load_yaml_config(path: str | Path) -> dict[str, Any]:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"설정 파일이 없습니다: {cfg_path}")
    return _simple_yaml_load(cfg_path)


def load_model_parameters(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    cfg_path = Path(path) if path else DEFAULT_MODEL_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"모델 파라미터 파일이 없습니다: {cfg_path}")
    return _simple_yaml_load(cfg_path)


def get_parameter_value(
    params: dict[str, dict[str, Any]],
    name: str,
    default: Any = None,
) -> Any:
    item = params.get(name, {})
    if isinstance(item, dict) and "value" in item:
        return item["value"]
    return default


def dump_parameter_table(params: dict[str, dict[str, Any]], path: str | Path) -> None:
    rows = []
    for name, meta in params.items():
        rows.append(
            {
                "parameter": name,
                "value": json.dumps(meta.get("value"), ensure_ascii=False)
                if isinstance(meta.get("value"), (dict, list))
                else meta.get("value"),
                "unit": meta.get("unit"),
                "source_type": meta.get("source_type"),
                "source_note": meta.get("source_note"),
                "used_in": meta.get("used_in"),
                "fallback": meta.get("fallback"),
            }
        )
    import pandas as pd

    pd.DataFrame(rows).to_csv(path, index=False)
