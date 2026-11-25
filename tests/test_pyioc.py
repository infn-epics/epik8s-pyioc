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
    assert "TEST:MYDEV:DEV01:" in capsys.readouterr().out
    for path in (tmp_path / "mydev").rglob("*.py"):
        compile(path.read_text(), str(path), "exec")
    with pytest.raises(FileExistsError):
        scaffold("mydev", tmp_path / "mydev")


def test_render_project_from_a_description():
    from epik8s_pyioc.scaffold import EXAMPLE, render_project

    project = render_project(EXAMPLE)
    assert project["kind"] == "device"
    assert project["self_check"]["ok"], project["self_check"]
    files = project["files"]
    assert {"pyproject.toml", "Dockerfile", ".github/workflows/ci.yml", "example.yaml",
            "src/tpg366_ioc/pyioc.yaml", "src/tpg366_ioc/config.yaml", "src/tpg366_ioc/ioc.py",
            "src/tpg366_ioc/driver.py", "tests/test_ioc.py"} <= set(files)
    ioc = files["src/tpg366_ioc/ioc.py"]
    assert "class DeviceIOC(TaskBase)" in ioc and "AsyncioDispatcher" in ioc
    assert '"LastErrorLong"' in ioc and "padded_chars" in ioc           # the pyioc PV names are kept
    assert "TcpDriver" in files["src/tpg366_ioc/driver.py"]
    assert "iocmng>=" in files["pyproject.toml"] and "config.yaml" in files["pyproject.toml"]
    assert "FROM ${BASE}" in files["Dockerfile"] and "HEALTHCHECK NONE" in files["Dockerfile"]
    spec = yaml.safe_load(files["src/tpg366_ioc/pyioc.yaml"])
    assert spec["name"] == "tpg366" and spec["entrypoint"] == "tpg366_ioc.ioc:run"
    assert spec["protocols"] == ["ca", "pva"]
    assert spec["devices"]["parameters"]["port"]["default"] == 8000
    config = yaml.safe_load(files["src/tpg366_ioc/config.yaml"])
    assert config["arguments"]["outputs"]["Pressure"] == {
        "type": "float", "source": "driver", "value": 0.0, "unit": "mbar", "prec": 3}
    assert config["arguments"]["inputs"]["Unit"]["access"] == "rw"
    assert config["arguments"]["inputs"]["Reset"] == {"type": "bool", "source": "driver", "access": "cmd"}


def test_a_device_with_derived_values_and_rules():
    from epik8s_pyioc.scaffold import EXAMPLE, render_project

    project = render_project({**EXAMPLE, "device_parameters": {"limit": {"type": "float", "default": 1.0}},
                              "pvs": EXAMPLE["pvs"] + [
        {"name": "Upstream", "type": "float", "source": {"link": "SPARC:VAC:GAUGE02:Pressure"}},
        {"name": "Ratio", "type": "float", "source": {"expr": "Pressure / Upstream"}},
        {"name": "Leak", "type": "bool", "source": "soft", "latch": True}],
        "rules": [{"id": "LEAK", "condition": "Pressure > limit", "outputs": {"Leak": 1}}]})
    assert project["self_check"]["ok"], project["self_check"]
    files = project["files"]
    config = yaml.safe_load(files["src/tpg366_ioc/config.yaml"])
    assert config["arguments"]["inputs"]["Upstream"]["link"] == "SPARC:VAC:GAUGE02:Pressure"
    assert "trigger" not in config["arguments"]["inputs"]["Upstream"]         # continuous mode
    assert config["transforms"] == [{"output": "Ratio", "expression": "Pressure / Upstream"}]
    chained = render_project({**EXAMPLE, "pvs": EXAMPLE["pvs"] + [
        {"name": "Double", "type": "float", "source": {"expr": "Pressure * 2"}},
        {"name": "Quad", "type": "float", "source": {"expr": "Double * 2"}}]})      # earlier expr: visible
    assert chained["self_check"]["ok"], chained["self_check"]
    assert config["rules"][0]["outputs"] == {"Leak": 1}
    spec = yaml.safe_load(files["src/tpg366_ioc/pyioc.yaml"])
    assert spec["parameters"]["link_protocol"]["default"] == "ca"            # it has links


def test_render_a_box():
    from epik8s_pyioc.scaffold import EXAMPLE_BOX, render_project

    project = render_project(EXAMPLE_BOX)
    assert project["kind"] == "box"
    assert project["self_check"]["ok"], project["self_check"]
    files = project["files"]
    assert set(files) == {"README.md", "config.yaml", "start.sh.j2", "tests/test_box.py"}
    config = yaml.safe_load(files["config.yaml"])
    assert config["parameters"] == {"mode": "reactive", "interval": 1.0, "ratio": 3.0}
    p1 = config["arguments"]["inputs"]["P1"]
    assert p1["link"] == "SPARC:VAC:GAUGE01:Pressure" and p1["trigger"] is True   # reactive: links trigger
    assert config["arguments"]["outputs"]["Leak"]["latch"] is True
    assert "iocmng.declarative" in files["start.sh.j2"] and "{{iocprefix}}" in files["start.sh.j2"]
    assert "softiocs:" in files["README.md"]
    with_python = render_project({**EXAMPLE_BOX, "python": True})["files"]
    assert "vac_leak_box.py" in with_python and "--module vac_leak_box" in with_python["start.sh.j2"]
    assert "class VacLeak(DeclarativeTask)" in with_python["vac_leak_box.py"]


def test_scaffold_a_box(tmp_path, capsys):
    assert cli(["init", "mybox", "--box", "--dir", str(tmp_path / "mybox")]) == 0
    assert (tmp_path / "mybox" / "config.yaml").is_file()
    assert (tmp_path / "mybox" / "start.sh.j2").is_file()


@pytest.mark.parametrize("change, message", [
    ({"name": "Bad Name"}, "name"),
    ({"pvs": []}, "pvs"),
    ({"pvs": [{"name": "X", "type": "complex"}]}, "type"),
    ({"pvs": [{"name": "X", "type": "enum", "choices": ["only"]}]}, "choices"),
    ({"pvs": [{"name": "X", "type": "float", "access": "cmd"}]}, "cmd"),
    ({"pvs": [{"name": "X", "type": "waveform"}]}, "length"),
    ({"pvs": [{"name": "Connected"}]}, "generated"),
    ({"pvs": [{"name": "STATUS"}]}, "generated"),
    ({"pvs": [{"name": "A"}, {"name": "a"}]}, "duplicate"),
    ({"parameters": {"template": "string"}}, "reserved"),
    ({"transport": "carrier-pigeon"}, "transport"),
    ({"colour": "blue"}, "unknown field"),
    ({"pvs": [{"name": "X", "source": "magic"}]}, "source"),
    ({"pvs": [{"name": "X", "source": {"link": "has space"}}]}, "not a PV name"),
    ({"pvs": [{"name": "X", "source": {"link": "A:B", "mode": "push"}}]}, "link mode"),
    ({"pvs": [{"name": "X", "access": "rw", "source": {"link": "A:B"}}]}, "read only"),
    ({"pvs": [{"name": "X", "type": "bool", "access": "cmd", "source": "soft"}]}, "needs source: driver"),
    ({"pvs": [{"name": "X", "type": "waveform", "length": 4, "source": "soft"}]}, "only a driver PV"),
    ({"pvs": [{"name": "X", "source": {"expr": "1 +"}}]}, "not an expression"),
    ({"pvs": [{"name": "A"}, {"name": "X", "source": {"expr": "B * 2"}}]}, "unknown name 'B'"),
    ({"pvs": [{"name": "A"}, {"name": "S", "source": "soft"}, {"name": "X", "source": {"expr": "S * 2"}}]},
     "S is a soft PV"),
    ({"pvs": [{"name": "A"}, {"name": "X", "source": {"expr": "mean(A_buf)"}}]}, "unknown name 'A_buf'"),
    ({"pvs": [{"name": "A"}, {"name": "X", "source": {"expr": "Y"}}, {"name": "Y", "source": {"expr": "A"}}]},
     "Y is a expr PV"),
    ({"pvs": [{"name": "A"}, {"name": "X", "source": {"expr": "A"}}],
      "rules": [{"id": "R", "condition": "X > 1", "outputs": {"A": 0}}]}, "X is a expr PV"),
    ({"rules": [{"id": "R", "condition": "Pressure > 1"}]}, "sets outputs"),
    ({"rules": [{"id": "R", "condition": "Pressure > 1", "outputs": {"Pressure": 0}}]}, "soft PV or a driver"),
    ({"rules": [{"id": "R", "condition": "Pressure > 1", "actuators": {"Unit": 0}}]}, "must be a link PV"),
    ({"rules": [{"id": "R", "condition": "Pressure >", "outputs": {"Unit": 0}}]}, "not an expression"),
    ({"rules": [{"id": "R", "condition": "1", "tolerance": -1, "outputs": {"Unit": 0}}]}, "tolerance"),
    ({"rules": [{"id": "R", "condition": "1", "outputs": {"Unit": 0}}] * 2}, "duplicate id"),
    ({"rule_defaults": {"Pressure": 0}}, "rule_defaults"),
    ({"mode": "sometimes"}, "mode"),
    ({"python": True}, "always has its Python class"),
])
def test_bad_descriptions_report_every_problem(change, message):
    from epik8s_pyioc.scaffold import EXAMPLE, ScaffoldError, render_project

    with pytest.raises(ScaffoldError) as info:
        render_project({**EXAMPLE, **change})
    assert any(message in e for e in info.value.errors), info.value.errors


@pytest.mark.parametrize("change, message", [
    ({"transport": "tcp"}, "needs at least one PV with source: driver"),
    ({"parameters": {"mode": "x"}}, "set by the generator"),
    ({"parameters": {"ratio": [1, 2]}}, "a number, a string or a bool"),
    ({"device_parameters": {"x": {"type": "int"}}}, "only a device IOC"),
    ({"pvs": [{"name": "ratio", "source": "soft"}]}, "same name as a parameter"),
])
def test_bad_boxes(change, message):
    from epik8s_pyioc.scaffold import EXAMPLE_BOX, ScaffoldError, render_project

    with pytest.raises(ScaffoldError) as info:
        render_project({**EXAMPLE_BOX, **change})
    assert any(message in e for e in info.value.errors), info.value.errors


def test_guide_ships_every_document():
    from epik8s_pyioc.guide import guide

    docs = guide()
    titles = list(docs)
    assert titles[0].startswith("How to")
    assert any("specification" in t for t in titles) and any("Pitfalls" in t for t in titles)
    assert all(text.strip() for text in docs.values())
    assert "enum_index" in next(text for t, text in docs.items() if "Pitfalls" in t)
