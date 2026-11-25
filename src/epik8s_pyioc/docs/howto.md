# How to: a new Python IOC, from nothing to a beamline

## 0. Is a Python IOC the right kind?

epik8s runs three kinds of IOC. Pick by what the IOC does:

| The IOC... | Use | Configured by |
|---|---|---|
| drives hardware that has an EPICS driver (asyn, StreamDevice, modbus, areaDetector, motor) | an **ibek template** in the infn-epics-ioc image | `template:` + `devtype`, entity models in ibek-support-infn |
| drives hardware best reached from Python: a vendor library for Python or .NET, a protocol simpler to write in Python | a **device IOC** from `pyioc init`, in its own image (this guide) | `devtype` + `start: pyioc-run`, parameters in values.yaml checked against `pyioc.yaml` |
| adds logic over PVs that already exist: interlocks, sequences, monitoring, derived values | a **box** from `pyioc init` (section 7), run by iocmng in the shared [epik8s-softioc-mng](https://github.com/infn-epics/epik8s-softioc-mng) image | an entry under `softiocs:`, and the box's `config.yaml` in the beamline repository |

Both Python shapes come from the same description and run on the same engine, iocmng on
pythonSoftIOC (Channel Access and PV Access). A device IOC owns a device; a box only reads
other IOCs' PVs and writes others, needs no image of its own, and is reviewed as
configuration. A device IOC may still hold local logic over its own values (a derived value,
a local interlock); logic across IOCs belongs in a box, in its own pod.

## 1. Describe the device and generate the project

```yaml
# tpg366.yaml
name: tpg366                      # becomes the devtype
description: Pfeiffer TPG366 gauge controller
devgroup: vac
transport: tcp                    # tcp: a line-oriented socket driver; none: write your own
default_port: 8000
device_parameters:
  max_pressure: {type: float, default: 1.0e-3, description: Leak threshold}
pvs:
  - {name: Pressure, type: float, access: ro, unit: mbar, precision: 3, buffer_size: 60}
  - {name: Unit, type: enum, access: rw, choices: [mbar, Torr, Pa]}
  - {name: Trace, type: waveform, access: ro, length: 512}
  - {name: Reset, type: bool, access: cmd}
  - {name: Average, type: float, source: {expr: "mean(Pressure_buf)"}}
  - {name: Leak, type: bool, source: soft, choices: [OK, LEAK], alarm_on: MAJOR}
rules:
  - {id: LEAK, condition: "Pressure > max_pressure", tolerance: 10, outputs: {Leak: 1}}
rule_defaults: {Leak: 0}
```

```sh
pyioc init tpg366 --from tpg366.yaml
```

PV types: `float`, `int`, `bool`, `enum` (2 to 16 `choices`), `string` (39 characters),
`waveform` (`length`, driver PVs only). Access: `ro`, `rw`, or `cmd` (a write-only action, a
bool shown as `Idle`/`<name>`). Optional: `unit`, `precision`, `limits: [lo, hi]`,
`default`, `description`, `buffer_size` (keeps the last samples as `<name>_buf` for
expressions), `alarm_on` (`MAJOR`/`MINOR` when a bool is 1), `latch` (a soft bool set by a
rule stays set until `CLEAR`/`RESET`).

Where each value comes from, `source`:

| `source` | Value from | Writable |
|---|---|---|
| `driver` (the default) | the device, read every `poll_period` | `rw` and `cmd` PVs go to the device |
| `soft` | operators (`access: rw`) or rules | `rw` only |
| `{link: <PV>, mode: monitor}` | another IOC's PV (`mode: poll` to poll it) | by rule `actuators` |
| `{expr: "..."}` | an expression, every `interval` | no |

Expressions and rule conditions see the driver and link PVs, their `<name>_buf` buffers,
and the parameters (values.yaml parameters for a device IOC); an expression also sees the
expr PVs listed before it. Soft PVs are not visible, and a rule does not see expr PVs.
Functions: `abs`, `round`, `sqrt`, `log`, `exp`, `pow`, `floor`, `ceil`, `clamp`, `mean`,
`std`, `variance`, `median`, `rms`, `min`, `max`, `any_of`, `all_of`, `count_true`,
`length`, `sum_of`, `diff`, `last`, `moving_avg`, `derivative`. A rule has an `id`, a
`condition`, an optional `tolerance` (seconds it must hold), `outputs` (soft PVs or driver
settings to set) and `actuators` (link PVs whose linked PV is written); `rule_defaults` are
applied before each evaluation. `mode: continuous` (the device default) evaluates every
`interval` (1 s); `mode: reactive` on each change. Extra configuration keys go in
`parameters` (IOC-wide) and `device_parameters` (per device), in the
[specification](specification.md)'s format.

The project:

| File | What it is |
|---|---|
| `src/<pkg>/pyioc.yaml` | the spec: `simulate`, `poll_period`, `server`/`port`/`timeout` (tcp), and your parameters |
| `src/<pkg>/config.yaml` | the PVs, expressions and rules, in iocmng's format: edit them here |
| `src/<pkg>/ioc.py` | `DeviceIOC`, an iocmng task per device with `Connected`, `LastError`, `LastErrorLong`; Python logic goes in its `execute()` |
| `src/<pkg>/driver.py` | the real driver to complete (`simulate: true` uses iocmng's simulator) |
| `tests/test_ioc.py` | starts the IOC on the simulator and reads and writes every PV over Channel Access |
| `example.yaml` | a values.yaml-style entry for local runs |
| `Dockerfile`, `.github/workflows/ci.yml` | the image, built on the epik8s-softioc-mng image, tested and published to `ghcr.io` on `v*` tags |

Every device also serves iocmng's `STATUS`, `MESSAGE`, `ENABLE`, `CONNECTED`, `CYCLE_COUNT`,
`CLEAR`, `RESET` and `VERSION`.

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
of `TcpDriver`, or implement `DeviceDriver`. Keep the driver synchronous: iocmng calls it
from one thread per device, one call at a time, so a call may block but delays that device's
writes while it does. A failed `read` means the device is lost (it is closed and
reconnected); a failed `write` or `command` is reported and keeps the connection. Read the
[pitfalls](pitfalls.md) before adding anything that can block for long.

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

## 7. A box: logic over existing PVs

The same description with no driver PV and no `transport` is a box:

```yaml
# vac-leak.yaml
name: vac-leak
description: Leak detection between two gauges
devgroup: vac
parameters: {ratio: 3.0}          # values, usable in expressions
pvs:
  - {name: P1, type: float, source: {link: "SPARC:VAC:GAUGE01:Pressure"}}
  - {name: P2, type: float, source: {link: "SPARC:VAC:GAUGE02:Pressure"}}
  - {name: Ratio, type: float, source: {expr: "P1 / P2 if P2 else 0"}}
  - {name: Leak, type: bool, source: soft, choices: [OK, LEAK], latch: true, alarm_on: MAJOR}
rules:
  - {id: LEAK, condition: "P2 > 0 and P1 / P2 > ratio", tolerance: 10, outputs: {Leak: 1}}
```

```sh
pyioc init vac-leak --from vac-leak.yaml     # or: pyioc init vac-leak --box (this example)
```

A box defaults to `mode: reactive`: its links get `trigger: true`, so a change of a linked PV
re-evaluates the expressions and rules. The folder holds `config.yaml`, `start.sh.j2` (the
`iocmng-run` command ioc-chart renders), a README with the `softiocs:` entry, and
`tests/test_box.py`, which serves the linked PVs from a stub IOC and checks that the box
follows them; fill in its `RULE_CASES` to test each rule. `python: true` adds a
`DeclarativeTask` subclass for logic the rules cannot express.

Deploy it by copying the folder to the beamline repository as `config/softiocs/<name>/` and
adding the `softiocs:` entry from its README.

