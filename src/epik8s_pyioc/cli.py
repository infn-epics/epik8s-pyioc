"""``pyioc``: tooling around pyioc.yaml specs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .instance import InstanceError, load_instance
from .runner import find_spec, installed_specs
from .scaffold import ScaffoldError, scaffold
from .spec import SpecError, load_spec
from . import tools


def _spec(args):
    if args.spec:
        return load_spec(args.spec)
    if Path("pyioc.yaml").is_file():
        return load_spec("pyioc.yaml")
    return find_spec(None, getattr(args, "devtype", None))


def _dump_yaml(data) -> str:
    import yaml

    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyioc", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, help_text, spec=True):
        cmd = sub.add_parser(name, help=help_text)
        if spec:
            cmd.add_argument("--spec", "-s", help="pyioc.yaml (default: ./pyioc.yaml or installed IOC)")
            cmd.add_argument("--devtype", help="select an installed IOC")
        return cmd

    add("check", "validate a pyioc.yaml spec")
    add("docs", "print Markdown documentation of the parameters")
    cmd = add("schema", "print the JSON Schema of an IOC entry")
    cmd.add_argument("--fragment", action="store_true",
                     help="'if devtype == name then ...' clause for a beamline schema")
    cmd = add("defaults", "print the iocDefaults block for values.yaml")
    cmd.add_argument("--tag", help="image tag (default: spec version)")
    cmd = add("render", "resolve an IOC instance YAML and print it as JSON")
    cmd.add_argument("config")
    cmd = add("validate", "validate the IOCs of this devtype in a beamline values.yaml")
    cmd.add_argument("values", nargs="+")
    add("list", "list installed Python IOCs", spec=False)
    cmd = add("init", "create a new Python IOC project", spec=False)
    cmd.add_argument("name", help="devtype of the new IOC")
    cmd.add_argument("--dir", default=None, help="target directory (default: ./<name>)")
    cmd.add_argument("--package", help="Python package name")
    cmd.add_argument("--image", help="container image repository")
    cmd.add_argument("--devgroup", help="epik8s devgroup")
    cmd.add_argument("--from", dest="description", metavar="YAML",
                     help="device description (name, transport, pvs, ...); see epik8s_pyioc.scaffold")
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            for name, spec in sorted(installed_specs().items()):
                print(f"{name}\t{spec.entrypoint}\t{spec.description}")
            return 0
        if args.command == "init":
            description = None
            if args.description:
                import yaml

                description = yaml.safe_load(Path(args.description).read_text()) or {}
            created = scaffold(args.name, Path(args.dir or args.name), args.package,
                               args.image or "", args.devgroup or "", description)
            print("\n".join(str(path) for path in created))
            return 0
        spec = _spec(args)
        if args.command == "check":
            print(f"{spec.name}: OK ({len(spec.parameters)} IOC parameters, "
                  f"{len(spec.device_params())} device parameters)")
        elif args.command == "docs":
            print(tools.markdown(spec))
        elif args.command == "schema":
            print(json.dumps(tools.json_schema(spec, args.fragment), indent=2))
        elif args.command == "defaults":
            print(_dump_yaml({"iocDefaults": {spec.name: tools.ioc_defaults(spec, args.tag)}}), end="")
        elif args.command == "render":
            instance = load_instance(spec, args.config)
            for warning in instance.warnings:
                print(f"warning: {warning}", file=sys.stderr)
            print(json.dumps(instance.summary(), indent=2, default=str))
        elif args.command == "validate":
            import yaml

            failed = False
            for path in args.values:
                values = yaml.safe_load(Path(path).read_text()) or {}
                checked, errors, warnings = tools.validate_values(spec, values)
                for warning in warnings:
                    print(f"{path}: warning: {warning}")
                for error in errors:
                    print(f"{path}: error: {error}")
                print(f"{path}: {checked} {spec.name} IOC(s), {len(errors)} error(s)")
                failed |= bool(errors)
            return 1 if failed else 0
    except ScaffoldError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    except (SpecError, FileExistsError, ValueError) as exc:
        if isinstance(exc, InstanceError):
            for error in exc.errors:
                print(f"error: {error}", file=sys.stderr)
        else:
            print(f"pyioc: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
