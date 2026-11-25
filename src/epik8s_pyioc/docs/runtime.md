# Runtime contract: from values.yaml to a running IOC

## How epik8s starts a Python IOC

```
values.yaml ─epik8s-chart─▶ ArgoCD Application ─ioc-chart─▶ pod (StatefulSet)
  iocDefaults[devtype] merged                      /tmp/iocconfig.yaml = the merged entry
  under the IOC entry                              start: pyioc-run
                                                     ├─ finds the IOC's pyioc.yaml (by devtype)
                                                     ├─ validates the entry, applies defaults
                                                     └─ calls entrypoint(instance)
```

1. **epik8s-chart** merges `iocDefaults.<template or devtype>` under the IOC entry
   (Sprig `mustMergeOverwrite`: the entry wins, but an empty value in it, such as `false`,
   `0` or `""`, does not replace a non-empty default) and injects `beamline`, `iocname`,
   `nfsMounts`.
2. **ioc-chart** writes the result to a ConfigMap and copies it to `/tmp/iocconfig.yaml`.
   It adds `config_dir`/`<mount>_dir`, `ioctop`, and each `iocparam` as a top-level key.
3. The pod's start script takes the **`template:` branch** if the entry has a template
   (ibek/jnjrender, which a Python image does not have), else runs `./start.sh` from the
   config directory if there is one, else the entry's **`start:`** command.

So a Python IOC entry must:

- select its defaults with **`devtype`**, never `template`;
- start with **`start: pyioc-run`** (in `iocDefaults`);
- say **`pva: false`** if it serves only Channel Access (the caproto IOCs of pyioc 0.2.x
  do; IOCs generated on iocmng serve CA and PVA). epik8s-chart then gives the pod a static
  IP, as for other CA-only IOCs.

`pyioc defaults` prints that block, and `pyioc validate` checks a beamline for all three.

## The image

- It installs the IOC package and `epik8s-pyioc`, and registers the spec as an entry point:

  ```toml
  [project.entry-points."epik8s.pyioc"]
  thorlabscct = "thorlabscct10_ioc"       # the package that contains pyioc.yaml

  [tool.setuptools.package-data]
  thorlabscct10_ioc = ["pyioc.yaml"]
  ```

  One image may hold several IOCs: `pyioc-run` picks the one matching the entry's
  `devtype`. Without entry points it uses `$PYIOC_SPEC` or `/epics/pyioc/pyioc.yaml`.
- It runs as an arbitrary uid. ioc-chart uses 1000, which has no home directory in most
  images: `pyioc-run` sets `HOME=/tmp` when `$HOME` is not writable (the .NET runtime, for
  one, needs it).
- Vendor libraries that may not be redistributed stay **out of public images**. Publish the
  image without them, and give the IOC a path to them: a parameter such as `sdk_path`
  pointing at a directory on the beamline's NFS `config` share, which ioc-chart mounts in
  every pod (`/nfs/config` at SPARC).
- Public images on `ghcr.io` are pulled through the cluster's HTTP proxy on port 443. A
  registry on another port (GitLab's `baltig.infn.it:4567`) may be blocked by the proxy and
  the firewall.

## The entrypoint

```python
def run(instance):                 # epik8s_pyioc.Instance, already validated
    instance.iocname, instance.iocprefix
    instance.log_level             # an IOC parameter (also instance["log_level"])
    for device in instance.devices:
        device.name, device.prefix # prefix from the spec's templates, e.g. SPARC:DIAG:CCT01:
        device.server, device.exposure
    instance.raw                   # the whole merged entry, for anything else
    return 0                       # exit status
```

## The chart's requirements

- ioc-chart from `dfe4b80` on: with an NFS `config` mount, older charts fail to start an IOC
  whose config directory is empty (`cp -r <dir>/*`). A Python IOC needs no
  `config/iocs/<dir>` in the beamline repository.
- An ArgoCD application without `autosync: true` must be synced by hand. A StatefulSet pod
  stuck in `ImagePullBackOff` is not replaced by a rollout: delete it after fixing the image.
