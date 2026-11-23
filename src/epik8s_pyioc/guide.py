"""The epik8s-pyioc documentation, shipped with the package (for ``pyioc`` and ARGUS)."""

from __future__ import annotations

from importlib import resources

ORDER = ("howto", "specification", "runtime", "pitfalls", "tools")


def guide() -> dict[str, str]:
    """{document title: Markdown text}, in reading order."""
    docs = resources.files(__package__).joinpath("docs")
    out: dict[str, str] = {}
    names = sorted((p.name[:-3] for p in docs.iterdir() if p.name.endswith(".md")),
                   key=lambda n: (ORDER.index(n) if n in ORDER else len(ORDER), n))
    for name in names:
        text = docs.joinpath(f"{name}.md").read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip() if text else name
        out[title] = text
    return out
