# The pyioc.yaml specification (`pyioc: 1`)

A containerized Python IOC ships one `pyioc.yaml` inside its Python package. It is the
IOC's support definition, the Python counterpart of an ibek `*.ibek.support.yaml`. It
declares which keys a beamline `values.yaml` IOC entry may set, with their types,
defaults and ranges. `pyioc-run` validates the entry against it before starting the IOC,
and the `pyioc` tools generate the JSON Schema, the documentation and the `iocDefaults`
block from it.

## Format

```yaml
pyioc: 1                         # spec format version (required)
name: thorlabscct                # devtype in values.yaml (required)
description: Thorlabs CCT spectrometers
devgroup: diag                   # default devgroup in the generated iocDefaults
image: ghcr.io/infn-epics/thorlabscct   # image repository; tag = version
version: 0.2.4
docs: https://github.com/infn-epics/thorlabscct   # becomes `asset` in iocDefaults
entrypoint: thorlabscct10_ioc.ioc:run_pyioc   # module:function(instance) -> int | None
protocols: [ca]                  # ca and/or pva; without pva the defaults say `pva: false`

parameters:                      # IOC-wide parameters (top level of the IOC entry)
  log_level: {type: enum, choices: [DEBUG, INFO, WARNING, ERROR], default: INFO}

devices:                         # optional: per-device parameters
  multiple: true                 # allow a `devices:` list (several devices, one process)
  prefix: "{iocprefix}:{name}:"  # PV prefix of each list entry
  single_prefix: "{iocprefix}:"  # PV prefix without a list
  parameters:
    server: {type: host, aliases: [ip]}
    exposure: {type: float, default: 8.3, min: 0.01, max: 30000, unit: ms}
    trigger_mode: {type: enum, choices: [free, hardware], default: free}
```

Unknown top-level fields are refused, so a typo in the spec is found at `pyioc check`,
not in production.

## Parameter definition

| Field | Meaning |
|---|---|
| `type` | `string`, `int`, `float`, `bool`, `enum`, `host`, `list`, `map`, `any`. A bare string is shorthand: `port: int` |
| `default` | Value when the key is absent. Without a default the value is `None` |
| `required` | A missing value is an error |
| `choices` | The values of an `enum`. Matched case-insensitively, returned in the declared spelling |
| `min`, `max` | Numeric range of `int` and `float` |
| `pattern` | Regular expression for `string` and `host` |
| `unit`, `description`, `example` | Documentation, also used in the JSON Schema and `pyioc docs` |
| `aliases` | Other keys accepted for the same parameter, such as `ip` for `server` |

Values are coerced from YAML scalars. Values set through `iocparam`, which ioc-chart
flattens to top-level `name: value` lines, arrive as strings and are accepted. Booleans
accept `true/false/yes/no/on/off/1/0`. A `host` is an IP address or a DNS name. Resolve it
in the IOC if the device library takes only IP addresses.

## Resolving an IOC entry

- `iocprefix` is required. `iocname` is injected by epik8s-chart.
- Without `devices:` there is one device with prefix `single_prefix`, and its parameters
  are read from the top level of the entry.
- With `devices:` each entry needs `name` (or an explicit `prefix`) and may set
  `disable: true`. Device parameters set at top level are **defaults for every entry**.
- Unknown keys at top level are ignored, because the chart's own settings live there. In
  `devices` entries they produce a warning, which catches typos.
- Every problem is reported at once, and `pyioc-run` exits with status 2 before starting
  anything.

## Reserved keys

A parameter may not be named after a key that epik8s-chart or ioc-chart interprets
(`template`, `host`, `serial`, `start`, `stop`, `image`, `devices`, `iocparam`, `gitinit`,
`nfsMounts`, `pva`, ...: `epik8s_pyioc.spec.RESERVED_KEYS`). Such a parameter would change
how the IOC is deployed. `server` plus `port` is allowed on purpose: ioc-chart then checks
that the device answers on that TCP port before the IOC starts. Set `nocheckconn: true` to
skip that check.

## PV names and operator screens

The epik8s screen tools (opigen and the launcher's `epik8sutil.py`) build PV names as
`$(P):$(R):<PV>`, with `P` = `iocprefix` and `R` = the device `name`, and they list only
IOCs that have a `devices:` list. Keep the default prefix templates and declare even a
single device in the list form:

```yaml
cct01:
  devtype: thorlabscct
  iocprefix: SPARC:DIAG
  devices:
    - name: CCT01            # PVs SPARC:DIAG:CCT01:*, screen macros P=SPARC:DIAG R=CCT01
```
