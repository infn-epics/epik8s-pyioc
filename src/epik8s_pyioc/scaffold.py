"""Generate a complete containerized Python IOC project from a short device description.

Used by ``pyioc init`` and by ARGUS's IOC engineer (``ioc_scaffold_pyioc``), so both produce the
same project.  The generated IOC carries what earlier IOCs learned the hard way:

* enum PVs arrive in putters as strings ("Off", "Closed"): values are compared by index
  (``enum_index``), never by truthiness;
* waveforms are always published full length (NaN padded), because Phoebus' CA client (CAJ)
  cannot decode arrays shorter than their declared length;
* identifiers and short texts are ``DBR_STRING`` (readable with a plain ``caget``), the full
  error text is a NUL-padded char waveform;
* a repeated error is logged with its traceback once, not on every poll;
* driver calls run outside the Channel Access event loop, and a lost device is reconnected.

Description (YAML or a dict)::

    name: tpg366                      # devtype in values.yaml (required)
    description: Pfeiffer TPG366 gauge controller
    devgroup: vac
    image: ghcr.io/infn-epics/tpg366  # default ghcr.io/infn-epics/<name>
    transport: tcp                    # tcp (socket driver skeleton) | none
    default_port: 8000
    multiple: true                    # allow a `devices:` list
    parameters: {}                    # extra IOC-wide pyioc parameters
    device_parameters: {}             # extra per-device pyioc parameters
    pvs:
      - {name: Pressure, type: float, access: ro, unit: mbar, precision: 3}
      - {name: Unit, type: enum, access: rw, choices: [mbar, Torr, Pa]}
      - {name: Trace, type: waveform, access: ro, length: 1024}
      - {name: Reset, type: bool, access: cmd}
"""

from __future__ import annotations

from pathlib import Path
import re
import textwrap
from typing import Any, Mapping

PYIOC_VERSION = "0.2.1"
PYIOC_REQUIREMENT = (f"epik8s-pyioc @ https://github.com/infn-epics/epik8s-pyioc/archive/refs/tags/"
                     f"v{PYIOC_VERSION}.tar.gz")

PV_TYPES = ("float", "int", "bool", "enum", "string", "waveform")
ACCESS = ("ro", "rw", "cmd")
TRANSPORTS = ("tcp", "none")
BUILTIN_PVS = {"Connected", "LastError", "LastErrorLong"}
EPICS_STRING = 39
EXAMPLE = {
    "name": "tpg366", "description": "Pfeiffer TPG366 gauge controller", "devgroup": "vac",
    "transport": "tcp", "default_port": 8000,
    "pvs": [{"name": "Pressure", "type": "float", "access": "ro", "unit": "mbar", "precision": 3},
            {"name": "Unit", "type": "enum", "access": "rw", "choices": ["mbar", "Torr", "Pa"]},
            {"name": "Reset", "type": "bool", "access": "cmd"}],
}


class ScaffoldError(ValueError):
    """The device description cannot become a project; ``errors`` lists every problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --- the description ------------------------------------------------------------------

def _snake(name: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    s = re.sub(r"\W+", "_", s).strip("_").lower()
    return s if s and not s[0].isdigit() else f"pv_{s}"


def normalize(description: Mapping[str, Any] | str) -> dict:
    """Validate a device description and fill in the defaults."""
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
             "multiple", "parameters", "device_parameters", "pvs", "protocols"}
    for key in sorted(set(description) - known):
        errors.append(f"{key}: unknown field (known: {', '.join(sorted(known))})")
    name = str(description.get("name") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        errors.append("name: required, lower case letters, digits, '_' or '-' (it is the devtype)")
    package = str(description.get("package") or re.sub(r"\W", "_", name) + "_ioc")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", package):
        errors.append(f"package: {package!r} is not a Python package name")
    transport = str(description.get("transport") or "tcp")
    if transport not in TRANSPORTS:
        errors.append(f"transport: one of {', '.join(TRANSPORTS)}")
    try:
        default_port = int(description.get("default_port") or 5025)
        if not 1 <= default_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("default_port: a TCP port number")
        default_port = 5025
    protocols = description.get("protocols") or ["ca"]
    if protocols != ["ca"]:
        errors.append("protocols: the generated IOC is caproto, Channel Access only: [ca]")

    pvs, seen = [], set()
    raw_pvs = description.get("pvs") or []
    if not isinstance(raw_pvs, list) or not raw_pvs:
        errors.append("pvs: a non-empty list of {name, type, access, ...}")
        raw_pvs = []
    for i, raw in enumerate(raw_pvs):
        where = f"pvs[{i}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{where}: a mapping")
            continue
        unknown = set(raw) - {"name", "type", "access", "unit", "precision", "choices", "length", "limits",
                              "description", "default"}
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
        pv_name = str(raw.get("name") or "")
        where = f"pvs[{i}] {pv_name}".rstrip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", pv_name):
            errors.append(f"{where}: name must start with a letter (letters, digits, '_')")
            continue
        if pv_name in BUILTIN_PVS:
            errors.append(f"{where}: {pv_name} is generated for every device")
        if pv_name.lower() in seen:
            errors.append(f"{where}: duplicate name")
        seen.add(pv_name.lower())
        kind = str(raw.get("type") or "float")
        access = str(raw.get("access") or "ro")
        if kind not in PV_TYPES:
            errors.append(f"{where}: type one of {', '.join(PV_TYPES)}")
        if access not in ACCESS:
            errors.append(f"{where}: access one of {', '.join(ACCESS)} (cmd = a write-only action)")
        choices = [str(c) for c in raw.get("choices") or []]
        if kind == "bool":
            choices = choices or ["Off", "On"]
            if len(choices) != 2:
                errors.append(f"{where}: a bool has two states (choices: [Off, On])")
        if kind == "enum" and not 2 <= len(choices) <= 16:
            errors.append(f"{where}: an enum needs 2..16 choices")
        if any(len(c) > 25 for c in choices):
            errors.append(f"{where}: EPICS enum strings are at most 25 characters")
        if access == "cmd" and kind not in ("bool", "enum"):
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
                errors.append(f"{where}: waveforms are read-only in the scaffold")
        limits = raw.get("limits")
        if limits is not None and (not isinstance(limits, list) or len(limits) != 2):
            errors.append(f"{where}: limits is [low, high]")
            limits = None
        pvs.append({
            "name": pv_name, "attr": _snake(pv_name), "type": kind, "access": access,
            "unit": str(raw.get("unit") or ""), "precision": raw.get("precision"),
            "choices": choices if kind == "enum" else (choices if kind == "bool" else []),
            "length": length if kind == "waveform" else None, "limits": limits,
            "description": str(raw.get("description") or ""), "default": raw.get("default"),
        })
    attrs = [p["attr"] for p in pvs]
    for attr in {a for a in attrs if attrs.count(a) > 1}:
        errors.append(f"pvs: names map to the same Python attribute {attr!r}")
    for p in pvs:
        if p["attr"] in {"connected", "last_error", "last_error_long", "driver", "device", "run"}:
            errors.append(f"pvs: {p['name']} clashes with a generated attribute ({p['attr']})")

    from .spec import SpecError, _parse_params, RESERVED_KEYS
    extra = {}
    for key in ("parameters", "device_parameters"):
        try:
            extra[key] = _parse_params(description.get(key), key)
        except SpecError as exc:
            errors.append(str(exc))
            extra[key] = {}
        for pname in extra[key]:
            if pname in RESERVED_KEYS:
                errors.append(f"{key}.{pname}: reserved by epik8s/ioc-chart")
            if pname in {"interfaces", "log_level", "simulate", "poll_period", "server", "ip", "port", "timeout"}:
                errors.append(f"{key}.{pname}: generated by the scaffold already")
    if errors:
        raise ScaffoldError(errors)
    return {
        "name": name, "package": package, "description": str(description.get("description") or f"{name} IOC"),
        "devgroup": str(description.get("devgroup") or "io"),
        "image": str(description.get("image") or f"ghcr.io/infn-epics/{name}"),
        "version": str(description.get("version") or "0.1.0"), "transport": transport,
        "default_port": default_port, "multiple": bool(description.get("multiple", True)),
        "parameters": description.get("parameters") or {},
        "device_parameters": description.get("device_parameters") or {},
        "pvs": pvs,
    }


# --- generated files -------------------------------------------------------------------

def _yaml_dump(data: Any) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


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
    spec = {
        "pyioc": 1,
        "name": d["name"],
        "description": d["description"],
        "devgroup": d["devgroup"],
        "image": d["image"],
        "version": d["version"],
        "entrypoint": f"{d['package']}.ioc:run",
        "protocols": ["ca"],
        "parameters": {
            "interfaces": {"type": "string", "default": "0.0.0.0",
                           "description": "Comma-separated IOC listen addresses"},
            "log_level": {"type": "enum", "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
                          "default": "INFO", "aliases": ["loglevel"]},
            **d["parameters"],
        },
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


def _pv_line(p: dict) -> str:
    args = []
    ro = p["access"] == "ro"
    kind = p["type"]
    if kind == "float":
        args += [f"value={float(p['default'] or 0.0)!r}", "dtype=float"]
    elif kind == "int":
        args += [f"value={int(p['default'] or 0)!r}", "dtype=int"]
    elif kind in ("bool", "enum"):
        choices = ["Idle", p["name"]] if p["access"] == "cmd" else p["choices"]
        default = 0
        if p["default"] is not None and p["access"] != "cmd":
            default = choices.index(str(p["default"])) if str(p["default"]) in choices else int(bool(p["default"]))
        args += [f"value={default}", "dtype=ChannelType.ENUM", f"enum_strings={tuple(choices)!r}"]
    elif kind == "string":
        args += [f"value={str(p['default'] or '')[:EPICS_STRING]!r}", "dtype=ChannelType.STRING"]
    elif kind == "waveform":
        args += [f"value=np.full({p['length']}, np.nan)", "dtype=float", f"max_length={p['length']}"]
    args.append(f"name={p['name']!r}")
    if ro:
        args.append("read_only=True")
    if p["unit"]:
        args.append(f"units={p['unit']!r}")
    if p["precision"] is not None and kind in ("float", "waveform"):
        args.append(f"precision={int(p['precision'])}")
    if p["limits"] and kind in ("float", "int"):
        args += [f"lower_ctrl_limit={p['limits'][0]!r}", f"upper_ctrl_limit={p['limits'][1]!r}"]
    if p["description"]:
        args.append(f"doc={p['description']!r}")
    return f"    {p['attr']} = pvproperty({', '.join(args)})"


def _ioc_py(d: dict) -> str:
    pv_lines = "\n".join(_pv_line(p) for p in d["pvs"])
    polled = [p for p in d["pvs"] if p["access"] in ("ro", "rw")]
    table = "\n".join(f"    ({p['name']!r}, {p['attr']!r}, {p['type']!r}, {p['length']!r}),"
                      for p in polled)
    putters = []
    for p in d["pvs"]:
        if p["access"] == "rw":
            putters.append(textwrap.dedent(f"""
                @{p['attr']}.putter
                async def {p['attr']}(self, instance, value):
                    return await self._put({p['name']!r}, {p['type']!r}, instance, value)
            """))
        elif p["access"] == "cmd":
            putters.append(textwrap.dedent(f"""
                @{p['attr']}.putter
                async def {p['attr']}(self, instance, value):
                    if enum_index(instance, value) == 1:
                        await self._call(self.driver.command, {p['name']!r})
                    return 0
            """))
    putter_text = textwrap.indent("".join(putters), "    ")
    defaults = {p["name"]: p["default"] for p in d["pvs"] if p["access"] in ("ro", "rw")}
    tcp = d["transport"] == "tcp"
    make_driver = (
        "    if device.simulate:\n"
        "        return SimulatedDriver(DEFAULTS)\n"
        + ("    if not device.server:\n"
           "        raise ValueError(f\"device {device.name or device.prefix}: set server (or simulate: true)\")\n"
           "    return TcpDriver(device.server, device.port, device.timeout)\n" if tcp else
           "    return DeviceDriver(device)\n")
    )
    return f'''"""{d['description']} — caproto Channel Access IOC.

Started by ``pyioc-run`` (epik8s-pyioc): the values.yaml IOC entry is validated against
pyioc.yaml and handed to :func:`run` as an ``Instance``.  Generated by ``pyioc init``; the
PV definitions below are yours to edit, the machinery at the bottom rarely needs to change.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
from caproto import ChannelType
from caproto.server import PVGroup, pvproperty, run as run_server

from .driver import {"DeviceDriver, " if not tcp else ""}SimulatedDriver{", TcpDriver" if tcp else ""}

log = logging.getLogger(__name__)

MAX_STRING = {EPICS_STRING}   # DBR_STRING holds 40 bytes including the terminator
MAX_ERROR = 240
# (PV name, attribute, type, waveform length) of every PV read from the device each poll
POLLED = [
{table}
]
# start values of the simulator
DEFAULTS = {defaults!r}


def enum_index(prop, value) -> int:
    """Index of an enum value: caproto hands putters strings ("Off"), seeds ints; never use truthiness."""
    if isinstance(value, str):
        return list(prop.enum_strings).index(value)
    return int(value)


def padded_text(text: str, length: int = MAX_ERROR) -> str:
    return text[:length].ljust(length, "\\0")


class DevicePVs(PVGroup):
    """The PVs of one device: <iocprefix>:<device name>:<PV>."""

    connected = pvproperty(value=0, dtype=ChannelType.ENUM, enum_strings=("Disconnected", "Connected"),
                           read_only=True, name="Connected")
    last_error = pvproperty(value="", dtype=ChannelType.STRING, read_only=True, name="LastError")
    # full text, NUL padded to its declared length (Phoebus cannot decode shorter arrays)
    last_error_long = pvproperty(value="\\0" * MAX_ERROR, max_length=MAX_ERROR, read_only=True,
                                 name="LastErrorLong")

{pv_lines}

    def __init__(self, *args, driver, period: float, **kwargs):
        self.driver = driver
        self.period = period
        self._lock: asyncio.Lock | None = None
        self._last_logged_error: str | None = None
        super().__init__(*args, **kwargs)
{putter_text}
    # --- machinery -----------------------------------------------------------------

    @connected.startup
    async def connected(self, instance, async_lib):
        self._lock = asyncio.Lock()
        asyncio.create_task(self._poll())

    async def _poll(self):
        while True:
            try:
                if enum_index(self.connected, self.connected.value) != 1:
                    await asyncio.to_thread(self.driver.connect)
                    await self.connected.write(value=1)
                    log.info("%s connected", self.prefix)
                async with self._lock:
                    values = await asyncio.to_thread(self.driver.read, [name for name, *_ in POLLED])
                for name, attr, kind, length in POLLED:
                    if name in values and values[name] is not None:
                        await self._publish(getattr(self, attr), kind, length, values[name])
                await self._clear_error()
            except Exception as exc:
                await self._record_error(exc, "device communication failed")
                if enum_index(self.connected, self.connected.value) == 1:
                    await self.connected.write(value=0)
                    await asyncio.to_thread(self.driver.close)
            await asyncio.sleep(self.period)

    async def _publish(self, prop, kind, length, value):
        if kind == "waveform":
            array = np.full(length, np.nan)        # always full length
            data = np.asarray(value, dtype=float)[:length]
            array[:data.size] = data
            value = array
        elif kind in ("bool", "enum"):
            value = enum_index(prop, value)
        elif kind == "string":
            value = str(value)[:MAX_STRING]
        if kind == "waveform" or value != prop.value:
            await prop.write(value=value, verify_value=False)

    async def _put(self, name, kind, instance, value):
        sent = enum_index(instance, value) if kind in ("bool", "enum") else value
        await self._call(self.driver.write, name, sent)
        return value

    async def _call(self, function, *args):
        if enum_index(self.connected, self.connected.value) != 1:
            raise RuntimeError("device is not connected")
        async with self._lock:
            try:
                return await asyncio.to_thread(function, *args)
            except Exception as exc:
                await self._record_error(exc)
                raise

    async def _record_error(self, exc: Exception, context: str | None = None):
        """Publish an error; log it with its traceback only when it changes."""
        message = str(exc) or type(exc).__name__
        await self.last_error.write(value=message[:MAX_STRING])
        await self.last_error_long.write(value=padded_text(message))
        if context and f"{{context}}: {{message}}" != self._last_logged_error:
            log.error("%s %s", self.prefix, context, exc_info=exc)
            self._last_logged_error = f"{{context}}: {{message}}"

    async def _clear_error(self):
        if self.last_error.value:
            await self.last_error.write(value="")
            await self.last_error_long.write(value=padded_text(""))
        if self._last_logged_error is not None:
            log.info("%s recovered after: %s", self.prefix, self._last_logged_error)
            self._last_logged_error = None


def make_driver(device):
{make_driver}

def run(instance) -> int:
    """pyioc-run entrypoint: one DevicePVs per device, all served by this process."""
    logging.basicConfig(level=getattr(logging, instance.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pvdb, drivers = {{}}, []
    for device in instance.devices:
        driver = make_driver(device)
        drivers.append(driver)
        pvdb.update(DevicePVs(prefix=device.prefix, driver=driver, period=device.poll_period).pvdb)
        log.info("device %s: prefix %s%s", device.name or "-", device.prefix,
                 " (simulated)" if device.simulate else "")
    interfaces = [i.strip() for i in instance.interfaces.split(",") if i.strip()]
    try:
        run_server(pvdb, interfaces=interfaces)
    finally:
        for driver in drivers:
            driver.close()
    return 0
'''


def _driver_py(d: dict) -> str:
    rw = [p for p in d["pvs"] if p["access"] in ("ro", "rw")]
    commands = "\n".join(
        f"    {p['name']!r}: ({(p['name'].upper() + '?')!r}, "
        f"{(p['name'].upper() + ' {value}') if p['access'] == 'rw' else None!r}),  # TODO: real command"
        for p in rw)
    actions = "\n".join(f"    {p['name']!r}: {p['name'].upper()!r},  # TODO: real command"
                        for p in d["pvs"] if p["access"] == "cmd")
    kinds = {p["name"]: p["type"] for p in rw}
    lengths = {p["name"]: p["length"] for p in rw if p["type"] == "waveform"}
    common = f'''"""Device access for the {d['name']} IOC: the simulator and the real driver.

A driver has connect(), close(), read(names) -> {{name: value}}, write(name, value) and
command(name).  The IOC calls them from worker threads, one call at a time per device.
Enum/bool values are indexes (0, 1, ...), waveforms any sequence of numbers.
"""

from __future__ import annotations

import math
import random
import socket

KINDS = {kinds!r}
LENGTHS = {lengths!r}


class SimulatedDriver:
    """Keeps the values in memory; read-only numbers drift a little so the IOC looks alive."""

    def __init__(self, defaults: dict):
        self.values = {{name: (("" if KINDS.get(name) == "string" else 0) if value is None else value)
                       for name, value in defaults.items()}}
        for name, length in LENGTHS.items():
            self.values[name] = [math.sin(i / 20.0) for i in range(length)]
        self.connected = False

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def read(self, names: list[str]) -> dict:
        out = {{}}
        for name in names:
            value = self.values.get(name)
            if KINDS.get(name) == "float":
                value = float(value or 0.0) + random.uniform(-0.01, 0.01)
            out[name] = value
        return out

    def write(self, name: str, value) -> None:
        self.values[name] = value

    def command(self, name: str) -> None:
        pass
'''
    if d["transport"] == "tcp":
        return common + f'''

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
    return common + '''

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


def _example_yaml(d: dict) -> str:
    lines = [
        "# A values.yaml-style IOC entry for local runs:",
        "#   pyioc-run example.yaml --check     (validate and show the resolved instance)",
        "#   pyioc-run example.yaml             (start the IOC on the simulator)",
        f"devtype: {d['name']}",
        f"iocname: {d['name']}-test",
        f"iocprefix: TEST:{re.sub(r'[^A-Z0-9]', '_', d['name'].upper())}",
        "simulate: true",
        "poll_period: 0.2",
    ]
    if d["multiple"]:
        lines += ["devices:", "  - name: DEV01"]
    return "\n".join(lines) + "\n"


def _test_py(d: dict) -> str:
    prefix = "TEST:" + re.sub(r"[^A-Z0-9]", "_", d["name"].upper()) + (":DEV01:" if d["multiple"] else ":")
    reads = [p["name"] for p in d["pvs"] if p["access"] in ("ro", "rw")]
    writes = [p for p in d["pvs"] if p["access"] == "rw"]
    write_lines = []
    for p in writes:
        if p["type"] in ("bool", "enum"):
            value = p["choices"][-1]
            write_lines.append(f"    put({p['name']!r}, {value!r})          # enums are written by name\n"
                               f"    assert _wait(lambda: get({p['name']!r}) == {value.encode()!r})")
        elif p["type"] == "string":
            write_lines.append(f"    put({p['name']!r}, 'test')\n"
                               f"    assert _wait(lambda: get({p['name']!r}) == b'test')")
        else:
            value = 1 if p["type"] == "int" else 1.5
            if p["limits"]:
                value = p["limits"][0]
            write_lines.append(f"    put({p['name']!r}, {value!r})\n"
                               f"    assert _wait(lambda: abs(get({p['name']!r}) - {value!r}) < 0.1)")
    cmd_lines = [f"    put({p['name']!r}, {p['name']!r})" for p in d["pvs"] if p["access"] == "cmd"]
    return f'''"""The IOC over real Channel Access, on the simulator: start it, read and write its PVs."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

client = pytest.importorskip("caproto.sync.client")
EXAMPLE = Path(__file__).resolve().parent.parent / "example.yaml"
P = {prefix!r}


def _wait(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def ioc(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, EPICS_CA_SERVER_PORT=str(port), EPICS_CA_ADDR_LIST="127.0.0.1",
               EPICS_CA_AUTO_ADDR_LIST="NO", EPICS_CAS_INTF_ADDR_LIST="127.0.0.1")
    for key in ("EPICS_CA_SERVER_PORT", "EPICS_CA_ADDR_LIST", "EPICS_CA_AUTO_ADDR_LIST"):
        monkeypatch.setenv(key, env[key])
    proc = subprocess.Popen([sys.executable, "-m", "epik8s_pyioc.runner", str(EXAMPLE)], env=env)

    def get(pv):
        return client.read(P + pv, timeout=2).data[0]

    def put(pv, value):
        client.write(P + pv, value, timeout=2, notify=True)

    try:
        assert _wait(lambda: get("Connected") == b"Connected", 15), "IOC did not start"
        yield get, put
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_every_pv_answers(ioc):
    get, put = ioc
    for name in {reads!r}:
        assert _wait(lambda: get(name) is not None), name
    assert get("LastError") == b""


def test_writes(ioc):
    get, put = ioc
{chr(10).join(write_lines) if write_lines else "    pass"}
{chr(10).join(cmd_lines)}
    assert get("Connected") == b"Connected"
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
  "caproto>=1.1",
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
{d['package']} = ["pyioc.yaml"]

[tool.pytest.ini_options]
testpaths = ["tests"]
'''


def _dockerfile(d: dict) -> str:
    return f'''FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
WORKDIR /opt/ioc
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && pyioc check --devtype {d['name']}
# ioc-chart starts it with `start: pyioc-run` (the IOC entry is in /tmp/iocconfig.yaml);
# the pod runs as uid 1000, so nothing here may need root at run time.
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
          python-version: "3.12"
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


def _readme(d: dict) -> str:
    rows = "\n".join(
        f"| `{p['name']}` | {p['type']}{' [' + str(p['length']) + ']' if p['length'] else ''} | "
        f"{ {'ro': 'RO', 'rw': 'RW', 'cmd': 'command'}[p['access']] } | {p['unit']} | {p['description']} |"
        for p in d["pvs"])
    prefix = "<iocprefix>:<device name>:" if d["multiple"] else "<iocprefix>:"
    return f'''# {d['name']}

{d['description']}. A containerized Python (caproto) EPICS IOC, configured from an epik8s
beamline `values.yaml` through [epik8s-pyioc](https://github.com/infn-epics/epik8s-pyioc).

## PVs

Every device serves `{prefix}<PV>`:

| PV | Type | Access | Unit | Description |
|---|---|---|---|---|
| `Connected` | enum | RO | | Disconnected / Connected (reconnects by itself) |
| `LastError` | string | RO | | last error, first 39 characters |
| `LastErrorLong` | char[240] | RO | | full error text (`caget -S`) |
{rows}

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
Tagging `vX.Y.Z` publishes `{d['image']}:X.Y.Z` (GitHub Actions).

## Write the driver

`src/{d['package']}/driver.py`: the simulator works as is; {"fill in the real commands in `COMMANDS`/`ACTIONS` and the reply parsing of `TcpDriver`" if d["transport"] == "tcp" else "implement `DeviceDriver`"}.
'''


def render_project(description: Mapping[str, Any] | str) -> dict:
    """The project's files as {path: text}, plus a self-check; raises ScaffoldError."""
    d = normalize(description)
    pkg = f"src/{d['package']}"
    files = {
        "pyproject.toml": _pyproject(d),
        "README.md": _readme(d),
        "Dockerfile": _dockerfile(d),
        ".dockerignore": ".git\n.venv\n__pycache__\n*.egg-info\n.pytest_cache\n",
        ".gitignore": ".venv/\n__pycache__/\n*.egg-info/\n.pytest_cache/\nbuild/\ndist/\n",
        ".github/workflows/ci.yml": _ci(d),
        "example.yaml": _example_yaml(d),
        f"{pkg}/__init__.py": f'"""{d["description"]}."""\n\n__version__ = "{d["version"]}"\n',
        f"{pkg}/pyioc.yaml": _spec_yaml(d),
        f"{pkg}/ioc.py": _ioc_py(d),
        f"{pkg}/driver.py": _driver_py(d),
        "tests/test_ioc.py": _test_py(d),
    }
    return {"files": files, "description": d, "self_check": check_project(files, d)}


def check_project(files: Mapping[str, str], d: dict) -> dict:
    """What can be checked without running anything: the spec, the example against it, Python syntax."""
    import yaml

    from .instance import InstanceError, resolve_instance
    from .spec import SpecError, parse_spec

    errors, notes = [], []
    pkg = f"src/{d['package']}"
    try:
        spec = parse_spec(yaml.safe_load(files[f"{pkg}/pyioc.yaml"]))
        instance = resolve_instance(spec, yaml.safe_load(files["example.yaml"]))
        notes.append(f"example.yaml resolves: {', '.join(dev.prefix for dev in instance.devices)}")
    except (SpecError, InstanceError) as exc:
        errors.append(f"pyioc.yaml/example.yaml: {exc}")
    for path, text in files.items():
        if path.endswith(".py"):
            try:
                compile(text, path, "exec")
            except SyntaxError as exc:
                errors.append(f"{path}:{exc.lineno}: {exc.msg}")
    ioc = files[f"{pkg}/ioc.py"]
    for p in d["pvs"]:
        if f"name={p['name']!r}" not in ioc:
            errors.append(f"PV {p['name']} missing from ioc.py")
    return {"ok": not errors, "errors": errors, "notes": notes}


def scaffold(name: str, directory: Path, package: str | None = None, image: str = "",
             devgroup: str = "", description: Mapping[str, Any] | None = None) -> list[Path]:
    """Write a new project to ``directory`` (``pyioc init``); refuses to overwrite files."""
    spec = dict(description or {"pvs": [
        {"name": "Value", "type": "float", "access": "ro", "unit": "", "precision": 3},
        {"name": "Setpoint", "type": "float", "access": "rw", "precision": 3},
        {"name": "Enable", "type": "bool", "access": "rw", "choices": ["Off", "On"]},
        {"name": "Reset", "type": "bool", "access": "cmd"},
    ]})
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
