"""Generate a Python IOC project, or a software box, from a short description.

Used by ``pyioc init`` and by ARGUS's IOC engineer (``ioc_scaffold_pyioc``), so both produce the
same project.  Every PV declares where its value comes from (``source``); the shape follows:

* **device IOC** — at least one PV with ``source: driver`` (the default), or a ``transport``.
  A Python package started by ``pyioc-run`` in its own image: ``pyioc.yaml`` (what a values.yaml
  entry may set), ``config.yaml`` (the PVs, rules and expressions, in iocmng's format), a driver,
  a Dockerfile and CI.
* **box** — no driver: logic over PVs that already exist (``link``), derived values (``expr``),
  operator settings and rule outputs (``soft``).  A ``config.yaml`` folder run by ``iocmng-run``
  in the shared epik8s-softioc-mng image, deployed as a ``softiocs:`` entry.

Both run on iocmng (pythonSoftIOC: CA and PVA).  The generated device IOC keeps what earlier
IOCs learned the hard way, now done by iocmng's driver source: enums handled by index,
full-length waveforms, ``DBR_STRING`` identifiers, one traceback per repeated error, driver calls
outside the IOC's event loop, reconnection of a lost device.

Description (YAML or a dict)::

    name: tpg366                      # devtype / box name (required)
    description: Pfeiffer TPG366 gauge controller
    devgroup: vac
    transport: tcp                    # device IOC: tcp (socket driver skeleton) | none
    default_port: 8000
    multiple: true                    # device IOC: allow a `devices:` list
    mode: continuous                  # continuous (rules every `interval`) | reactive (on change)
    pvs:
      - {name: Pressure, type: float, unit: mbar, precision: 3, buffer_size: 60}  # source: driver
      - {name: Unit, type: enum, access: rw, choices: [mbar, Torr, Pa]}
      - {name: Reset, type: bool, access: cmd}
      - {name: Upstream, type: float, source: {link: "SPARC:VAC:GAUGE02:Pressure"}}
      - {name: Average, type: float, source: {expr: "mean(Pressure_buf)"}}
      - {name: Leak, type: bool, source: soft, choices: [OK, LEAK], latch: true, alarm_on: MAJOR}
    rules:
      - {id: LEAK, condition: "Pressure > 3 * Upstream", tolerance: 10, outputs: {Leak: 1}}
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any, Mapping

PYIOC_VERSION = "0.3.0"
PYIOC_REQUIREMENT = (f"epik8s-pyioc @ https://github.com/infn-epics/epik8s-pyioc/archive/refs/tags/"
                     f"v{PYIOC_VERSION}.tar.gz")
# the first iocmng with the driver source and the asyncio dispatcher
IOCMNG_VERSION = "2.8.0"
IOCMNG_REQUIREMENT = f"iocmng>={IOCMNG_VERSION}"
BASE_IMAGE = f"ghcr.io/infn-epics/epik8s-softioc-mng:{IOCMNG_VERSION}"

PV_TYPES = ("float", "int", "bool", "enum", "string", "waveform")
ACCESS = ("ro", "rw", "cmd")
SOURCES = ("driver", "soft", "link", "expr")
LINK_MODES = ("monitor", "poll")
MODES = ("continuous", "reactive")
TRANSPORTS = ("tcp", "none")
# the standard PVs of every epik8s-pyioc device
DEVICE_PVS = {"Connected", "LastError", "LastErrorLong"}
# the PVs iocmng creates for every task
IOCMNG_PVS = {"STATUS", "MESSAGE", "ENABLE", "RUN", "CYCLE_COUNT", "VERSION", "CLEAR", "RESET", "CONNECTED",
              "CONN_INP", "CONN_INP_NAMES", "CONN_OUT", "CONN_OUT_NAMES"}
# iocmng's expression functions (iocmng.core.functions)
FUNCTIONS = {"abs", "round", "sqrt", "log", "exp", "pow", "floor", "ceil", "clamp", "mean", "std", "variance",
             "median", "rms", "min", "max", "any_of", "all_of", "count_true", "length", "sum_of", "diff", "last",
             "moving_avg", "derivative"}
EPICS_STRING = 39
EXAMPLE = {
    "name": "tpg366", "description": "Pfeiffer TPG366 gauge controller", "devgroup": "vac",
    "transport": "tcp", "default_port": 8000,
    "pvs": [{"name": "Pressure", "type": "float", "access": "ro", "unit": "mbar", "precision": 3},
            {"name": "Unit", "type": "enum", "access": "rw", "choices": ["mbar", "Torr", "Pa"]},
            {"name": "Reset", "type": "bool", "access": "cmd"}],
}
EXAMPLE_BOX = {
    "name": "vac-leak", "description": "Leak detection between two gauges", "devgroup": "vac",
    "parameters": {"ratio": 3.0},
    "pvs": [{"name": "P1", "type": "float", "unit": "mbar", "precision": 3,
             "source": {"link": "SPARC:VAC:GAUGE01:Pressure"}},
            {"name": "P2", "type": "float", "unit": "mbar", "precision": 3,
             "source": {"link": "SPARC:VAC:GAUGE02:Pressure"}},
            {"name": "Ratio", "type": "float", "precision": 2, "source": {"expr": "P1 / P2 if P2 else 0"}},
            {"name": "Leak", "type": "bool", "choices": ["OK", "LEAK"], "source": "soft", "latch": True,
             "alarm_on": "MAJOR"}],
    "rules": [{"id": "LEAK", "condition": "P2 > 0 and P1 / P2 > ratio", "tolerance": 10,
               "message": "P1/P2 above ratio", "outputs": {"Leak": 1}}],
}


class ScaffoldError(ValueError):
    """The description cannot become a project; ``errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --- the description ------------------------------------------------------------------

_PV_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_LINK = re.compile(r"[A-Za-z0-9_$\[\]<>{}-]+(:[A-Za-z0-9_$\[\]<>{}.-]+)*(\.[A-Z0-9]+)?")


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part) or "Box"


def _source(raw: Any, where: str, errors: list[str]) -> dict:
    """``driver`` | ``soft`` | ``{link: PV, mode: monitor|poll}`` | ``{expr: "..."}``."""
    if raw is None or raw in ("driver", "soft"):
        return {"kind": raw or "driver"}
    if isinstance(raw, Mapping):
        keys = set(raw)
        if "link" in raw and keys <= {"link", "mode", "poll_rate"}:
            link, mode = str(raw.get("link") or ""), str(raw.get("mode") or "monitor")
            if not _LINK.fullmatch(link):
                errors.append(f"{where}: link {link!r} is not a PV name")
            if mode not in LINK_MODES:
                errors.append(f"{where}: link mode one of {', '.join(LINK_MODES)}")
            out = {"kind": "link", "link": link, "mode": mode}
            if raw.get("poll_rate") is not None:
                try:
                    out["poll_rate"] = float(raw["poll_rate"])
                except (TypeError, ValueError):
                    errors.append(f"{where}: poll_rate is a number of seconds")
            return out
        if "expr" in raw and keys == {"expr"}:
            return {"kind": "expr", "expr": str(raw["expr"] or "")}
    errors.append(f"{where}: source is driver, soft, {{link: <PV>, mode: monitor|poll}} or {{expr: \"...\"}}")
    return {"kind": "soft"}


def _expression_names(text: str, where: str, errors: list[str]) -> set[str]:
    """The variable names of an expression (function names excluded); records syntax errors."""
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        errors.append(f"{where}: {text!r} is not an expression ({exc.msg})")
        return set()
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} - called - {"True", "False", "None"}


def _parse_pv(i: int, raw: Any, errors: list[str]) -> dict | None:
    where = f"pvs[{i}]"
    if not isinstance(raw, Mapping):
        errors.append(f"{where}: a mapping")
        return None
    unknown = set(raw) - {"name", "type", "access", "unit", "precision", "choices", "length", "limits",
                          "description", "default", "source", "buffer_size", "latch", "alarm_on", "trigger"}
    if unknown:
        errors.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    name = str(raw.get("name") or "")
    where = f"pvs[{i}] {name}".rstrip()
    if not _PV_NAME.fullmatch(name):
        errors.append(f"{where}: name must start with a letter (letters, digits, '_')")
        return None
    source = _source(raw.get("source"), where, errors)
    kind = str(raw.get("type") or "float")
    access = str(raw.get("access") or "ro")
    if kind not in PV_TYPES:
        errors.append(f"{where}: type one of {', '.join(PV_TYPES)}")
    if access not in ACCESS:
        errors.append(f"{where}: access one of {', '.join(ACCESS)} (cmd = a write-only action)")
    if access == "cmd" and source["kind"] != "driver":
        errors.append(f"{where}: a cmd PV sends a command to the device: it needs source: driver")
    if access == "rw" and source["kind"] in ("link", "expr"):
        errors.append(f"{where}: a {source['kind']} PV is read only (rules write linked PVs with actuators)")
    choices = [str(c) for c in raw.get("choices") or []]
    if kind == "bool":
        choices = choices or ["Off", "On"]
        if len(choices) != 2:
            errors.append(f"{where}: a bool has two states (choices: [Off, On])")
    if kind == "enum" and not 2 <= len(choices) <= 16:
        errors.append(f"{where}: an enum needs 2..16 choices")
    if any(len(c) > 25 for c in choices):
        errors.append(f"{where}: EPICS enum strings are at most 25 characters")
    if access == "cmd" and kind != "bool":
        errors.append(f"{where}: a cmd PV is a bool (Idle/<name>): set type: bool")
    length = raw.get("length")
    if kind == "waveform":
        try:
            length = int(length or 0)
            if not 1 <= length <= 1_000_000:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{where}: a waveform needs length (1 .. 1000000 elements)")
            length = 1
        if access != "ro":
            errors.append(f"{where}: waveforms are read-only")
        if source["kind"] != "driver":
            errors.append(f"{where}: only a driver PV can be a waveform")
    limits = raw.get("limits")
    if limits is not None and (not isinstance(limits, list) or len(limits) != 2):
        errors.append(f"{where}: limits is [low, high]")
        limits = None
    buffer_size = raw.get("buffer_size")
    if buffer_size is not None:
        try:
            buffer_size = int(buffer_size)
            if buffer_size < 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{where}: buffer_size is a number of samples (>= 1)")
            buffer_size = None
        if source["kind"] not in ("driver", "link") or kind in ("string", "waveform"):
            errors.append(f"{where}: buffer_size keeps the samples of a numeric driver or link PV")
    alarm_on = str(raw.get("alarm_on") or "").upper()
    if alarm_on and alarm_on not in ("MAJOR", "MINOR"):
        errors.append(f"{where}: alarm_on is MAJOR or MINOR")
    if raw.get("latch") and (kind != "bool" or source["kind"] != "soft"):
        errors.append(f"{where}: latch holds a soft bool set by a rule until CLEAR/RESET")
    return {
        "name": name, "type": kind, "access": access, "source": source,
        "unit": str(raw.get("unit") or ""), "precision": raw.get("precision"),
        "choices": choices if kind in ("bool", "enum") else [],
        "length": length if kind == "waveform" else None, "limits": limits,
        "description": str(raw.get("description") or ""), "default": raw.get("default"),
        "buffer_size": buffer_size, "latch": bool(raw.get("latch")), "alarm_on": alarm_on,
        "trigger": None if raw.get("trigger") is None else bool(raw.get("trigger")),
    }


def _check_logic(d: dict, rules_raw: Any, defaults_raw: Any, errors: list[str]) -> tuple[list, dict]:
    """Rules, rule defaults and expressions refer only to what iocmng can see."""
    pvs = {p["name"]: p for p in d["pvs"]}
    # iocmng evaluates expressions over link and driver readings, their buffers and the parameters
    visible = {n for n, p in pvs.items() if p["source"]["kind"] in ("driver", "link") and p["access"] != "cmd"}
    visible |= {f"{n}_buf" for n, p in pvs.items() if p["buffer_size"]}
    visible |= set(d["parameter_names"])

    def names_ok(text: str, where: str, earlier: frozenset = frozenset()):
        for name in sorted(_expression_names(text, where, errors) - visible - earlier):
            if name in pvs:
                errors.append(f"{where}: {name} is a {pvs[name]['source']['kind']} PV; rules see only "
                              f"driver and link PVs, their <name>_buf buffers and the parameters, "
                              f"expressions also the expr PVs before them")
            elif name not in FUNCTIONS:
                errors.append(f"{where}: unknown name {name!r}")

    earlier: set = set()   # iocmng evaluates the transforms in order, each seeing the ones before
    for p in d["pvs"]:
        if p["source"]["kind"] == "expr":
            names_ok(p["source"]["expr"], f"pvs {p['name']}: expr", frozenset(earlier))
            earlier.add(p["name"])

    rules, seen = [], set()
    if rules_raw is not None and not isinstance(rules_raw, list):
        errors.append("rules: a list of {id, condition, outputs, actuators, ...}")
        rules_raw = []
    for i, raw in enumerate(rules_raw or []):
        where = f"rules[{i}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{where}: a mapping")
            continue
        unknown = set(raw) - {"id", "condition", "tolerance", "message", "message_pv", "outputs", "actuators"}
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
        rule_id = str(raw.get("id") or "")
        where = f"rules[{i}] {rule_id}".rstrip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", rule_id):
            errors.append(f"{where}: id required (letters, digits, '_')")
        if rule_id in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(rule_id)
        condition = str(raw.get("condition") or "")
        if not condition:
            errors.append(f"{where}: condition required")
        else:
            names_ok(condition, f"{where}: condition")
        try:
            tolerance = float(raw.get("tolerance") or 0)
            if not 0 <= tolerance < float("inf"):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{where}: tolerance is a number of seconds >= 0")
            tolerance = 0.0
        outputs = raw.get("outputs") or {}
        actuators = raw.get("actuators") or {}
        if not isinstance(outputs, Mapping) or not isinstance(actuators, Mapping):
            errors.append(f"{where}: outputs and actuators map PV names to values")
            outputs, actuators = {}, {}
        if not outputs and not actuators:
            errors.append(f"{where}: a rule sets outputs, writes actuators, or both")
        for name in outputs:
            p = pvs.get(name)
            if p is None:
                errors.append(f"{where}: output {name} is not a PV")
            elif not (p["source"]["kind"] == "soft" or (p["source"]["kind"] == "driver" and p["access"] == "rw")):
                errors.append(f"{where}: output {name} must be a soft PV or a driver setting (access: rw)")
        for name in actuators:
            p = pvs.get(name)
            if p is None or p["source"]["kind"] != "link":
                errors.append(f"{where}: actuator {name} must be a link PV (its linked PV is written)")
        message_pv = raw.get("message_pv")
        if message_pv and (message_pv not in pvs or pvs[message_pv]["type"] != "string"
                           or pvs[message_pv]["source"]["kind"] != "soft"):
            errors.append(f"{where}: message_pv {message_pv} must be a soft string PV")
        rule = {"id": rule_id, "condition": condition}
        if tolerance:
            rule["tolerance"] = tolerance
        for key in ("message", "message_pv"):
            if raw.get(key):
                rule[key] = str(raw[key])
        if outputs:
            rule["outputs"] = dict(outputs)
        if actuators:
            rule["actuators"] = dict(actuators)
        rules.append(rule)

    rule_defaults = defaults_raw or {}
    if not isinstance(rule_defaults, Mapping):
        errors.append("rule_defaults: maps soft PV names to the value set before each rule evaluation")
        rule_defaults = {}
    for name in rule_defaults:
        if name not in pvs or pvs[name]["source"]["kind"] != "soft":
            errors.append(f"rule_defaults: {name} must be a soft PV")
    return rules, dict(rule_defaults)


def normalize(description: Mapping[str, Any] | str) -> dict:
    """Validate a description and fill in the defaults."""
    import yaml

    if isinstance(description, str):
        try:
            description = yaml.safe_load(description) or {}
        except yaml.YAMLError as exc:
            raise ScaffoldError([f"not YAML: {exc}"]) from exc
    if not isinstance(description, Mapping):
        raise ScaffoldError(["the description must be a mapping"])
    errors: list[str] = []
    known = {"name", "package", "description", "devgroup", "image", "version", "transport", "default_port",
             "multiple", "parameters", "device_parameters", "pvs", "protocols", "rules", "rule_defaults",
             "mode", "interval", "python"}
    for key in sorted(set(description) - known):
        errors.append(f"{key}: unknown field (known: {', '.join(sorted(known))})")
    name = str(description.get("name") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        errors.append("name: required, lower case letters, digits, '_' or '-' (it is the devtype)")
    package = str(description.get("package") or re.sub(r"\W", "_", name) + "_ioc")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", package):
        errors.append(f"package: {package!r} is not a Python package name")

    pvs, seen = [], set()
    raw_pvs = description.get("pvs") or []
    if not isinstance(raw_pvs, list) or not raw_pvs:
        errors.append("pvs: a non-empty list of {name, type, access, source, ...}")
        raw_pvs = []
    for i, raw in enumerate(raw_pvs):
        pv = _parse_pv(i, raw, errors)
        if pv is None:
            continue
        if pv["name"].lower() in seen:
            errors.append(f"pvs[{i}] {pv['name']}: duplicate name")
        seen.add(pv["name"].lower())
        pvs.append(pv)

    kind = "device" if any(p["source"]["kind"] == "driver" for p in pvs) or "transport" in description \
        else "box"
    reserved = IOCMNG_PVS | (DEVICE_PVS if kind == "device" else set())
    for p in pvs:
        if p["name"] in reserved:
            errors.append(f"pvs {p['name']}: generated for every {'device' if kind == 'device' else 'box'}")
    transport = str(description.get("transport") or ("tcp" if kind == "device" else "none"))
    if transport not in TRANSPORTS:
        errors.append(f"transport: one of {', '.join(TRANSPORTS)}")
    if kind == "device" and not any(p["source"]["kind"] == "driver" for p in pvs) and pvs:
        errors.append("transport: a device IOC needs at least one PV with source: driver "
                      "(leave transport out for a box)")
    try:
        default_port = int(description.get("default_port") or 5025)
        if not 1 <= default_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("default_port: a TCP port number")
        default_port = 5025
    protocols = description.get("protocols") or ["ca", "pva"]
    if not isinstance(protocols, list) or not protocols or not set(protocols) <= {"ca", "pva"}:
        errors.append("protocols: a list of ca, pva (pythonSoftIOC serves both)")
        protocols = ["ca", "pva"]
    mode = str(description.get("mode") or ("continuous" if kind == "device" else "reactive"))
    if mode not in MODES:
        errors.append(f"mode: one of {', '.join(MODES)}")
    try:
        interval = float(description.get("interval") or 1.0)
        if not interval > 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("interval: seconds between evaluations (> 0)")
        interval = 1.0

    from .spec import SpecError, _parse_params, RESERVED_KEYS
    generated = {"interfaces", "log_level", "link_protocol", "simulate", "poll_period", "server", "ip", "port",
                 "timeout", "mode", "interval", "driver", "driver_args", "reconnect_period", "version"}
    box_generated = {"mode", "interval", "driver", "driver_args", "simulate", "poll_period", "reconnect_period"}
    params: dict = {}
    device_params: dict = {}
    if kind == "device":
        for key in ("parameters", "device_parameters"):
            try:
                parsed = _parse_params(description.get(key), key)
            except SpecError as exc:
                errors.append(str(exc))
                parsed = {}
            for pname in parsed:
                if pname in RESERVED_KEYS:
                    errors.append(f"{key}.{pname}: reserved by epik8s/ioc-chart")
                if pname in generated:
                    errors.append(f"{key}.{pname}: generated by the scaffold already")
            (params if key == "parameters" else device_params).update(description.get(key) or {})
        parameter_names = set(params) | set(device_params) | {"simulate", "poll_period", "timeout"}
    else:
        if description.get("device_parameters"):
            errors.append("device_parameters: only a device IOC has devices")
        raw_params = description.get("parameters") or {}
        if not isinstance(raw_params, Mapping):
            errors.append("parameters: a box's parameters map names to values (ratio: 3)")
            raw_params = {}
        for pname, value in raw_params.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(pname)):
                errors.append(f"parameters.{pname}: not a name expressions can use")
            elif pname in box_generated:
                errors.append(f"parameters.{pname}: set by the generator (mode, interval, ...)")
            if not isinstance(value, (int, float, str, bool)):
                errors.append(f"parameters.{pname}: a number, a string or a bool")
        params = dict(raw_params)
        parameter_names = set(params)
    for p in pvs:
        if p["name"] in parameter_names:
            errors.append(f"pvs {p['name']}: same name as a parameter")

    d = {
        "kind": kind, "name": name, "package": package,
        "description": str(description.get("description") or f"{name} {'IOC' if kind == 'device' else 'box'}"),
        "devgroup": str(description.get("devgroup") or "io"),
        "image": str(description.get("image") or (f"ghcr.io/infn-epics/{name}" if kind == "device" else "")),
        "version": str(description.get("version") or "0.1.0"), "transport": transport,
        "default_port": default_port, "multiple": bool(description.get("multiple", True)),
        "protocols": list(protocols), "mode": mode, "interval": interval,
        "parameters": params, "device_parameters": device_params, "parameter_names": sorted(parameter_names),
        "python": bool(description.get("python", False)),
        "class_name": _class_name(name), "pvs": pvs,
    }
    for p in pvs:
        # iocmng re-evaluates a reactive task only when a link with trigger: true changes
        if p["trigger"] is None:
            p["trigger"] = mode == "reactive" and p["source"]["kind"] == "link"
    if kind == "device" and description.get("python") is not None:
        errors.append("python: a device IOC always has its Python class (ioc.py)")
    d["rules"], d["rule_defaults"] = _check_logic(d, description.get("rules"), description.get("rule_defaults"),
                                                  errors)
    if errors:
        raise ScaffoldError(errors)
    return d


# --- shared: the iocmng config -------------------------------------------------------

def _yaml_dump(data: Any) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True, width=110)


def _iocmng_pv(p: dict) -> dict:
    out: dict = {"type": p["type"]}
    source = p["source"]
    if source["kind"] == "driver":
        out["source"] = "driver"
        if p["access"] != "ro":
            out["access"] = p["access"]
    elif source["kind"] == "link":
        out["link"] = source["link"]
        out["link_mode"] = source["mode"]
        if "poll_rate" in source:
            out["poll_rate"] = source["poll_rate"]
    default = p["default"]
    if p["type"] in ("float", "int"):
        out["value"] = (float if p["type"] == "float" else int)(default or 0)
    elif p["type"] == "string":
        out["value"] = str(default or "")[:EPICS_STRING]
    elif p["type"] == "enum":
        out["choices"] = list(p["choices"])
        out["value"] = str(default) if default in p["choices"] else p["choices"][0]
    elif p["type"] == "bool":
        if p["access"] != "cmd":
            out["znam"], out["onam"] = p["choices"]
            out["value"] = 1 if default in (1, True, p["choices"][1]) else 0
    elif p["type"] == "waveform":
        out["length"] = p["length"]
    if p["unit"]:
        out["unit"] = p["unit"]
    if p["precision"] is not None and p["type"] in ("float", "waveform"):
        out["prec"] = int(p["precision"])
    if p["limits"] and p["type"] in ("float", "int"):
        out["low"], out["high"] = p["limits"]
    for key in ("buffer_size", "alarm_on"):
        if p[key]:
            out[key] = p[key]
    if p["latch"]:
        out["latch"] = True
    if p["trigger"]:
        out["trigger"] = True
    return out


def _is_input(p: dict) -> bool:
    """iocmng inputs: client-written PVs and linked PVs; outputs: readbacks and computed values."""
    kind = p["source"]["kind"]
    return kind == "link" or p["access"] in ("rw", "cmd")


def _config_dict(d: dict) -> dict:
    config: dict = {"parameters": {"mode": d["mode"], "interval": d["interval"]}}
    if d["kind"] == "box":
        config["parameters"].update(d["parameters"])
    inputs = {p["name"]: _iocmng_pv(p) for p in d["pvs"] if _is_input(p)}
    outputs = {p["name"]: _iocmng_pv(p) for p in d["pvs"] if not _is_input(p)}
    config["arguments"] = {k: v for k, v in (("inputs", inputs), ("outputs", outputs)) if v}
    transforms = [{"output": p["name"], "expression": p["source"]["expr"]}
                  for p in d["pvs"] if p["source"]["kind"] == "expr"]
    if transforms:
        config["transforms"] = transforms
    if d["rule_defaults"]:
        config["rule_defaults"] = d["rule_defaults"]
    if d["rules"]:
        config["rules"] = d["rules"]
    return config


def _config_yaml(d: dict) -> str:
    if d["kind"] == "device":
        head = ("# iocmng configuration of every device of this IOC: its PVs, rules and expressions.\n"
                "# source: driver PVs are read from driver.py each poll_period; `link` PVs follow other\n"
                "# PVs; transforms compute values; rules act on conditions.  Per device, pyioc-run adds\n"
                "# the values.yaml parameters (simulate, poll_period, server, ...) to `parameters`.\n")
    else:
        head = (f"# {d['name']}: {d['description']}.\n"
                "# iocmng configuration: `link` inputs follow other PVs, transforms compute values,\n"
                "# rules act on conditions.  Run it with start.sh (iocmng-run).\n")
    return head + _yaml_dump(_config_dict(d))


def _pv_rows(d: dict) -> str:
    def where(p):
        s = p["source"]
        return {"driver": "device", "soft": "soft", "link": f"link `{s.get('link')}`",
                "expr": f"`{s.get('expr')}`"}[s["kind"]]
    return "\n".join(
        f"| `{p['name']}` | {p['type']}{' [' + str(p['length']) + ']' if p['length'] else ''} | "
        f"{ {'ro': 'RO', 'rw': 'RW', 'cmd': 'command'}[p['access']] } | {where(p)} | {p['unit']} | "
        f"{p['description']} |"
        for p in d["pvs"])


def _rule_rows(d: dict) -> str:
    if not d["rules"]:
        return ""
    rows = "\n".join(
        f"| `{r['id']}` | `{r['condition']}` | {r.get('tolerance', 0)} s | "
        f"{', '.join(f'{k}={v}' for k, v in {**r.get('outputs', {}), **r.get('actuators', {})}.items())} |"
        for r in d["rules"])
    return f"\n## Rules\n\n| Rule | Condition | Tolerance | Sets |\n|---|---|---|---|\n{rows}\n"


# --- device IOC -------------------------------------------------------------------------

def _spec_yaml(d: dict) -> str:
    device: dict = {
        "simulate": {"type": "bool", "default": False,
                     "description": "Built-in simulator instead of the hardware (no device needed)"},
        "poll_period": {"type": "float", "default": 1.0, "min": 0.05, "max": 3600, "unit": "s",
                        "description": "Readback polling period"},
    }
    if d["transport"] == "tcp":
        device.update({
            "server": {"type": "host", "aliases": ["ip"],
                       "description": "Device (or terminal server) address; required unless simulate"},
            "port": {"type": "int", "default": d["default_port"], "min": 1, "max": 65535,
                     "description": "TCP port (ioc-chart checks server:port is reachable before start)"},
            "timeout": {"type": "float", "default": 2.0, "min": 0.1, "max": 60, "unit": "s",
                        "description": "Reply timeout"},
        })
    device.update(d["device_parameters"])
    parameters = {
        "interfaces": {"type": "string", "default": "0.0.0.0",
                       "description": "Comma-separated IOC listen addresses"},
        "log_level": {"type": "enum", "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
                      "default": "INFO", "aliases": ["loglevel"]},
    }
    if any(p["source"]["kind"] == "link" for p in d["pvs"]):
        parameters["link_protocol"] = {"type": "enum", "choices": ["ca", "pva"], "default": "ca",
                                       "description": "Protocol used to read and write the linked PVs"}
    parameters.update(d["parameters"])
    spec = {
        "pyioc": 1,
        "name": d["name"],
        "description": d["description"],
        "devgroup": d["devgroup"],
        "image": d["image"],
        "version": d["version"],
        "entrypoint": f"{d['package']}.ioc:run",
        "protocols": d["protocols"],
        "parameters": parameters,
        "devices": {
            "multiple": d["multiple"],
            "prefix": "{iocprefix}:{name}:",
            "single_prefix": "{iocprefix}:",
            "parameters": device,
        },
    }
    return ("# epik8s-pyioc support definition: the parameters a values.yaml IOC entry may set.\n"
            "# Check it with `pyioc check --spec <this file>`; document it with `pyioc docs`.\n"
            + _yaml_dump(spec))


def _ioc_py(d: dict) -> str:
    return f'''"""{d['description']}: a Python IOC on iocmng (pythonSoftIOC, CA and PVA).

Started by ``pyioc-run`` (epik8s-pyioc): the values.yaml IOC entry is validated against
pyioc.yaml and handed to :func:`run` as an ``Instance``.  Generated by ``pyioc init``:

* the PVs, rules and expressions are in ``config.yaml``;
* device access is in ``driver.py`` (``simulate: true`` uses iocmng's simulator);
* Python logic, if you need any, goes in :class:`DeviceIOC`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import threading

import numpy as np
import yaml
from iocmng.base.task import TaskBase
from iocmng.core.plugin_spec import PluginSpec

from .driver import make_driver

log = logging.getLogger(__name__)

CONFIG = Path(__file__).with_name("config.yaml")
MAX_STRING = {EPICS_STRING}   # DBR_STRING holds 40 bytes including the terminator
MAX_ERROR = 240


def padded_chars(text: str, length: int = MAX_ERROR) -> np.ndarray:
    """A char waveform always published full length, NUL padded (Phoebus cannot decode shorter arrays)."""
    data = text.encode(errors="replace")[:length]
    return np.frombuffer(data.ljust(length, b"\\0"), dtype=np.uint8)


class DeviceIOC(TaskBase):
    """One device, <iocprefix>:<device name>:<PV>.

    iocmng reads the driver PVs every poll_period, then every ``interval`` evaluates the
    transforms and rules of config.yaml and calls :meth:`execute`.
    """

    def __init__(self, *args, device, **kwargs):
        self.device = device   # the resolved pyioc Device: its parameters as attributes
        super().__init__(*args, **kwargs)

    def initialize(self) -> None:
        pass

    def execute(self) -> None:
        """Called every ``interval``: Python logic over ``self.link_values`` and ``self.get_pv()``."""

    def cleanup(self) -> None:
        pass

    def create_driver(self):
        if self.device.simulate:
            return super().create_driver()   # parameters.simulate: iocmng's in-memory simulator
        return make_driver(self.device)

    # --- the standard PVs of every epik8s-pyioc device ---------------------------------

    def build_pvs(self) -> None:
        from softioc import builder

        super().build_pvs()   # leaves the builder on this device's prefix
        self.pvs["Connected"] = builder.boolIn("Connected", initial_value=0, ZNAM="Disconnected",
                                               ONAM="Connected", ZSV="MAJOR")
        self.pvs["LastError"] = builder.stringIn("LastError", initial_value="")
        self.pvs["LastErrorLong"] = builder.WaveformIn("LastErrorLong", initial_value=padded_chars(""),
                                                       length=MAX_ERROR, FTVL="CHAR")

    def on_driver_connection(self, connected: bool) -> None:
        self.pvs["Connected"].set(int(connected))

    def on_driver_error(self, message: str) -> None:
        self.pvs["LastError"].set(message[:MAX_STRING])
        self.pvs["LastErrorLong"].set(padded_chars(message))

    def on_driver_recovered(self) -> None:
        self.pvs["LastError"].set("")
        self.pvs["LastErrorLong"].set(padded_chars(""))


def run(instance) -> int:
    """pyioc-run entrypoint: one DeviceIOC per device, all served by this process."""
    from iocmng.core import pv_client
    from softioc import asyncio_dispatcher, builder, softioc

    logging.basicConfig(level=getattr(logging, instance.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    interfaces = [i.strip() for i in instance.interfaces.split(",") if i.strip() and i.strip() != "0.0.0.0"]
    if interfaces:
        os.environ.setdefault("EPICS_CAS_INTF_ADDR_LIST", " ".join(interfaces))
        os.environ.setdefault("EPICS_PVAS_INTF_ADDR_LIST", " ".join(interfaces))
    pv_client.init(pva=str(instance.params.get("link_protocol", "ca")) == "pva")
    config = yaml.safe_load(CONFIG.read_text()) or {{}}
    tasks = []
    for device in instance.devices:
        name = device.name or instance.iocname
        # the values.yaml parameters are seen by iocmng (simulate, poll_period) and by expressions
        parameters = {{**dict(instance.params), **dict(device.params)}}
        spec = PluginSpec.from_config(config=config, parameters_override=parameters, default_prefix=name.upper())
        task = DeviceIOC(name=name, plugin_spec=spec, device=device)
        task.pv_prefix = device.prefix.rstrip(":")
        task.build_pvs()
        tasks.append(task)
        log.info("device %s: prefix %s%s", device.name or "-", device.prefix,
                 " (simulated)" if device.simulate else "")
    builder.LoadDatabase()
    softioc.iocInit(asyncio_dispatcher.AsyncioDispatcher())

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    for task in tasks:
        task.initialize()
        task.start()
    stop.wait()
    for task in tasks:
        task.stop()
    pv_client.close()
    return 0
'''


def _driver_py(d: dict) -> str:
    polled = [p for p in d["pvs"] if p["source"]["kind"] == "driver" and p["access"] in ("ro", "rw")]
    commands = "\n".join(
        f"    {p['name']!r}: ({(p['name'].upper() + '?')!r}, "
        f"{(p['name'].upper() + ' {value}') if p['access'] == 'rw' else None!r}),  # TODO: real command"
        for p in polled)
    actions = "\n".join(f"    {p['name']!r}: {p['name'].upper()!r},  # TODO: real command"
                        for p in d["pvs"] if p["access"] == "cmd")
    kinds = {p["name"]: p["type"] for p in polled}
    head = f'''"""Device access for the {d['name']} IOC.

A driver has connect(), close(), read(names) -> {{name: value}}, write(name, value) and
command(name).  iocmng calls them from one thread per device, one call at a time, so they may
block.  Enum/bool values are indexes (0, 1, ...), waveforms any sequence of numbers.  With
``simulate: true`` iocmng's simulator is used instead and this file is not needed.
"""

from __future__ import annotations

import socket

KINDS = {kinds!r}
'''
    if d["transport"] == "tcp":
        return head + f'''

def make_driver(device):
    if not device.server:
        raise ValueError(f"device {{device.name or device.prefix}}: set server (or simulate: true)")
    return TcpDriver(device.server, device.port, device.timeout)


# PV name -> (query, write format) for the real device; a write format of None means read only.
COMMANDS = {{
{commands}
}}
# cmd PV name -> command string
ACTIONS = {{
{actions}
}}


class TcpDriver:
    """A line-oriented TCP device: send a query, read one reply line.  Adapt query()/parse to it."""

    terminator = b"\\r\\n"   # TODO: the device's line terminator

    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, int(port), float(timeout)
        self._sock: socket.socket | None = None
        self._buffer = b""

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self._buffer = b""

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def query(self, command: str) -> str:
        if self._sock is None:
            raise ConnectionError("not connected")
        self._sock.sendall(command.encode() + self.terminator)
        while self.terminator not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("device closed the connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(self.terminator, 1)
        return line.decode(errors="replace").strip()

    def read(self, names: list[str]) -> dict:
        out = {{}}
        for name in names:
            reply = self.query(COMMANDS[name][0])
            kind = KINDS.get(name)
            if kind == "float":
                out[name] = float(reply)
            elif kind in ("int", "bool", "enum"):
                out[name] = int(reply)
            elif kind == "waveform":
                out[name] = [float(x) for x in reply.split(",") if x.strip()]
            else:
                out[name] = reply
        return out

    def write(self, name: str, value) -> None:
        template = COMMANDS[name][1]
        if template is None:
            raise PermissionError(f"{{name}} is read only")
        self.query(template.format(value=value))   # TODO: most devices acknowledge; check the reply

    def command(self, name: str) -> None:
        self.query(ACTIONS[name])
'''
    return head + '''

def make_driver(device):
    return DeviceDriver(device)


class DeviceDriver:
    """The real device: replace the NotImplementedError bodies (vendor SDK, serial port, ...)."""

    def __init__(self, device):
        self.device = device   # the resolved pyioc Device: its parameters as attributes

    def connect(self) -> None:
        raise NotImplementedError("DeviceDriver.connect: talk to the device here")

    def close(self) -> None:
        pass

    def read(self, names: list[str]) -> dict:
        raise NotImplementedError("DeviceDriver.read")

    def write(self, name: str, value) -> None:
        raise NotImplementedError("DeviceDriver.write")

    def command(self, name: str) -> None:
        raise NotImplementedError("DeviceDriver.command")
'''


def _test_prefix(d: dict) -> str:
    return "TEST:" + re.sub(r"[^A-Z0-9]", "_", d["name"].upper())


def _example_yaml(d: dict) -> str:
    lines = [
        "# A values.yaml-style IOC entry for local runs:",
        "#   pyioc-run example.yaml --check     (validate and show the resolved instance)",
        "#   pyioc-run example.yaml             (start the IOC on the simulator)",
        f"devtype: {d['name']}",
        f"iocname: {d['name']}-test",
        f"iocprefix: {_test_prefix(d)}",
        "simulate: true",
        "poll_period: 0.2",
    ]
    if d["multiple"]:
        lines += ["devices:", "  - name: DEV01"]
    return "\n".join(lines) + "\n"


_TEST_HELPERS = '''
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return predicate()


def _server_env(port: int, **extra) -> dict:
    """A private Channel Access / PV Access server on localhost: tests never see the beamline."""
    return dict(os.environ, EPICS_CA_SERVER_PORT=str(port), EPICS_CAS_INTF_ADDR_LIST="127.0.0.1",
                EPICS_CA_AUTO_ADDR_LIST="NO", EPICS_PVAS_INTF_ADDR_LIST="127.0.0.1",
                EPICS_PVAS_SERVER_PORT=str(_free_port()), EPICS_PVAS_BROADCAST_PORT=str(_free_port()),
                EPICS_PVA_AUTO_ADDR_LIST="NO", **extra)
'''


def _test_value(p: dict):
    if p["type"] in ("bool", "enum"):
        return p["choices"][-1]
    if p["type"] == "string":
        return "test"
    if p["limits"]:
        return p["limits"][0]
    return 2 if p["type"] == "int" else 1.5


def _test_py(d: dict) -> str:
    prefix = _test_prefix(d) + (":DEV01:" if d["multiple"] else ":")
    reads = [p["name"] for p in d["pvs"] if p["access"] != "cmd" and p["source"]["kind"] != "link"]
    writes = []
    for p in d["pvs"]:
        if p["access"] != "rw":
            continue
        value = _test_value(p)
        if p["type"] in ("bool", "enum", "string"):
            writes.append(f"    put({p['name']!r}, {value!r})\n"
                          f"    assert _wait(lambda: get({p['name']!r}, as_string=True) == {value!r})")
        else:
            writes.append(f"    put({p['name']!r}, {value!r})\n"
                          f"    assert _wait(lambda: abs(get({p['name']!r}) - {value!r}) < 1e-6)")
    for p in d["pvs"]:
        if p["access"] == "cmd":
            writes.append(f"    put({p['name']!r}, 1)                 # a command returns to Idle when done\n"
                          f"    assert _wait(lambda: get({p['name']!r}) == 0)")
    return f'''"""The IOC over real Channel Access, on the simulator: start it, read and write its PVs."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
{_TEST_HELPERS}

PORT = _free_port()
# pyepics reads these when it first connects
os.environ.update(EPICS_CA_ADDR_LIST="127.0.0.1", EPICS_CA_AUTO_ADDR_LIST="NO",
                  EPICS_CA_SERVER_PORT=str(PORT))
epics = pytest.importorskip("epics")

EXAMPLE = Path(__file__).resolve().parent.parent / "example.yaml"
P = {prefix!r}


def get(pv, **kwargs):
    return epics.caget(P + pv, timeout=2, **kwargs)


def put(pv, value):
    assert epics.caput(P + pv, value, wait=True, timeout=5) == 1, pv


@pytest.fixture(scope="module", autouse=True)
def ioc():
    proc = subprocess.Popen([sys.executable, "-m", "epik8s_pyioc.runner", str(EXAMPLE)], env=_server_env(PORT))
    try:
        assert _wait(lambda: get("Connected") == 1, 30), "IOC did not start"
        yield
    finally:
        proc.terminate()
        proc.wait(timeout=15)


def test_every_pv_answers():
    for name in {reads!r}:
        assert _wait(lambda: get(name) is not None), name
    assert get("LastError") == ""
    assert get("STATUS", as_string=True) == "RUN"


def test_writes():
{chr(10).join(writes) if writes else "    pass"}
    assert get("Connected") == 1
'''


def _pyproject(d: dict) -> str:
    dist = d["package"].replace("_", "-")
    return f'''[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{dist}"
version = "{d['version']}"
description = "{d['description']}"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
  "{PYIOC_REQUIREMENT}",
  "{IOCMNG_REQUIREMENT}",
  "numpy>=1.24",
]

[project.optional-dependencies]
test = ["pytest>=7"]

# registers pyioc.yaml so `pyioc-run` finds this IOC by its devtype
[project.entry-points."epik8s.pyioc"]
{d['name']} = "{d['package']}"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
{d['package']} = ["pyioc.yaml", "config.yaml"]

[tool.pytest.ini_options]
testpaths = ["tests"]
'''


def _dockerfile(d: dict) -> str:
    return f'''# The shared framework image: Python, pythonSoftIOC, p4p, pyepics, iocmng.
ARG BASE={BASE_IMAGE}
FROM ${{BASE}}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /opt/ioc
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && pyioc check --devtype {d['name']}
LABEL org.epik8s.pyioc.devtypes="{d['name']}" org.epik8s.iocmng.version="{IOCMNG_VERSION}"
# ioc-chart starts it with `start: pyioc-run` (the IOC entry is in /tmp/iocconfig.yaml);
# the pod runs as uid 1000, so nothing here may need root at run time.
HEALTHCHECK NONE
ENTRYPOINT []
CMD ["pyioc-run"]
'''


def _ci(d: dict) -> str:
    return '''name: Test and publish

on:
  push:
    branches: [main]
    tags: ["v*"]
  pull_request:
    branches: [main]

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: "3.11"
      - run: python -m pip install -e '.[test]'
      - run: pyioc check --spec src/*/pyioc.yaml
      - run: pytest -q
      - run: docker build --tag ioc:ci .

  publish:
    if: startsWith(github.ref, 'refs/tags/v')
    needs: test
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v7
      - uses: docker/setup-buildx-action@v4
      - uses: docker/login-action@v4
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - id: meta
        uses: docker/metadata-action@v6
        with:
          images: ghcr.io/${{ github.repository }}
          tags: |
            type=semver,pattern={{version}}
            type=raw,value=latest
      - uses: docker/build-push-action@v7
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
'''


def _device_readme(d: dict) -> str:
    prefix = "<iocprefix>:<device name>:" if d["multiple"] else "<iocprefix>:"
    return f'''# {d['name']}

{d['description']}. A containerized Python EPICS IOC (iocmng on pythonSoftIOC: Channel Access
and PV Access), configured from an epik8s beamline `values.yaml` through
[epik8s-pyioc](https://github.com/infn-epics/epik8s-pyioc).

## PVs

Every device serves `{prefix}<PV>`:

| PV | Type | Access | Value from | Unit | Description |
|---|---|---|---|---|---|
| `Connected` | enum | RO | | | Disconnected / Connected (reconnects by itself) |
| `LastError` | string | RO | | | last error, first 39 characters |
| `LastErrorLong` | char[240] | RO | | | full error text (`caget -S`) |
{_pv_rows(d)}

plus iocmng's `STATUS`, `MESSAGE`, `ENABLE`, `CONNECTED`, `CYCLE_COUNT`, `CLEAR`, `RESET`, `VERSION`.
{_rule_rows(d)}
## Parameters

`pyioc docs --spec src/{d['package']}/pyioc.yaml` lists them. Without hardware set `simulate: true`.

## Run it

```sh
pip install -e '.[test]'
pyioc-run example.yaml --check   # validate the entry
pyioc-run example.yaml           # IOC on the simulator
pytest                           # the same over Channel Access
```

## Deploy (epik8s)

`pyioc defaults --spec src/{d['package']}/pyioc.yaml` prints the `iocDefaults` block for the
beamline `values.yaml` (keyed by `devtype`, `start: pyioc-run`, never `template`). Then:

```yaml
epicsConfiguration:
  iocs:
    {d['name']}01:
      devtype: {d['name']}
      iocprefix: BEAMLINE:{d['devgroup'].upper()}{"" if d["multiple"] else ":DEV01"}
''' + ("      devices:\n        - name: DEV01\n" if d["multiple"] else "") + (
        "          server: 192.168.1.10\n" if d["multiple"] and d["transport"] == "tcp" else
        "      server: 192.168.1.10\n" if d["transport"] == "tcp" else "") + f'''```

`pyioc validate --spec src/{d['package']}/pyioc.yaml deploy/values.yaml` checks the beamline.
Tagging `vX.Y.Z` publishes `{d['image']}:X.Y.Z` (GitHub Actions), built on `{BASE_IMAGE}`.

## Write the driver

`src/{d['package']}/driver.py`: {"fill in the real commands in `COMMANDS`/`ACTIONS` and the reply parsing of `TcpDriver`" if d["transport"] == "tcp" else "implement `DeviceDriver`"}.
PVs, rules and expressions: `src/{d['package']}/config.yaml`. Python logic: `DeviceIOC.execute()` in `ioc.py`.
'''


def _device_files(d: dict) -> dict:
    pkg = f"src/{d['package']}"
    return {
        "pyproject.toml": _pyproject(d),
        "README.md": _device_readme(d),
        "Dockerfile": _dockerfile(d),
        ".dockerignore": ".git\n.venv\n__pycache__\n*.egg-info\n.pytest_cache\n",
        ".gitignore": ".venv/\n__pycache__/\n*.egg-info/\n.pytest_cache/\nbuild/\ndist/\n",
        ".github/workflows/ci.yml": _ci(d),
        "example.yaml": _example_yaml(d),
        f"{pkg}/__init__.py": f'"""{d["description"]}."""\n\n__version__ = "{d["version"]}"\n',
        f"{pkg}/pyioc.yaml": _spec_yaml(d),
        f"{pkg}/config.yaml": _config_yaml(d),
        f"{pkg}/ioc.py": _ioc_py(d),
        f"{pkg}/driver.py": _driver_py(d),
        "tests/test_ioc.py": _test_py(d),
    }


# --- box -----------------------------------------------------------------------------

def _box_module(d: dict) -> tuple[str, str]:
    if d["python"]:
        return d["package"].removesuffix("_ioc") + "_box", d["class_name"]
    return "iocmng.declarative", "DeclarativeTask"


def _start_sh(d: dict) -> str:
    module, cls = _box_module(d)
    return f'''#!/bin/bash
# Rendered by ioc-chart with the softiocs: entry of values.yaml, then run in the
# epik8s-softioc-mng image.
script_dir=$(dirname "$0")
cd $script_dir
echo "Starting {{{{iocname}}}}"
exec iocmng-run \\
  --module {module} \\
  --class-name {cls} \\
  --config config.yaml \\
  --prefix {{{{iocprefix}}}} \\
  --name {{{{iocname}}}} \\
  --pva {{{{pva|default("false")}}}} \\
  --pvout {{{{config_dir}}}}/pvlist.txt \\
  --log-level {{{{log_level|default("INFO")}}}}
'''


def _box_py(d: dict) -> str:
    return f'''"""{d['description']}: config.yaml does the work; Python logic goes here."""

from iocmng import DeclarativeTask


class {d['class_name']}(DeclarativeTask):
    """Transforms and rules come from config.yaml.  Override the hooks for logic they cannot express."""

    def on_input_changed(self, key, value, old_value):
        """Called when a linked PV with `trigger: true` changes."""

    def execute(self):
        """Called every `interval` in continuous mode."""
'''


def _upstream_type(p: dict) -> str:
    return {"float": "aOut", "int": "longOut", "bool": "boolOut", "enum": "mbbOut",
            "string": "stringOut"}[p["type"]]


def _box_test_py(d: dict) -> str:
    module, cls = _box_module(d)
    links = [p for p in d["pvs"] if p["source"]["kind"] == "link"]
    upstream = {p["source"]["link"].split(".")[0]: [_upstream_type(p), p["choices"]] for p in links}
    follow = {p["name"]: [p["source"]["link"].split(".")[0],
                          1 if p["type"] in ("bool", "enum") else _test_value(p)] for p in links}
    cases = []
    for r in d["rules"]:
        names = sorted(_expression_names(r["condition"], "", []))
        inputs = ", ".join(f"{next(p['source']['link'] for p in links if p['name'] == n)!r}: 0"
                           for n in names if any(p["name"] == n for p in links))
        cases.append(f"    # {r['id']!r}: ({{{inputs}}}, {r.get('outputs') or {}!r}),   # {r['condition']}")
    return f'''"""The box over real Channel Access: a stub IOC serves the linked PVs, the box follows them."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
{_TEST_HELPERS}

UPSTREAM_PORT, BOX_PORT = _free_port(), _free_port()
# pyepics reads these when it first connects: it sees both IOCs
os.environ.update(EPICS_CA_ADDR_LIST=f"127.0.0.1:{{UPSTREAM_PORT}} 127.0.0.1:{{BOX_PORT}}",
                  EPICS_CA_AUTO_ADDR_LIST="NO")
epics = pytest.importorskip("epics")

BOX = Path(__file__).resolve().parent.parent
P = "TEST:BOX:"
# linked PV -> [softioc record type, enum choices]: what the stub IOC serves
UPSTREAM = {upstream!r}
# box PV -> [linked PV, value written upstream]
FOLLOW = {follow!r}
# rule id -> (values written to linked PVs, box PVs expected afterwards); fill in:
RULE_CASES = {{
{chr(10).join(cases)}
}}
STUB = """
import json, sys, threading
from softioc import asyncio_dispatcher, builder, softioc
for name, (kind, choices) in json.loads(sys.argv[1]).items():
    getattr(builder, kind)(name, *choices) if kind == "mbbOut" else getattr(builder, kind)(name)
builder.LoadDatabase()
softioc.iocInit(asyncio_dispatcher.AsyncioDispatcher())
threading.Event().wait()
"""


def get(pv, **kwargs):
    return epics.caget(pv, timeout=2, **kwargs)


def put(pv, value):
    assert epics.caput(pv, value, wait=True, timeout=5) == 1, pv


@pytest.fixture(scope="module", autouse=True)
def iocs():
    procs = [subprocess.Popen([sys.executable, "-c", STUB, json.dumps(UPSTREAM)],
                              env=_server_env(UPSTREAM_PORT))] if UPSTREAM else []
    procs.append(subprocess.Popen(
        [sys.executable, "-c", "from iocmng.runner import main; main()", "--module", {module!r}, "--class-name", {cls!r},
         "--config", "config.yaml", "--prefix", P.rstrip(":"), "--name", "box-test", "--pva", "false"],
        cwd=BOX, env=_server_env(BOX_PORT, EPICS_CA_ADDR_LIST=f"127.0.0.1:{{UPSTREAM_PORT}}")))
    try:
        assert _wait(lambda: get(P + "STATUS", as_string=True) == "RUN", 30), "box did not start"
        yield
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=15)


def test_every_pv_answers():
    for name in {[p["name"] for p in d["pvs"]]!r}:
        assert _wait(lambda: get(P + name) is not None), name


@pytest.mark.parametrize("name", sorted(FOLLOW))
def test_links_follow_upstream(name):
    link, value = FOLLOW[name]
    put(link, value)
    if isinstance(value, float):
        assert _wait(lambda: abs(get(P + name) - value) < 1e-6), name
    else:
        assert _wait(lambda: get(P + name, as_string=isinstance(value, str)) == value), name


@pytest.mark.parametrize("rule", sorted(RULE_CASES))
def test_rule(rule):
    writes, expected = RULE_CASES[rule]
    put(P + "RESET", 1)
    for link, value in writes.items():
        put(link, value)
    for name, value in expected.items():
        assert _wait(lambda: get(P + name) == value, 30), f"{{rule}}: {{name}} != {{value}}"
'''


def _box_readme(d: dict) -> str:
    module, cls = _box_module(d)
    params = "".join(f"| `{k}` | `{v!r}` |\n" for k, v in d["parameters"].items())
    params = f"\n## Parameters\n\nIn `config.yaml`, usable in expressions:\n\n| Parameter | Value |\n|---|---|\n{params}" \
        if params else ""
    return f'''# {d['name']}

{d['description']}. A software box: iocmng logic over existing PVs, run by `iocmng-run` in the
shared `ghcr.io/infn-epics/epik8s-softioc-mng` image (no image of its own).

## PVs

`<iocprefix>:<PV>`:

| PV | Type | Access | Value from | Unit | Description |
|---|---|---|---|---|---|
{_pv_rows(d)}

plus iocmng's `STATUS`, `MESSAGE`, `ENABLE`, `CYCLE_COUNT`, `CLEAR`, `RESET`, `VERSION`, and
`CONN_INP` for the links. Mode: `{d['mode']}`{" (rules run when a linked PV changes)" if d["mode"] == "reactive" else f" (rules run every {d['interval']} s)"}.
{_rule_rows(d)}{params}
## Run it

```sh
pip install 'iocmng>={IOCMNG_VERSION}' pytest
iocmng-run --module {module} --class-name {cls} --config config.yaml --prefix TEST:BOX --pva false
pytest tests        # a stub IOC serves the linked PVs; fill in RULE_CASES to test the rules
```

## Deploy (epik8s)

Copy this folder to the beamline repository as `config/softiocs/{d['name']}/` and add the entry:

```yaml
softiocs:
  - name: {d['name']}
    iocprefix: BEAMLINE:{d['devgroup'].upper()}:{re.sub(r"[^A-Z0-9]", "", d['name'].upper())}
    devgroup: {d['devgroup']}
    module: {module}
    className: {cls}
    description: "{d['description']}"
    gitRepoConfig:
      url: <the beamline repository>
      path: config/softiocs/{d['name']}
      branch: main
```

The image must carry iocmng {IOCMNG_VERSION} or later (`ioc_version` in `softiocDefaults`).
'''


def _box_files(d: dict) -> dict:
    files = {
        "README.md": _box_readme(d),
        "config.yaml": _config_yaml(d),
        "start.sh.j2": _start_sh(d),
        "tests/test_box.py": _box_test_py(d),
    }
    if d["python"]:
        files[f"{_box_module(d)[0]}.py"] = _box_py(d)
    return files


# --- the project -------------------------------------------------------------------------

def render_project(description: Mapping[str, Any] | str) -> dict:
    """The project's files as {path: text}, its kind (device or box) and a self-check; raises ScaffoldError."""
    d = normalize(description)
    files = _device_files(d) if d["kind"] == "device" else _box_files(d)
    return {"kind": d["kind"], "files": files, "description": d, "self_check": check_project(files, d)}


def check_project(files: Mapping[str, str], d: dict) -> dict:
    """What can be checked without running anything: specs, the example, the iocmng config, Python syntax."""
    import yaml

    errors, notes = [], []
    config_path = f"src/{d['package']}/config.yaml" if d["kind"] == "device" else "config.yaml"
    if d["kind"] == "device":
        from .instance import InstanceError, resolve_instance
        from .spec import SpecError, parse_spec

        try:
            spec = parse_spec(yaml.safe_load(files[f"src/{d['package']}/pyioc.yaml"]))
            instance = resolve_instance(spec, yaml.safe_load(files["example.yaml"]))
            notes.append(f"example.yaml resolves: {', '.join(dev.prefix for dev in instance.devices)}")
        except (SpecError, InstanceError) as exc:
            errors.append(f"pyioc.yaml/example.yaml: {exc}")
    config = yaml.safe_load(files[config_path]) or {}
    arguments = config.get("arguments") or {}
    declared = set(arguments.get("inputs") or {}) | set(arguments.get("outputs") or {})
    for p in d["pvs"]:
        if p["name"] not in declared:
            errors.append(f"PV {p['name']} missing from {config_path}")
    try:   # iocmng itself, when installed here: the same parser the IOC uses
        from iocmng.core.plugin_spec import PluginSpec
    except ImportError:
        notes.append(f"iocmng is not installed: {config_path} checked by the generator only")
    else:
        try:
            PluginSpec.from_config(config)
            notes.append(f"{config_path} parses with iocmng")
        except ValueError as exc:
            errors.append(f"{config_path}: {exc}")
    for path, text in files.items():
        if path.endswith(".py"):
            try:
                compile(text, path, "exec")
            except SyntaxError as exc:
                errors.append(f"{path}:{exc.lineno}: {exc.msg}")
    return {"ok": not errors, "errors": errors, "notes": notes}


DEFAULT_DEVICE = {"pvs": [
    {"name": "Value", "type": "float", "access": "ro", "unit": "", "precision": 3},
    {"name": "Setpoint", "type": "float", "access": "rw", "precision": 3},
    {"name": "Enable", "type": "bool", "access": "rw", "choices": ["Off", "On"]},
    {"name": "Reset", "type": "bool", "access": "cmd"},
]}


def scaffold(name: str, directory: Path, package: str | None = None, image: str = "",
             devgroup: str = "", description: Mapping[str, Any] | None = None, box: bool = False) -> list[Path]:
    """Write a new project to ``directory`` (``pyioc init``); refuses to overwrite files."""
    if description is None:
        description = {k: v for k, v in EXAMPLE_BOX.items() if k not in ("name", "description")} if box \
            else DEFAULT_DEVICE
    spec = dict(description)
    spec.setdefault("name", name)
    if package:
        spec["package"] = package
    if image:
        spec["image"] = image
    if devgroup:
        spec["devgroup"] = devgroup
    project = render_project(spec)
    if not project["self_check"]["ok"]:
        raise ScaffoldError(project["self_check"]["errors"])
    targets = {directory / path: text for path, text in project["files"].items()}
    existing = [str(p) for p in targets if p.exists()]
    if existing:
        raise FileExistsError(", ".join(existing))
    for path, text in targets.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return list(targets)


__all__ = ["EXAMPLE", "EXAMPLE_BOX", "ScaffoldError", "check_project", "normalize", "render_project", "scaffold"]
