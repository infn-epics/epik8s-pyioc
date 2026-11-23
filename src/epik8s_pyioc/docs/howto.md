# How to: a new Python IOC, from nothing to a beamline

## 0. Is a Python IOC the right kind?

epik8s runs three kinds of IOC. Pick by what the IOC does:

| The IOC... | Use | Configured by |
|---|---|---|
| drives hardware that has an EPICS driver (asyn, StreamDevice, modbus, areaDetector, motor) | an **ibek template** in the infn-epics-ioc image | `template:` + `devtype`, entity models in ibek-support-infn |
| drives hardware best reached from Python: a vendor library for Python or .NET, a protocol simpler to write in Python | an **epik8s-pyioc** IOC in its own image (this guide) | `devtype` + `start: pyioc-run`, parameters in values.yaml checked against `pyioc.yaml` |
| adds logic over PVs that already exist: interlocks, sequences, monitoring, derived values | an **iocmng** soft IOC ([epik8s-softioc-mng](https://github.com/infn-epics/epik8s-softioc-mng)) | an entry under `softiocs:`, and a `config.yaml` of inputs, rules and outputs in the beamline repository |

An epik8s-pyioc IOC owns a device. A task that only reads other IOCs' PVs and writes others
belongs in iocmng: its declarative rules need no code, run in a shared image, and are
reviewed as configuration. A device IOC built as an iocmng task, or an interlock built as a
device IOC, works but is the wrong shape to maintain.

## 1. Describe the device and generate the project

```yaml
# tpg366.yaml
name: tpg366                      # becomes the devtype
description: Pfeiffer TPG366 gauge controller
devgroup: vac
transport: tcp                    # tcp: a line-oriented socket driver; none: write your own
default_port: 8000
pvs:
  - {name: Pressure, type: float, access: ro, unit: mbar, precision: 3}
  - {name: Unit, type: enum, access: rw, choices: [mbar, Torr, Pa]}
  - {name: Trace, type: waveform, access: ro, length: 512}
  - {name: Reset, type: bool, access: cmd}
```

```sh
pyioc init tpg366 --from tpg366.yaml
```

PV types: `float`, `int`, `bool`, `enum` (2 to 16 `choices`), `string` (39 characters),
`waveform` (`length`). Access: `ro`, `rw`, or `cmd` (a write-only action, a bool shown as
`Idle`/`<name>`). Optional: `unit`, `precision`, `limits: [lo, hi]`, `default`,
`description`. Extra configuration keys go in `parameters` (IOC-wide) and
`device_parameters` (per device), in the [specification](specification.md)'s format.

The project:

| File | What it is |
|---|---|
| `src/<pkg>/pyioc.yaml` | the spec: `simulate`, `poll_period`, `server`/`port`/`timeout` (tcp), and your parameters |
| `src/<pkg>/ioc.py` | the caproto IOC: one PV group per device, `Connected`, `LastError`, `LastErrorLong`, your PVs, polling and reconnection |
| `src/<pkg>/driver.py` | the simulator (works as is) and the real driver to complete |
| `tests/test_ioc.py` | starts the IOC on the simulator and reads and writes every PV over Channel Access |
| `example.yaml` | a values.yaml-style entry for local runs |
| `Dockerfile`, `.github/workflows/ci.yml` | the image, tested and published to `ghcr.io` on `v*` tags |

## 2. Run it on the simulator

```sh
cd tpg366
pip install -e '.[test]'
pyioc-run example.yaml --check    # the entry as the IOC will see it
pyioc-run example.yaml            # serve TEST:TPG366:DEV01:*
pytest                            # the same, checked over Channel Access
```

## 3. Write the driver

In `driver.py`, fill in the device's commands (`COMMANDS`, `ACTIONS`) and the reply parsing
of `TcpDriver`, or implement `DeviceDriver`. Keep the driver synchronous: the IOC calls it
from worker threads, one call at a time per device. Read the [pitfalls](pitfalls.md) before
adding anything that can block.

## 4. Publish

Push to `github.com/infn-epics/<name>` and tag `v0.1.0`. CI tests it and publishes
`ghcr.io/infn-epics/<name>:0.1.0`.

## 5. Deploy

```sh
pyioc defaults --spec src/tpg366_ioc/pyioc.yaml    # the iocDefaults block
```

Add it, and the IOC entry, to the beamline `deploy/values.yaml`:

```yaml
epicsConfiguration:
  iocs:
    gauges01:
      devtype: tpg366
      iocprefix: SPARC:VAC
      server: plsparcmoxa001.lnf.infn.it
      devices:
        - name: TPG01
```

```sh
pyioc validate --spec src/tpg366_ioc/pyioc.yaml deploy/values.yaml
```

Commit, then sync the ArgoCD application. ARGUS's IOC engineer can do the same checks:
`ioc_get_pyioc`, and `ioc_render` or `ioc_check_beamline` on the beamline.

## 6. An operator screen

Put a Phoebus `.bob` in `epik8s-opi` with PV names `$(P):$(R):<PV>`, and set
`opi: <dir>/<file>.bob` in `iocDefaults`. Check every PV the screen uses against
`pyioc-run example.yaml` and a CA client.
