"""Generators and checks derived from a pyioc.yaml spec."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .instance import InstanceError, resolve_instance
from .spec import Param, Spec

CHART_URL = "https://github.com/infn-epics/ioc-chart.git"
_JSON_TYPES = {"string": "string", "host": "string", "int": "integer", "float": "number",
               "bool": "boolean", "list": "array", "map": "object"}


def _param_schema(param: Param) -> dict[str, Any]:
    schema: dict[str, Any] = {}
    if param.type == "enum":
        schema["enum"] = list(param.choices)
    elif param.type == "bool":
        # YAML authors (and iocparam) also write "true"/"yes"/1
        schema["anyOf"] = [{"type": "boolean"}, {"type": "string"}, {"type": "integer"}]
    elif param.type in ("int", "float"):
        schema["type"] = [_JSON_TYPES[param.type], "string"]
    elif param.type in _JSON_TYPES:
        schema["type"] = _JSON_TYPES[param.type]
    for key, value in (("minimum", param.min), ("maximum", param.max), ("pattern", param.pattern)):
        if value is not None:
            schema[key] = value
    description = param.description
    if param.unit:
        description = f"{description} [{param.unit}]".strip()
    if description:
        schema["description"] = description
    if param.has_default and param.default is not None:
        schema["default"] = param.default
    return schema


def _properties(params: Mapping[str, Param]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for param in params.values():
        schema = _param_schema(param)
        properties[param.name] = schema
        for alias in param.aliases:
            properties[alias] = {**schema, "description": f"Alias of '{param.name}'."}
    return properties


def json_schema(spec: Spec, fragment: bool = False) -> dict[str, Any]:
    """JSON Schema of one IOC entry.

    With ``fragment=True`` it returns an ``if devtype == <name> then ...`` clause
    meant to be merged into a beamline values.yaml schema (``allOf``).
    """
    props = _properties(spec.parameters)
    required = [p.name for p in spec.parameters.values() if p.required]
    device_props = _properties(spec.device_params())
    if spec.devices is not None:
        props.update(device_props)  # top-level values are defaults for every device
        if spec.devices.multiple:
            props["devices"] = {
                "type": "array",
                "description": f"{spec.name} devices served by this IOC; PV prefix "
                               f"'{spec.devices.prefix}'.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Device name, used in the PV prefix."},
                        "prefix": {"type": "string", "description": "Explicit PV prefix (overrides the template)."},
                        "disable": {"type": "boolean"},
                        **device_props,
                    },
                },
            }
    body: dict[str, Any] = {"properties": props}
    if required:
        body["required"] = required
    if fragment:
        return {
            "if": {"properties": {"devtype": {"const": spec.name}}, "required": ["devtype"]},
            "then": body,
        }
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": f"{spec.name} Python IOC instance",
        "description": spec.description,
        "type": "object",
        "required": ["iocprefix", *required],
        "properties": {
            "iocprefix": {"type": "string", "description": "PV prefix of the IOC."},
            "devtype": {"const": spec.name},
            **props,
        },
    }


def ioc_defaults(spec: Spec, tag: str | None = None) -> dict[str, Any]:
    """The ``iocDefaults.<name>`` block for a beamline values.yaml."""
    defaults: dict[str, Any] = {"charturl": CHART_URL, "devtype": spec.name}
    if spec.devgroup:
        defaults["devgroup"] = spec.devgroup
    if spec.image:
        version = tag or spec.version
        defaults["image"] = f"{spec.image}:{version}" if version and ":" not in spec.image.rsplit("/", 1)[-1] else spec.image
    defaults["start"] = "pyioc-run"
    defaults["pva"] = "pva" in spec.protocols
    if spec.docs:
        defaults["asset"] = spec.docs
    return defaults


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _table(params: Mapping[str, Param]) -> list[str]:
    lines = ["| Key | Type | Default | Description |", "|---|---|---|---|"]
    for param in params.values():
        kind = param.type if param.type != "enum" else " \\| ".join(param.choices)
        if param.min is not None or param.max is not None:
            kind += f" [{_fmt(param.min)} .. {_fmt(param.max)}]"
        if param.unit:
            kind += f" {param.unit}"
        default = "**required**" if param.required else _fmt(param.default if param.has_default else None)
        names = ", ".join(f"`{key}`" for key in param.keys())
        lines.append(f"| {names} | {kind} | {default} | {param.description} |")
    return lines


def markdown(spec: Spec) -> str:
    lines = [f"# {spec.name}", ""]
    if spec.description:
        lines += [spec.description, ""]
    lines += ["| | |", "|---|---|", f"| devtype | `{spec.name}` |"]
    if spec.image:
        lines.append(f"| image | `{spec.image}` |")
    lines += [f"| protocols | {', '.join(spec.protocols)} |", f"| start | `pyioc-run` |", ""]
    if spec.parameters:
        lines += ["## IOC parameters", "", *_table(spec.parameters), ""]
    if spec.devices is not None:
        lines += ["## Device parameters", ""]
        if spec.devices.multiple:
            lines += [f"Set at top level for a single device (PV prefix `{spec.devices.single_prefix}`), "
                      f"or per entry of `devices:` (PV prefix `{spec.devices.prefix}`); top-level "
                      "values are defaults for every entry.", ""]
        lines += [*_table(spec.device_params()), ""]
    return "\n".join(lines)


def _merged_iocs(values: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """IOC entries of a beamline values.yaml merged with iocDefaults, as epik8s-chart does."""
    iocs = (values.get("epicsConfiguration") or {}).get("iocs") or []
    if isinstance(iocs, Mapping):
        items = [(str(key), dict(value or {}, name=(value or {}).get("name", key)))
                 for key, value in iocs.items()]
    else:
        items = [(str(entry.get("name", index)), dict(entry)) for index, entry in enumerate(iocs)]
    defaults = values.get("iocDefaults") or {}
    merged = []
    for key, ioc in items:
        selector = ioc.get("template") or ioc.get("devtype") or ""
        base = defaults.get(selector) if selector else None
        merged.append((key, _deep_merge(base or {}, ioc)))
    return merged


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_values(spec: Spec, values: Mapping[str, Any]) -> tuple[int, list[str], list[str]]:
    """Validate every IOC of ``spec.name`` in a beamline values.yaml.

    Returns (number of IOCs checked, errors, warnings).  ``template`` is reported as an
    error: it would send the IOC through ioc-chart's ibek path.
    """
    problems: list[str] = []
    warnings: list[str] = []
    checked = 0
    for key, ioc in _merged_iocs(values):
        if ioc.get("devtype") != spec.name and ioc.get("template") != spec.name:
            continue
        checked += 1
        if ioc.get("disable") is True:
            continue
        if ioc.get("template"):
            problems.append(f"{key}: remove 'template' (it selects ibek in ioc-chart); use devtype only")
        if ioc.get("image") is None:
            problems.append(f"{key}: no image (add iocDefaults.{spec.name}.image)")
        start = str(ioc.get("start") or "")
        if not start.split() or Path(start.split()[0]).name != "pyioc-run":
            problems.append(f"{key}: start should be 'pyioc-run' (got {start!r})")
        if ioc.get("pva") is not False and "pva" not in spec.protocols:
            problems.append(f"{key}: set 'pva: false' ({spec.name} serves only Channel Access)")
        try:
            instance = resolve_instance(spec, ioc)
        except InstanceError as exc:
            problems += [f"{key}: {error}" for error in exc.errors]
            continue
        warnings += [f"{key}: {warning}" for warning in instance.warnings]
    return checked, problems, warnings
