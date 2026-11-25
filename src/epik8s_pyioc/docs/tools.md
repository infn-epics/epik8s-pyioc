# Tools

```sh
pyioc init NAME [--from DESCRIPTION.yaml | --box]   # a new device IOC project or box (see the how-to)
pyioc check   -s src/pkg/pyioc.yaml         # validate the spec
pyioc docs    -s ...                        # Markdown parameter tables
pyioc schema  -s ... [--fragment]           # JSON Schema of an entry (or an if-devtype clause for a beamline schema)
pyioc defaults -s ... [--tag X.Y.Z]         # the iocDefaults block
pyioc render  -s ... entry.yaml             # an entry resolved as pyioc-run sees it
pyioc validate -s ... deploy/values.yaml    # every IOC of this devtype in a beamline (CI)
pyioc list                                  # the IOCs installed in this environment
pyioc-run [config] [--check]                # start (default config: $IOC_CONFIG or /tmp/iocconfig.yaml)
```

Without `-s` the tools use `./pyioc.yaml`, or the installed IOC chosen with `--devtype`.
Inside an image, `pyioc docs --devtype <name>` documents the IOC that image ships.

`pyioc validate` reports, for each IOC of the devtype, the spec's own errors and the chart
mistakes it can see: a `template:` key (which sends the pod down the ibek branch), a
`start` that is not `pyioc-run`, and a missing `pva: false` for a CA-only IOC.

## As a library

```python
from epik8s_pyioc import load_spec, resolve_instance
from epik8s_pyioc.scaffold import render_project       # {kind, files: {path: text}, self_check}
from epik8s_pyioc.tools import json_schema, ioc_defaults, markdown, validate_values
from epik8s_pyioc.guide import guide                    # these documents, by title
```

ARGUS's IOC engineer uses these functions, so its answers and `pyioc` agree.
