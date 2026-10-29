#!/usr/bin/env python3
"""Syntax-check JavaScript only under custom_components/dooods and local_apps/dooods2."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_COMPONENT_DIRS = (Path("custom_components/dooods"), Path("local_apps/dooods2"))
_JS_TYPES = {
    "",
    "application/ecmascript",
    "application/javascript",
    "text/ecmascript",
    "text/javascript",
    "module",
}


class _InlineScriptParser(HTMLParser):
    """Collect executable inline script blocks from an HTML document."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[tuple[str, str]] = []
        self._active = False
        self._module = False
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attributes = dict(attrs)
        script_type = (attributes.get("type") or "").strip().lower()
        if "src" in attributes or script_type not in _JS_TYPES:
            self._active = False
            return
        self._active = True
        self._module = script_type == "module"
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._active:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._active:
            self.blocks.append((".mjs" if self._module else ".js", "".join(self._parts)))
            self._active = False
            self._parts = []


def _check(node: str, path: Path, label: str, root: Path) -> str | None:
    try:
        result = subprocess.run(
            [node, "--check", str(path)], cwd=root, capture_output=True, text=True, check=False
        )
    except OSError as err:
        return f"could not run Node.js for {label}: {err}"
    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or "node --check failed"
    return f"JavaScript syntax error in {label}:\n{detail}"


def validate_javascript(root: Path = ROOT, node: str = "node") -> list[str]:
    """Check external and inline JS files in the integration and App directories."""
    root = root.resolve()
    errors: list[str] = []
    js_files: set[Path] = set()
    html_files: set[Path] = set()
    for relative in _COMPONENT_DIRS:
        base = root / relative
        if not base.is_dir():
            errors.append(f"component directory is missing: {relative.as_posix()}")
            continue
        js_files.update(base.rglob("*.js"))
        js_files.update(base.rglob("*.mjs"))
        html_files.update(base.rglob("*.html"))

    checked = 0
    for path in sorted(js_files):
        if path.is_file():
            label = path.relative_to(root).as_posix()
            error = _check(node, path, label, root)
            if error:
                errors.append(error)
            checked += 1

    with tempfile.TemporaryDirectory(prefix="dooods-component-js-") as temp:
        temp_root = Path(temp)
        for html_path in sorted(html_files):
            try:
                parser = _InlineScriptParser()
                parser.feed(html_path.read_text(encoding="utf-8"))
                parser.close()
            except (OSError, UnicodeError) as err:
                errors.append(f"could not read {html_path.relative_to(root).as_posix()}: {err}")
                continue
            for index, (suffix, source) in enumerate(parser.blocks, start=1):
                temp_path = temp_root / f"inline-{checked}-{index}{suffix}"
                temp_path.write_text(source, encoding="utf-8")
                label = f"{html_path.relative_to(root).as_posix()} (inline script {index})"
                error = _check(node, temp_path, label, root)
                if error:
                    errors.append(error)
                checked += 1

    if checked == 0:
        errors.append("no JavaScript files or inline scripts found in the two component directories")
    return errors


def main() -> int:
    errors = validate_javascript()
    if errors:
        print("JavaScript validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("JavaScript validation passed for custom_components/dooods and local_apps/dooods2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
