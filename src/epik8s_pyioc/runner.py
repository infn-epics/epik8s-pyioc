"""``pyioc-run``: the single start command of every containerized Python IOC.

ioc-chart writes the IOC entry of values.yaml (merged with iocDefaults) to
/tmp/iocconfig.yaml.  pyioc-run finds the IOC's pyioc.yaml spec, validates the
entry against it and calls the spec ``entrypoint`` with the resolved Instance.
"""

from __future__ import annotations

import argparse
from importlib import import_module, metadata, resources
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable

from .instance import Instance, InstanceError, load_instance, resolve_instance
from .spec import Spec, SpecError, load_spec, parse_spec

ENTRY_POINT_GROUP = "epik8s.pyioc"
DEFAULT_CONFIG = "/tmp/iocconfig.yaml"
IMAGE_SPEC = "/epics/pyioc/pyioc.yaml"
SPEC_FILENAME = "pyioc.yaml"


def installed_specs() -> dict[str, Spec]:
    """Specs registered by installed packages.

    A package registers its IOC with::

        [project.entry-points."epik8s.pyioc"]
        thorlabscct = "thorlabscct10_ioc"     # package containing pyioc.yaml
    """
    import yaml

    specs: dict[str, Spec] = {}
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        package, _, resource = entry.value.partition(":")
        resource = resource or SPEC_FILENAME
        try:
            text = resources.files(package).joinpath(resource).read_text()
        except (ModuleNotFoundError, FileNotFoundError, OSError) as exc:
            raise SpecError(f"entry point {entry.name} = {entry.value}: {exc}") from exc
        spec = parse_spec(yaml.safe_load(text) or {}, Path(f"<{package}/{resource}>"))
        if spec.name != entry.name:
            raise SpecError(f"entry point {entry.name!r} registers spec named {spec.name!r}")
        specs[spec.name] = spec
    return specs


def find_spec(spec_path: str | None = None, devtype: str | None = None) -> Spec:
    """``--spec`` > $PYIOC_SPEC > installed entry points (by devtype) > /epics/pyioc/pyioc.yaml."""
    path = spec_path or os.environ.get("PYIOC_SPEC")
    if path:
        return load_spec(path)
    specs = installed_specs()
    if devtype and devtype in specs:
        return specs[devtype]
    if len(specs) == 1 and not devtype:
        return next(iter(specs.values()))
    if Path(IMAGE_SPEC).is_file():
        return load_spec(IMAGE_SPEC)
    if len(specs) == 1:
        return next(iter(specs.values()))
    if not specs:
        raise SpecError(f"no pyioc spec found (use --spec, $PYIOC_SPEC, an '{ENTRY_POINT_GROUP}' "
                        f"entry point or {IMAGE_SPEC})")
    raise SpecError(f"devtype {devtype!r} matches none of the installed IOCs: {', '.join(sorted(specs))}")


def load_entrypoint(spec: Spec) -> Callable[[Instance], Any]:
    module_name, _, attr = spec.entrypoint.partition(":")
    try:
        return getattr(import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise SpecError(f"cannot load entrypoint {spec.entrypoint}: {exc}") from exc


def _read_devtype(config_path: str) -> str | None:
    import yaml

    try:
        data = yaml.safe_load(Path(config_path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    return data.get("devtype") if isinstance(data, dict) else None


def prepare_environment() -> None:
    # ioc-chart runs pods as uid 1000, normally without a home directory in the image.
    home = os.environ.get("HOME")
    if not home or not os.access(home, os.W_OK):
        os.environ["HOME"] = "/tmp"


def main_run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyioc-run", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", nargs="?",
                        default=os.environ.get("IOC_CONFIG", DEFAULT_CONFIG),
                        help="IOC instance YAML (default: $IOC_CONFIG or %(default)s)")
    parser.add_argument("--spec", help="pyioc.yaml to use instead of discovery")
    parser.add_argument("--devtype", help="select an installed IOC (default: 'devtype' of the config)")
    parser.add_argument("--check", action="store_true",
                        help="validate and print the resolved instance as JSON, do not start")
    args = parser.parse_args(argv)

    try:
        spec = find_spec(args.spec, args.devtype or _read_devtype(args.config))
        instance = load_instance(spec, args.config)
    except SpecError as exc:
        print(f"pyioc-run: spec error: {exc}", file=sys.stderr)
        return 2
    except InstanceError as exc:
        print(f"pyioc-run: invalid configuration {args.config} for {spec.name}:", file=sys.stderr)
        for error in exc.errors:
            print(f"  - {error}", file=sys.stderr)
        return 2
    for warning in instance.warnings:
        print(f"pyioc-run: warning: {warning}", file=sys.stderr)
    if args.check:
        print(json.dumps(instance.summary(), indent=2, default=str))
        return 0
    prepare_environment()
    try:
        entrypoint = load_entrypoint(spec)
    except SpecError as exc:
        print(f"pyioc-run: {exc}", file=sys.stderr)
        return 2
    result = entrypoint(instance)
    return result if isinstance(result, int) else 0


def run(spec: Spec, config: dict) -> Any:
    """Programmatic equivalent of pyioc-run (useful in tests)."""
    return load_entrypoint(spec)(resolve_instance(spec, config))


if __name__ == "__main__":
    sys.exit(main_run())
