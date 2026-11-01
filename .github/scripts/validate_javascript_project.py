#!/usr/bin/env python3
"""Syntax-check integration/App JavaScript, including inline scripts in App HTML.

Uses analysis/application when present, otherwise local_apps/dooods2/application.
Requires Node.js on PATH. Run from any directory with:
    python .github/scripts/validate_javascript_project.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
_JS_TYPES = {
    "",
    "application/ecmascript",
    "application/javascript",
    "text/ecmascript",
    "text/javascript",
    "module",
}


class _InlineScriptParser(HTMLParser):
    """Extract executable inline script blocks from an HTML document."""

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
            suffix = ".mjs" if self._module else ".js"
            self.blocks.append((suffix, "".join(self._parts)))
            self._active = False
            self._parts = []


def _inline_scripts(path: Path) -> list[tuple[str, str]]:
    """Return executable script blocks found in an HTML file."""
    parser = _InlineScriptParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser.blocks


def _node_check(node: str, path: Path, label: str, root: Path) -> str | None:
    """Run Node's parser and return a diagnostic on failure."""
    try:
        result = subprocess.run(
            [node, "--check", str(path)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:
        return f"could not run Node.js for {label}: {err}"

    if result.returncode == 0:
        return None
    detail = result.stderr.strip() or result.stdout.strip() or "node --check failed"
    return f"JavaScript syntax error in {label}:\n{detail}"


def _app_source_root(root: Path) -> Path:
    """Use the authoritative source when present, otherwise the App build context."""
    authoritative = root / "analysis" / "application"
    if authoritative.is_dir():
        return authoritative
    return root / "local_apps" / "dooods2" / "application"


def validate_javascript(root: Path = ROOT, node: str = "node") -> list[str]:
    """Check integration and available App JavaScript; do not modify the tree."""
    root = root.resolve()
    errors: list[str] = []
    js_files: set[Path] = set()
    html_files: set[Path] = set()
    app_source = _app_source_root(root)

    for project_base in (root / "custom_components" / "dooods", app_source):
        if project_base.is_dir():
            js_files.update(project_base.rglob("*.js"))
            js_files.update(project_base.rglob("*.mjs"))

    app_html = app_source / "html"
    if app_html.is_dir():
        html_files.update(app_html.rglob("*.html"))

    checked_files = 0
    for path in sorted(js_files):
        if not path.is_file():
            continue
        label = path.relative_to(root).as_posix()
        error = _node_check(node, path, label, root)
        if error:
            errors.append(error)
        checked_files += 1

    with tempfile.TemporaryDirectory(prefix="dooods-js-check-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        for html_path in sorted(html_files):
            try:
                blocks = _inline_scripts(html_path)
            except (OSError, UnicodeError) as err:
                errors.append(
                    f"could not read HTML for JavaScript validation: "
                    f"{html_path.relative_to(root).as_posix()}: {err}"
                )
                continue

            relative_html = html_path.relative_to(root).as_posix()
            for index, (suffix, source) in enumerate(blocks, start=1):
                temporary_path = temporary_root / f"inline-{checked_files}-{index}{suffix}"
                temporary_path.write_text(source, encoding="utf-8")
                label = f"{relative_html} (inline script {index})"
                error = _node_check(node, temporary_path, label, root)
                if error:
                    errors.append(error)
                checked_files += 1

    if not checked_files:
        errors.append("no JavaScript files or inline scripts were found")
    return errors


def main() -> int:
    """Run syntax checks and return a conventional process exit status."""
    errors = validate_javascript()
    if errors:
        print("JavaScript validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("JavaScript syntax validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
