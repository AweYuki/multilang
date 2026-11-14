#!/usr/bin/env python3
"""Shared helpers for managing untranslated, pending, and translated XML resources."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

TARGET_XML_NAMES = ("strings.xml", "arrays.xml", "plurals.xml")
RESOURCE_REF_ONLY_RE = re.compile(r"^\s*@(?:[a-zA-Z0-9_.]+:)?[a-zA-Z0-9_]+/[a-zA-Z0-9_.]+\s*$")


def serialize_element(elem: ET.Element) -> str:
    return ET.tostring(elem, encoding="unicode").strip()


def load_xml_entries(xml_path: Path) -> dict[tuple[str, str], ET.Element]:
    if not xml_path.is_file():
        return {}
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return {}
    entries: dict[tuple[str, str], ET.Element] = {}
    for child in root:
        name = child.attrib.get("name")
        if name:
            entries[(child.tag, name)] = child
    return entries


def is_translatable_entry(elem: ET.Element) -> bool:
    if elem.attrib.get("translatable", "").lower() == "false":
        return False
    text = "".join(elem.itertext()).strip()
    if not text:
        return False
    if elem.tag == "string" and RESOURCE_REF_ONLY_RE.match(text):
        return False
    return True


def write_resources_xml(xml_path: Path, entries: list[ET.Element]) -> None:
    if not entries:
        xml_path.unlink(missing_ok=True)
        return
    root = ET.Element("resources")
    for elem in entries:
        root.append(elem)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ")
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(xml_path, encoding="utf-8", xml_declaration=True)


def compute_pending_for_package(
    untranslated_values_dir: Path,
    prev_untranslated_values_dir: Path | None,
    translated_values_dir: Path,
    pending_values_dir: Path,
) -> int:
    """Compute pending (untranslated or source-updated) XML entries for one package."""
    total_pending = 0
    for xml_name in TARGET_XML_NAMES:
        src_entries = load_xml_entries(untranslated_values_dir / xml_name)
        prev_entries = (
            load_xml_entries(prev_untranslated_values_dir / xml_name)
            if prev_untranslated_values_dir
            else {}
        )
        translated_entries = load_xml_entries(translated_values_dir / xml_name)

        pending_list: list[ET.Element] = []
        for key, elem in src_entries.items():
            if not is_translatable_entry(elem):
                continue
            already_translated = key in translated_entries
            source_updated = (
                key in prev_entries
                and serialize_element(prev_entries[key]) != serialize_element(elem)
            )
            if not already_translated or source_updated:
                pending_list.append(elem)

        write_resources_xml(pending_values_dir / xml_name, pending_list)
        total_pending += len(pending_list)

    if total_pending == 0 and pending_values_dir.parent.parent.exists():
        shutil.rmtree(pending_values_dir.parent.parent, ignore_errors=True)
    return total_pending


def compute_pending_for_version(
    untranslated_dir: Path,
    overlays_dir: Path,
    pending_dir: Path,
    prev_untranslated_dir: Path | None = None,
    target_lang: str = "vi",
) -> tuple[int, int]:
    """Recompute pending/<version>/ from untranslated/<version>/ and overlays/<version>/."""
    if not untranslated_dir.is_dir():
        return 0, 0

    pkg_count = 0
    entry_count = 0
    for pkg_dir in sorted(d for d in untranslated_dir.iterdir() if d.is_dir()):
        src_values = pkg_dir / "res" / "values"
        if not src_values.is_dir():
            continue
        prev_values = (
            prev_untranslated_dir / pkg_dir.name / "res" / "values"
            if prev_untranslated_dir
            else None
        )
        translated_values = overlays_dir / pkg_dir.name / "res" / f"values-{target_lang}"
        pending_values = pending_dir / pkg_dir.name / "res" / "values"
        count = compute_pending_for_package(src_values, prev_values, translated_values, pending_values)
        if count > 0:
            pkg_count += 1
            entry_count += count
    return pkg_count, entry_count


def ensure_overlay_metadata(overlay_pkg_dir: Path, target_package: str) -> None:
    meta_file = overlay_pkg_dir / "overlay.json"
    if not meta_file.is_file():
        overlay_pkg_dir.mkdir(parents=True, exist_ok=True)
        meta_file.write_text(
            json.dumps(
                {
                    "targetPackage": target_package,
                    "priority": 100,
                    "isStatic": True,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
