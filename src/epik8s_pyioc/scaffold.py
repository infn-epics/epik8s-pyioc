"""``pyioc init``: skeleton of a new containerized Python IOC."""

from __future__ import annotations

from pathlib import Path
import re

SPEC = """\
pyioc: 1
name: {name}                       # devtype used in values.yaml
description: {name} IOC
devgroup: {devgroup}
image: {image}
version: 0.1.0
entrypoint: {package}.ioc:run      # called by pyioc-run with the resolved Instance
protocols: [ca]

# IOC-wide parameters
parameters:
  poll_period:
    type: float
    default: 1.0
    min: 0.1
    max: 3600
    unit: s
    description: Device polling period

# Per-device parameters. Top-level values are defaults for every `devices:` entry.
devices:
  multiple: true
  prefix: "{{iocprefix}}:{{name}}:"
  single_prefix: "{{iocprefix}}:"
  parameters:
    server:
      type: host
      aliases: [ip]
      required: true
      description: Device address
    port:
      type: int
      default: 5025
      min: 1
      max: 65535
      description: TCP port (ioc-chart checks server:port is reachable before start)
"""

IOC = '''\
"""{name} IOC (caproto).  Started by ``pyioc-run``; parameters come from values.yaml."""

from __future__ import annotations

import asyncio
import logging

from caproto.server import PVGroup, pvproperty, run as run_server

from epik8s_pyioc import Device, Instance

log = logging.getLogger(__name__)


class DeviceIOC(PVGroup):
    value = pvproperty(value=0.0, read_only=True, name="Value")
    connected = pvproperty(value=0, read_only=True, name="Connected")

    def __init__(self, *args, device: Device, period: float, **kwargs):
        self.device = device
        self.period = period
        super().__init__(*args, **kwargs)

    @connected.startup
    async def connected(self, instance, async_lib):
        while True:
            try:
                # replace with the real device communication
                # (self.device.server, self.device.port, ...)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.device.server, self.device.port), timeout=2)
                writer.close()
                await instance.write(value=1)
            except (OSError, asyncio.TimeoutError):
                await instance.write(value=0)
            await asyncio.sleep(self.period)


def run(instance: Instance) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pvdb = {{}}
    for device in instance.devices:
        log.info("device %s prefix=%s server=%s:%s", device.name or "-", device.prefix,
                 device.server, device.port)
        pvdb.update(DeviceIOC(prefix=device.prefix, device=device,
                              period=instance.poll_period).pvdb)
    run_server(pvdb, interfaces=["0.0.0.0"])
    return 0
'''

PYPROJECT = """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "{dist}"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["epik8s-pyioc>=0.1", "caproto>=1.1"]

# registers pyioc.yaml so `pyioc-run` finds it
[project.entry-points."epik8s.pyioc"]
{name} = "{package}"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
{package} = ["pyioc.yaml"]
"""

DOCKERFILE = """\
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/ioc
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && pyioc check src/{package}/pyioc.yaml
# ioc-chart overrides the command with `start: pyioc-run`; this is for docker run
CMD ["pyioc-run"]
"""

EXAMPLE = """\
# local test:  pyioc-run example.yaml --check   /   pyioc-run example.yaml
devtype: {name}
iocname: {name}-test
iocprefix: TEST:{upper}
devices:
  - name: DEV01
    server: 127.0.0.1
"""


def scaffold(name: str, directory: Path, package: str | None = None, image: str = "",
             devgroup: str = "") -> list[Path]:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise ValueError("name: letters, digits, '_' and '-' only")
    package = package or re.sub(r"\W", "_", name.lower()) + "_ioc"
    image = image or f"ghcr.io/infn-epics/{name.lower()}"
    fields = dict(name=name, package=package, image=image, devgroup=devgroup or "io",
                  dist=package.replace("_", "-"), upper=re.sub(r"\W", "_", name.upper()))
    files = {
        directory / "pyproject.toml": PYPROJECT,
        directory / "Dockerfile": DOCKERFILE,
        directory / "example.yaml": EXAMPLE,
        directory / "src" / package / "__init__.py": '"""{name} Python IOC."""\n',
        directory / "src" / package / "ioc.py": IOC,
        directory / "src" / package / "pyioc.yaml": SPEC,
    }
    existing = [path for path in files if path.exists()]
    if existing:
        raise FileExistsError(", ".join(str(p) for p in existing))
    for path, template in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(**fields))
    return list(files)
