#!/usr/bin/env python3
"""Extract integration release notes by comparing adjacent changelog versions."""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

_VERSION_TEXT = r"\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?"
_VERSION = re.compile(_VERSION_TEXT)
_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$")
_RELEASE_HEADING = re.compile(
    rf"^DOOODS\s+(?:[—–-]\s+)?(?P<integration>{_VERSION_TEXT})"
    rf"(?:\s*/\s*(?:App\s+)?(?P<app>{_VERSION_TEXT}))?\s*$",
    re.IGNORECASE,
)


# Historical headers predate the current two-version format. Do not extend this
# set for new releases: their headings must always contain both versions.
_LEGACY_HEADINGS = frozenset({
    'DOOODS — 0.1.12 / App 0.1.12-dev',
    'DOOODS 0.1.10 / 0.1.11-dev',
    'DOOODS 0.1.9.1 / 0.1.11-dev',
    'DOOODS 0.1.9 / 0.1.8-dev',
    'DOOODS 0.1.9 / 0.1.7-dev',
    'DOOODS 0.1.8 / 0.1.7-dev',
    'DOOODS 0.1.7.6 / 0.1.7-dev',
    'DOOODS 0.1.7.5 / 0.1.7-dev',
    'DOOODS 0.1.7.4 / 0.1.7-dev',
    'DOOODS 0.1.7.3 / 0.1.4-dev',
    'DOOODS 0.1.7.2 / 0.1.3-dev',
    'DOOODS 0.1.7.1 / 0.1.2-dev',
    'DOOODS 0.1.7 / 0.1.2-dev',
    'DOOODS 0.1.6.1 / 0.1.1-dev',
    'DOOODS 0.1.6 / 0.1.1-dev',
    'DOOODS 0.1.5 / 0.1.1-dev',
    'DOOODS 0.1.4',
    'DOOODS 0.1.3',
    'DOOODS 0.1.2',
    'DOOODS 0.1.1',
})
_CANONICAL_HEADING = re.compile(
    rf"^DOOODS\s+{_VERSION_TEXT}\s*/\s*App\s+{_VERSION_TEXT}\s*$"
)


def validate_changelog_headings(changelog: str) -> None:
    """Require the two-version format, preserving known historical headings."""
    count = 0
    for number, line in enumerate(changelog.splitlines(), 1):
        heading = _HEADING.match(line)
        if heading is None:
            continue
        count += 1
        title = heading.group(1)
        if not _CANONICAL_HEADING.fullmatch(title) and title not in _LEGACY_HEADINGS:
            raise ValueError(
                f"Некорректный заголовок changelog в строке {number}: {title!r}. "
                "Формат: ### DOOODS <версия интеграции> / App <версия App>"
            )
    if not count:
        raise ValueError("В changelog нет заголовков версий")


@dataclass
class Entry:
    title: str
    integration: str | None
    body: list[str]


def extract_integration_changes(changelog: str, version: str) -> str:
    """Use the full entry only when its integration version differs from the next."""
    if not _VERSION.fullmatch(version):
        raise ValueError("Некорректная версия интеграции")
    validate_changelog_headings(changelog)
    entries: list[Entry] = []
    for line in changelog.splitlines():
        heading = _HEADING.match(line)
        if heading:
            title = heading.group(1)
            match = _RELEASE_HEADING.fullmatch(title)
            entries.append(Entry(title, match.group("integration") if match else None, []))
        elif entries:
            entries[-1].body.append(line)

    found_version = False
    selected: list[str] = []
    for index, entry in enumerate(entries):
        if entry.integration != version:
            continue
        found_version = True
        if index + 1 >= len(entries) or entries[index + 1].integration is None:
            raise ValueError(
                f"Для записи «{entry.title}» нет предыдущего заголовка с версией DOOODS"
            )
        previous = entries[index + 1]
        if previous.integration != entry.integration:
            text = "\n".join(entry.body).strip()
            if not text:
                raise ValueError(f"Раздел «{entry.title}» не содержит изменений")
            selected.append(text)
    if not found_version:
        raise ValueError(f"В changelog не найдена версия интеграции {version}")
    if not selected:
        raise ValueError(
            f"Для интеграции {version} не найдено изменение версии относительно предыдущей записи"
        )
    return "\n\n".join(selected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--changelog", type=Path, default=Path("docs/CHANGELOG.md"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only and (args.version is not None or args.output is not None):
        parser.error("--validate-only нельзя сочетать с --version или --output")
    if not args.validate_only and (args.version is None or args.output is None):
        parser.error("Для формирования описания нужны --version и --output")
    try:
        changelog = args.changelog.read_text(encoding="utf-8")
        if args.validate_only:
            validate_changelog_headings(changelog)
            print("Формат заголовков changelog проверен")
            return
        changes = extract_integration_changes(changelog, args.version)
    except (OSError, UnicodeError, ValueError) as err:
        raise SystemExit(f"Не удалось подготовить описание релиза: {err}") from err
    args.output.write_text(
        f"## Изменения интеграции DOOODS {args.version}\n\n{changes}\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
