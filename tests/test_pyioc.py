import json
import textwrap

import pytest
import yaml

from epik8s_pyioc import InstanceError, SpecError, parse_spec, resolve_instance
from epik8s_pyioc.cli import main as cli
from epik8s_pyioc.runner import main_run
from epik8s_pyioc.scaffold import scaffold
from epik8s_pyioc import tools

SPEC = yaml.safe_load(textwrap.dedent("""
    pyioc: 1
    name: demo
    devgroup: diag
    image: registry.example/demo
    version: 1.2.3
    entrypoint: demo_pkg.ioc:run
    parameters:
      log_level: {type: enum, choices: [DEBUG, INFO], default: INFO}
    devices:
      multiple: true
      parameters:
        server: {type: host, aliases: [ip], required: true}
        exposure: {type: float, default: 8.3, min: 0.01, max: 30000, unit: ms}
        average: {type: int, default: 1, min: 1}
        continuous: {type: bool, default: false}
"""))


@pytest.fixture
def spec():
    return parse_spec(SPEC)


def test_single_device_from_ioc_chart_dump(spec):
    # flattened iocparam values arrive as strings; chart keys are ignored
    instance = resolve_instance(spec, {
        "iocname": "demo01", "iocprefix": "SPARC:DIAG:DEMO", "devtype": "demo",
        "image": "x", "serial": {"ip": None, "port": None}, "config_dir": "/epics/ioc/config",
        "ip": "10.0.0.5", "exposure": "12.5", "continuous": "yes", "log_level": "debug",
    })
    (device,) = instance.devices
    assert device.prefix == "SPARC:DIAG:DEMO:"
    assert device.server == "10.0.0.5" and device["exposure"] == 12.5
    assert device.continuous is True and device.average == 1
    assert instance.log_level == "DEBUG"


def test_devices_inherit_defaults_and_warn_on_typos(spec):
    instance = resolve_instance(spec, {
        "iocprefix": "SPARC:DIAG:SPEC:", "exposure": 5, "server": "10.0.0.1",
        "devices": [
            {"name": "A"},
            {"name": "B", "server": "10.0.0.2", "exposure": 20, "averag": 3},
            {"name": "C", "disable": True},
            {"name": "D", "prefix": "OTHER:"},
        ],
    })
    assert [d.prefix for d in instance.devices] == ["SPARC:DIAG:SPEC:A:", "SPARC:DIAG:SPEC:B:", "OTHER:"]
    assert [d.exposure for d in instance.devices] == [5, 20, 5]
    assert [d.server for d in instance.devices] == ["10.0.0.1", "10.0.0.2", "10.0.0.1"]
    assert any("averag" in w for w in instance.warnings)


def test_all_errors_are_reported(spec):
    with pytest.raises(InstanceError) as info:
        resolve_instance(spec, {"exposure": 0, "average": "x", "continuous": "maybe"})
    text = " ".join(info.value.errors)
    for key in ("iocprefix", "server", "exposure", "average", "continuous"):
        assert key in text


@pytest.mark.parametrize("change", [
    {"pyioc": 2},
    {"name": ""},
    {"entrypoint": "nocolon"},
    {"parameters": {"template": "string"}},          # reserved by ioc-chart
    {"parameters": {"server": "string"}},            # duplicate of a device parameter
    {"parameters": {"x": {"type": "enum"}}},
    {"parameters": {"x": {"type": "int", "default": "a"}}},
    {"parameters": {"x": {"type": "int", "minimum": 1}}},
    {"unknown": 1},
])
def test_invalid_specs(change):
    with pytest.raises(SpecError):
        parse_spec({**SPEC, **change})


def test_generated_defaults_and_schema(spec):
    defaults = tools.ioc_defaults(spec)
    assert defaults["image"] == "registry.example/demo:1.2.3"
    assert defaults["start"] == "pyioc-run" and defaults["pva"] is False
    schema = tools.json_schema(spec)
    assert schema["properties"]["exposure"]["maximum"] == 30000
    assert "server" in schema["properties"]["devices"]["items"]["properties"]
    fragment = tools.json_schema(spec, fragment=True)
    assert fragment["if"]["properties"]["devtype"]["const"] == "demo"
    assert "| `server`, `ip` |" in tools.markdown(spec)


def test_validate_beamline_values(spec):
    values = {
        "iocDefaults": {"demo": tools.ioc_defaults(spec)},
        "epicsConfiguration": {"iocs": {
            "good": {"devtype": "demo", "iocprefix": "A", "server": "1.2.3.4"},
            "bad": {"devtype": "demo", "template": "demo", "iocprefix": "B"},
            "off": {"devtype": "demo", "disable": True},
            "other": {"devtype": "camera", "iocprefix": "C"},
        }},
    }
    checked, errors, _ = tools.validate_values(spec, values)
    assert checked == 3
    assert all(e.startswith("bad:") for e in errors)
    assert any("template" in e for e in errors) and any("server" in e for e in errors)


def test_runner_check_and_start(tmp_path, monkeypatch, capsys):
    package = tmp_path / "demo_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "ioc.py").write_text(
        "def run(instance):\n"
        "    print('STARTED', instance.iocname, [d.prefix for d in instance.devices])\n"
        "    return 0\n")
    spec_path = tmp_path / "pyioc.yaml"
    spec_path.write_text(yaml.safe_dump(SPEC))
    config = tmp_path / "iocconfig.yaml"
    config.write_text("iocname: d1\niocprefix: X\ndevtype: demo\nserver: 1.2.3.4\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("PYIOC_SPEC", raising=False)

    assert main_run([str(config), "--spec", str(spec_path), "--check"]) == 0
    assert json.loads(capsys.readouterr().out)["devices"][0]["prefix"] == "X:"
    assert main_run([str(config), "--spec", str(spec_path)]) == 0
    assert "STARTED d1 ['X:']" in capsys.readouterr().out

    config.write_text("iocprefix: X\n")
    assert main_run([str(config), "--spec", str(spec_path)]) == 2
    assert "server: required" in capsys.readouterr().err


def test_scaffold_produces_valid_project(tmp_path, capsys):
    scaffold("mydev", tmp_path / "mydev")
    spec_file = tmp_path / "mydev" / "src" / "mydev_ioc" / "pyioc.yaml"
    assert cli(["check", "--spec", str(spec_file)]) == 0
    assert cli(["render", "--spec", str(spec_file), str(tmp_path / "mydev" / "example.yaml")]) == 0
    out = capsys.readouterr().out
    assert "TEST:MYDEV:DEV01:" in out
    compile((tmp_path / "mydev" / "src" / "mydev_ioc" / "ioc.py").read_text(), "ioc.py", "exec")
    with pytest.raises(FileExistsError):
        scaffold("mydev", tmp_path / "mydev")
