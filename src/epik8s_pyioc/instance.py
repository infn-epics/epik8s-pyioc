"""Resolve an IOC instance (a values.yaml IOC entry) against its pyioc.yaml spec."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

from .spec import DEVICE_BUILTIN_KEYS, Param, ParamError, Spec, to_bool


class InstanceError(ValueError):
    """An IOC instance does not satisfy its spec.  ``errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class _Params(Mapping[str, Any]):
    """Read-only parameters, accessible as ``obj["key"]`` or ``obj.key``."""

    params: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.params[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.params)

    def __len__(self) -> int:
        return len(self.params)

    def __getattr__(self, key: str) -> Any:
        try:
            return self.__dict__["params"][key]
        except KeyError:
            raise AttributeError(key) from None


@dataclass(eq=False)
class Device(_Params):
    name: str
    prefix: str
    params: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(eq=False)
class Instance(_Params):
    spec: Spec = field(repr=False)
    iocname: str
    iocprefix: str
    params: dict[str, Any]
    devices: list[Device]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    warnings: list[str] = field(default_factory=list)
    config_path: Path | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "spec": self.spec.name,
            "iocname": self.iocname,
            "iocprefix": self.iocprefix,
            "params": self.params,
            "devices": [{"name": d.name, "prefix": d.prefix, "params": d.params} for d in self.devices],
        }


def _lookup(param: Param, sources: list[Mapping[str, Any]]) -> tuple[bool, Any]:
    """First non-empty value for the parameter (or an alias); later sources win."""
    for source in reversed(sources):
        for key in param.keys():
            if key in source and source[key] is not None and source[key] != "":
                return True, source[key]
    return False, None


def _resolve(params: Mapping[str, Param], sources: list[Mapping[str, Any]], where: str,
             errors: list[str]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for name, param in params.items():
        found, value = _lookup(param, sources)
        if not found:
            if param.required:
                errors.append(f"{where}{name}: required parameter is missing")
                continue
            value = param.default if param.has_default else None
            resolved[name] = value
            continue
        try:
            resolved[name] = param.coerce(value)
        except ParamError as exc:
            errors.append(f"{where}{name}: {exc}")
    return resolved


def _format_prefix(template: str, iocprefix: str, name: str, iocname: str) -> str:
    return template.format(iocprefix=iocprefix.rstrip(":"), name=name, iocname=iocname)


def resolve_instance(spec: Spec, config: Mapping[str, Any], config_path: Path | None = None) -> Instance:
    if not isinstance(config, Mapping):
        raise InstanceError(["the IOC configuration must be a mapping"])
    errors: list[str] = []
    warnings: list[str] = []
    iocname = str(config.get("iocname") or config.get("name") or "")
    iocprefix = str(config.get("iocprefix") or "")
    if not iocprefix:
        errors.append("iocprefix: required")
    devtype = config.get("devtype")
    if devtype and devtype != spec.name:
        warnings.append(f"devtype {devtype!r} differs from spec name {spec.name!r}")

    params = _resolve(spec.parameters, [config], "", errors)

    devices: list[Device] = []
    entries = config.get("devices")
    section = spec.devices
    if section is None:
        if entries:
            errors.append(f"devices: {spec.name} does not support a devices list")
    elif entries:
        if not section.multiple:
            errors.append(f"devices: {spec.name} supports a single device; set its parameters at top level")
        elif not isinstance(entries, list):
            errors.append("devices: must be a list")
        else:
            known = DEVICE_BUILTIN_KEYS | {k for p in section.parameters.values() for k in p.keys()}
            # ioc-chart probes server/ip + port with socat, so they are legal on any device entry
            known |= {"server", "ip", "port"}
            for index, entry in enumerate(entries):
                where = f"devices[{index}]."
                if not isinstance(entry, Mapping):
                    errors.append(f"devices[{index}]: must be a mapping")
                    continue
                try:
                    if to_bool(entry.get("disable", False)):
                        continue
                except ParamError as exc:
                    errors.append(f"{where}disable: {exc}")
                    continue
                name = str(entry.get("name") or "")
                if not name and not entry.get("prefix"):
                    errors.append(f"devices[{index}]: 'name' (or an explicit 'prefix') is required")
                    continue
                for key in sorted(set(entry) - known):
                    warnings.append(f"{where}{key}: not a parameter of {spec.name}, ignored")
                values = _resolve(section.parameters, [config, entry], f"devices[{name or index}].", errors)
                prefix = str(entry.get("prefix") or _format_prefix(section.prefix, iocprefix, name, iocname))
                devices.append(Device(name=name, prefix=prefix, params=values, raw=dict(entry)))
            if not devices and not errors:
                errors.append("devices: every device is disabled")
    else:
        values = _resolve(section.parameters, [config], "", errors)
        prefix = _format_prefix(section.single_prefix, iocprefix, "", iocname)
        devices.append(Device(name="", prefix=prefix, params=values, raw={}))

    prefixes = [device.prefix for device in devices]
    duplicates = sorted({p for p in prefixes if prefixes.count(p) > 1})
    if duplicates:
        errors.append(f"devices: duplicate PV prefixes {', '.join(duplicates)}")
    if errors:
        raise InstanceError(errors)
    return Instance(spec=spec, iocname=iocname, iocprefix=iocprefix, params=params, devices=devices,
                    raw=dict(config), warnings=warnings, config_path=config_path)


def load_instance(spec: Spec, path: str | Path) -> Instance:
    import yaml

    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise InstanceError([f"cannot load {path}: {exc}"]) from exc
    return resolve_instance(spec, data, path)
