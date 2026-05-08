from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    from .model_config import load_yaml_config
except ImportError:
    from model_config import load_yaml_config


DEFAULT_SENSITIVITY_SCENARIOS_PATH = (
    Path(__file__).resolve().parent / "config" / "sensitivity_scenarios.yaml"
)


def deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def clone_model_parameters(model_params: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return json.loads(json.dumps(model_params, ensure_ascii=False))


def apply_parameter_value_overrides(
    model_params: dict[str, dict[str, Any]],
    parameter_overrides: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not parameter_overrides:
        return clone_model_parameters(model_params)
    merged = clone_model_parameters(model_params)
    for name, value in parameter_overrides.items():
        current = merged.get(name, {})
        if not isinstance(current, dict):
            current = {}
        current["value"] = value
        merged[name] = current
    return merged


def load_sensitivity_scenarios(
    path: str | Path | None = None,
) -> dict[str, Any]:
    cfg_path = Path(path) if path else DEFAULT_SENSITIVITY_SCENARIOS_PATH
    return load_yaml_config(cfg_path)


def resolve_named_profile(
    config: dict[str, Any],
    catalog_key: str,
    profile_name: str,
) -> dict[str, Any]:
    catalog = config.get(catalog_key, {})
    if not isinstance(catalog, dict):
        return {}
    selected = catalog.get(profile_name, {})
    return selected if isinstance(selected, dict) else {}


def materialize_sensitivity_case(
    config: dict[str, Any],
    case_overrides: dict[str, Any] | None = None,
    *,
    case_id: str | None = None,
    dimension_name: str | None = None,
    level_name: str | None = None,
) -> dict[str, Any]:
    base_case = config.get("base_scenario", {})
    case = deep_merge(base_case if isinstance(base_case, dict) else {}, case_overrides or {})

    walking_profile_name = str(case.get("walking_speed_profile", "base"))
    vehicle_speed_name = str(case.get("vehicle_speed_factor", "base"))
    accident_risk_name = str(case.get("accident_risk_coefficient", "base"))
    extension_policy_name = str(case.get("green_extension_policy", "base_extension"))

    walking_profile = resolve_named_profile(config, "walking_speed_profiles", walking_profile_name)
    vehicle_speed_profile = resolve_named_profile(config, "vehicle_speed_profiles", vehicle_speed_name)
    accident_risk_profile = resolve_named_profile(
        config, "accident_risk_profiles", accident_risk_name
    )
    extension_policy = resolve_named_profile(
        config, "green_extension_policies", extension_policy_name
    )

    case["case_id"] = case_id or str(case.get("case_id", "base"))
    case["dimension_name"] = dimension_name or str(case.get("dimension_name", "base"))
    case["level_name"] = level_name or str(case.get("level_name", "base"))
    case["walking_speed_profile_name"] = walking_profile_name
    case["walking_speed_profile_config"] = walking_profile
    case["vehicle_speed_factor_name"] = vehicle_speed_name
    case["vehicle_speed_factor_config"] = vehicle_speed_profile
    case["accident_risk_coefficient_name"] = accident_risk_name
    case["accident_risk_coefficient_config"] = accident_risk_profile
    case["green_extension_policy_name"] = extension_policy_name
    case["green_extension_policy_config"] = extension_policy

    parameter_overrides = case.get("parameter_overrides", {})
    if not isinstance(parameter_overrides, dict):
        parameter_overrides = {}
    case["parameter_overrides"] = deep_merge(
        parameter_overrides,
        {
            **walking_profile,
            **extension_policy,
        },
    )

    case["pedestrian_arrival_rate_multiplier"] = float(
        case.get("pedestrian_arrival_rate_multiplier", 1.0) or 1.0
    )
    case["elderly_ratio_multiplier"] = float(case.get("elderly_ratio_multiplier", 1.0) or 1.0)
    if case.get("elderly_ratio_override") is not None:
        case["elderly_ratio_override"] = float(case["elderly_ratio_override"])
    case["vehicle_volume_multiplier"] = float(case.get("vehicle_volume_multiplier", 1.0) or 1.0)
    case["saturation_flow_multiplier"] = float(
        vehicle_speed_profile.get("saturation_flow_multiplier", 1.0) or 1.0
    )
    case["vehicle_speed_risk_multiplier"] = float(
        vehicle_speed_profile.get("risk_exposure_multiplier", 1.0) or 1.0
    )
    case["vehicle_speed_display_factor"] = float(
        vehicle_speed_profile.get("speed_factor", 1.0) or 1.0
    )
    case["accident_risk_coefficient_value"] = float(
        accident_risk_profile.get("coefficient", 1.0) or 1.0
    )
    return case


def build_sensitivity_cases(
    config: dict[str, Any],
    dimensions: list[str] | None = None,
) -> list[dict[str, Any]]:
    catalog = config.get("sensitivity_dimensions", {})
    if not isinstance(catalog, dict):
        return [materialize_sensitivity_case(config, {}, case_id="base", dimension_name="base", level_name="base")]

    selected_dimensions = dimensions or list(catalog.keys())
    cases = [
        materialize_sensitivity_case(
            config,
            {},
            case_id="base",
            dimension_name="base",
            level_name="base",
        )
    ]
    for dimension_name in selected_dimensions:
        dimension = catalog.get(dimension_name, {})
        if not isinstance(dimension, dict):
            continue
        levels = dimension.get("levels", {})
        if not isinstance(levels, dict):
            continue
        for level_name, overrides in levels.items():
            cases.append(
                materialize_sensitivity_case(
                    config,
                    overrides if isinstance(overrides, dict) else {},
                    case_id=f"{dimension_name}:{level_name}",
                    dimension_name=dimension_name,
                    level_name=str(level_name),
                )
            )
    return cases


def resolve_sensitivity_case(config: dict[str, Any], case_name: str) -> dict[str, Any]:
    catalog = config.get("scenario_catalog", {})
    if isinstance(catalog, dict) and case_name in catalog:
        overrides = catalog.get(case_name, {})
        return materialize_sensitivity_case(
            config,
            overrides if isinstance(overrides, dict) else {},
            case_id=case_name,
        )

    if ":" in case_name:
        dimension_name, level_name = case_name.split(":", 1)
        dimensions = config.get("sensitivity_dimensions", {})
        if isinstance(dimensions, dict):
            dimension = dimensions.get(dimension_name, {})
            if isinstance(dimension, dict):
                levels = dimension.get("levels", {})
                if isinstance(levels, dict) and level_name in levels:
                    overrides = levels.get(level_name, {})
                    return materialize_sensitivity_case(
                        config,
                        overrides if isinstance(overrides, dict) else {},
                        case_id=case_name,
                        dimension_name=dimension_name,
                        level_name=level_name,
                    )

    raise KeyError(f"정의되지 않은 sensitivity case: {case_name}")
