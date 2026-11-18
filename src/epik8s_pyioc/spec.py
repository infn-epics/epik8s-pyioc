"""The ``pyioc.yaml`` support definition of a containerized Python IOC."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from pathlib import Path
import re
from typing import Any, Mapping

SPEC_VERSION = 1

TYPES = ("string", "int", "float", "bool", "enum", "host", "list", "map", "any")

# Keys interpreted by epik8s-chart / ioc-chart (or written by them into
# /tmp/iocconfig.yaml).  A parameter with one of these names would change how
# the IOC is deployed, so a spec may not declare them.
RESERVED_KEYS = frozenset({
    "name", "iocname", "iocprefix", "iocroot", "template", "devtype", "devgroup", "devfunc",
    "devices", "disable", "image", "ioc_version", "imagePullPolicy", "start", "stop", "liveness",
    "host", "serial", "iocparam", "iocinit", "gitinit", "gitpath", "gitrev", "iocdir", "workdir",
    "gitRepoConfig", "nfsMounts", "env", "globalenv", "securityContext", "resources",
    "hostNetwork", "staticIP", "staticIp", "service", "ingress", "enable_ingress", "pva",
    "autosync", "charturl", "targetRevision", "beamline", "epicsConfiguration", "usegateway",
    "config_dir", "data_dir", "autosave_dir", "ioctop", "iocConfig", "iocFolder", "zones",
    "asset", "opi", "carepeater", "nocheckconn", "python_env", "dataVolume",
})
# keys every ``devices`` entry may carry besides the declared device parameters
DEVICE_BUILTIN_KEYS = frozenset({"name", "prefix", "disable", "nocheckconn"})

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}
_UNSET = object()


class SpecError(ValueError):
    """The pyioc.yaml support definition itself is invalid."""


class ParamError(ValueError):
    """A parameter value in an IOC instance is invalid."""


@dataclass
class Param:
    name: str
    type: str = "string"
    description: str = ""
    default: Any = _UNSET
    required: bool = False
    choices: list[str] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    pattern: str | None = None
    unit: str = ""
    aliases: list[str] = field(default_factory=list)
    example: Any = _UNSET

    @property
    def has_default(self) -> bool:
        return self.default is not _UNSET

    def keys(self) -> list[str]:
        return [self.name, *self.aliases]

    def coerce(self, value: Any) -> Any:
        """Convert and validate a value from YAML (strings from ``iocparam`` included)."""
        kind = self.type
        try:
            if kind == "string":
                if isinstance(value, (dict, list)):
                    raise ParamError("expected a string")
                value = str(value)
            elif kind == "host":
                value = str(value).strip()
                if not value or any(c.isspace() for c in value):
                    raise ParamError(f"invalid host {value!r}")
                try:
                    ipaddress.ip_address(value)
                except ValueError:
                    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?", value):
                        raise ParamError(f"invalid host {value!r}") from None
            elif kind == "int":
                if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                    raise ParamError(f"expected an integer, got {value!r}")
                value = int(str(value).strip(), 0) if isinstance(value, str) else int(value)
            elif kind == "float":
                if isinstance(value, bool):
                    raise ParamError(f"expected a number, got {value!r}")
                value = float(value)
            elif kind == "bool":
                value = to_bool(value)
            elif kind == "enum":
                text = str(value).strip()
                match = next((c for c in self.choices if c.lower() == text.lower()), None)
                if match is None:
                    raise ParamError(f"{value!r} is not one of {', '.join(self.choices)}")
                value = match
            elif kind == "list":
                if not isinstance(value, list):
                    raise ParamError("expected a list")
            elif kind == "map":
                if not isinstance(value, Mapping):
                    raise ParamError("expected a mapping")
                value = dict(value)
        except ParamError:
            raise
        except (TypeError, ValueError) as exc:
            raise ParamError(f"invalid {kind} value {value!r}") from exc
        if kind in ("int", "float"):
            if self.min is not None and value < self.min:
                raise ParamError(f"{value} is below the minimum {self.min}")
            if self.max is not None and value > self.max:
                raise ParamError(f"{value} is above the maximum {self.max}")
        if self.pattern and kind in ("string", "host") and not re.fullmatch(self.pattern, value):
            raise ParamError(f"{value!r} does not match {self.pattern!r}")
        return value


@dataclass
class DeviceSection:
    multiple: bool = True
    prefix: str = "{iocprefix}:{name}:"
    single_prefix: str = "{iocprefix}:"
    parameters: dict[str, Param] = field(default_factory=dict)


@dataclass
class Spec:
    name: str
    entrypoint: str
    description: str = ""
    devgroup: str = ""
    image: str = ""
    version: str = ""
    protocols: list[str] = field(default_factory=lambda: ["ca"])
    docs: str = ""
    parameters: dict[str, Param] = field(default_factory=dict)
    devices: DeviceSection | None = None
    path: Path | None = None
    raw: dict = field(default_factory=dict)

    def all_param_names(self) -> set[str]:
        names: set[str] = set()
        for param in [*self.parameters.values(), *self.device_params().values()]:
            names.update(param.keys())
        return names

    def device_params(self) -> dict[str, Param]:
        return self.devices.parameters if self.devices else {}


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ParamError(f"cannot interpret {value!r} as a boolean")


def _parse_param(name: str, data: Any, where: str) -> Param:
    if isinstance(data, str):
        data = {"type": data}
    if not isinstance(data, Mapping):
        raise SpecError(f"{where}.{name}: must be a mapping or a type name")
    unknown = set(data) - {"type", "description", "default", "required", "choices", "min", "max",
                           "pattern", "unit", "aliases", "example"}
    if unknown:
        raise SpecError(f"{where}.{name}: unknown field(s) {', '.join(sorted(unknown))}")
    choices = [str(c) for c in data.get("choices") or []]
    kind = str(data.get("type") or ("enum" if choices else "string"))
    if kind not in TYPES:
        raise SpecError(f"{where}.{name}: unknown type {kind!r} (one of {', '.join(TYPES)})")
    if kind == "enum" and not choices:
        raise SpecError(f"{where}.{name}: enum needs 'choices'")
    aliases = data.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    param = Param(
        name=name, type=kind, description=str(data.get("description") or ""),
        default=data.get("default", _UNSET), required=bool(data.get("required", False)),
        choices=choices, min=data.get("min"), max=data.get("max"), pattern=data.get("pattern"),
        unit=str(data.get("unit") or ""), aliases=[str(a) for a in aliases],
        example=data.get("example", _UNSET),
    )
    if param.has_default and param.default is not None:
        try:
            param.default = param.coerce(param.default)
        except ParamError as exc:
            raise SpecError(f"{where}.{name}: invalid default: {exc}") from exc
    return param


def _parse_params(data: Any, where: str) -> dict[str, Param]:
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise SpecError(f"{where}: must be a mapping of parameter definitions")
    params = {}
    for name, definition in data.items():
        name = str(name)
        params[name] = _parse_param(name, definition, where)
    return params


def parse_spec(data: Mapping[str, Any], path: Path | None = None) -> Spec:
    if not isinstance(data, Mapping):
        raise SpecError("pyioc.yaml must be a mapping")
    version = data.get("pyioc")
    if version != SPEC_VERSION:
        raise SpecError(f"'pyioc: {SPEC_VERSION}' is required (spec format version), got {version!r}")
    unknown = set(data) - {"pyioc", "name", "description", "devgroup", "image", "version",
                           "entrypoint", "protocols", "docs", "parameters", "devices"}
    if unknown:
        raise SpecError(f"unknown top-level field(s): {', '.join(sorted(unknown))}")
    name = str(data.get("name") or "")
    if not _NAME_RE.match(name):
        raise SpecError("'name' (the devtype used in values.yaml) is required: letters, digits, _ . -")
    entrypoint = str(data.get("entrypoint") or "")
    if not re.fullmatch(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*", entrypoint):
        raise SpecError("'entrypoint' must be 'package.module:function'")
    protocols = data.get("protocols") or ["ca"]
    if isinstance(protocols, str):
        protocols = [protocols]
    if not set(protocols) <= {"ca", "pva"}:
        raise SpecError("'protocols' entries must be 'ca' or 'pva'")

    devices = None
    if data.get("devices") is not None:
        section = data["devices"]
        if not isinstance(section, Mapping):
            raise SpecError("'devices' must be a mapping")
        unknown = set(section) - {"multiple", "prefix", "single_prefix", "parameters"}
        if unknown:
            raise SpecError(f"devices: unknown field(s) {', '.join(sorted(unknown))}")
        devices = DeviceSection(
            multiple=bool(section.get("multiple", True)),
            prefix=str(section.get("prefix") or DeviceSection.prefix),
            single_prefix=str(section.get("single_prefix") or DeviceSection.single_prefix),
            parameters=_parse_params(section.get("parameters"), "devices.parameters"),
        )
    spec = Spec(
        name=name, entrypoint=entrypoint, description=str(data.get("description") or ""),
        devgroup=str(data.get("devgroup") or ""), image=str(data.get("image") or ""),
        version=str(data.get("version") or ""), protocols=list(protocols),
        docs=str(data.get("docs") or ""),
        parameters=_parse_params(data.get("parameters"), "parameters"),
        devices=devices, path=path, raw=dict(data),
    )
    _check_names(spec)
    return spec


def _check_names(spec: Spec) -> None:
    seen: dict[str, str] = {}
    groups = [("parameters", spec.parameters), ("devices.parameters", spec.device_params())]
    for where, params in groups:
        for param in params.values():
            for key in param.keys():
                if not _PARAM_RE.match(key):
                    raise SpecError(f"{where}.{param.name}: invalid key name {key!r}")
                if key in RESERVED_KEYS:
                    raise SpecError(f"{where}.{param.name}: {key!r} is reserved by epik8s/ioc-chart")
                if key in seen:
                    raise SpecError(f"{where}.{param.name}: key {key!r} already used by {seen[key]}")
                seen[key] = f"{where}.{param.name}"
    for template in (spec.devices.prefix, spec.devices.single_prefix) if spec.devices else ():
        try:
            template.format(iocprefix="X", name="Y", iocname="Z")
        except (KeyError, IndexError, ValueError) as exc:
            raise SpecError(f"devices prefix template {template!r}: {exc}") from exc


def load_spec(path: str | Path) -> Spec:
    import yaml

    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as exc:
        raise SpecError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML in {path}: {exc}") from exc
    return parse_spec(data or {}, path)
