#!/usr/bin/env python3
"""Sync language XML resources from Xiaomi.eu XML repo into overlays/."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPO_URL = "https://github.com/ingbrzy/Xiaomi.eu-HyperOS-4.0-XML-Global.git"
INVALID_QUALIFIER_RE = re.compile(r"(?:^|-)(?:mcc|mnc)\d{4,}(?:-|$)")
NON_STRING_REF_RE = re.compile(r"@(?!android:)([a-zA-Z0-9_]+)/([a-zA-Z0-9_.]+)")
VALID_ENTRY_NAME_RE = re.compile(r"^[a-zA-Z0-9_.]+$")


def sanitize_values_xml(res_dir: Path) -> None:
    """Remove entries referencing internal resources not defined in this overlay."""
    xml_files = sorted(res_dir.glob("values*/**/*.xml"))
    parsed: list[tuple[Path, ET.ElementTree]] = []
    defined: set[tuple[str, str]] = set()

    for xml_file in xml_files:
        try:
            tree = ET.parse(xml_file)
        except ET.ParseError:
            continue
        parsed.append((xml_file, tree))
        for child in tree.getroot():
            name = child.attrib.get("name")
            if not name:
                continue
            tag = child.tag
            if tag in ("string-array", "integer-array", "array"):
                defined.add(("array", name))
            else:
                defined.add((tag, name))

    for xml_file, tree in parsed:
        root = tree.getroot()
        changed = False
        for child in list(root):
            name = child.attrib.get("name")
            if name and not VALID_ENTRY_NAME_RE.match(name):
                root.remove(child)
                changed = True
                continue
            text = "".join(child.itertext())
            if any((rtype, rname) not in defined for rtype, rname in NON_STRING_REF_RE.findall(text)):
                root.remove(child)
                changed = True
        if changed:
            ET.indent(tree, space="    ")
            tree.write(xml_file, encoding="utf-8", xml_declaration=True)


def sparse_clone(repo_url: str, dest: Path, variants: list[str], lang: str) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo_url, str(dest)],
        check=True,
    )
    if lang.lower() == "all":
        if any(v in (".", "") for v in variants):
            subprocess.run(["git", "-C", str(dest), "sparse-checkout", "disable"], check=True)
            return
        sparse_paths = list(variants)
    else:
        sparse_paths = [
            lang if variant in (".", "") else f"{variant}/{lang}"
            for variant in variants
        ]
    subprocess.run(
        ["git", "-C", str(dest), "sparse-checkout", "set", *sparse_paths],
        check=True,
    )


def discover_languages(source_dir: Path, variants: list[str], lang: str) -> list[str]:
    if lang.lower() != "all":
        return [lang]
    langs: set[str] = set()
    for variant in variants:
        variant_dir = source_dir if variant in (".", "") else source_dir / variant
        if variant_dir.is_dir():
            for child in variant_dir.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    langs.add(child.name)
    return sorted(langs)


def sync_overlays(
    source_dir: Path,
    overlays_dir: Path,
    package_map: dict[str, str],
    variants: list[str],
    lang: str,
    clean: bool,
) -> tuple[list[str], list[str]]:
    langs = discover_languages(source_dir, variants, lang)
    synced_pairs: set[tuple[str, str]] = set()
    cleaned_packages: set[str] = set()
    synced_packages: set[str] = set()
    unmapped: set[str] = set()

    for current_lang in langs:
        for variant in variants:
            variant_root = source_dir if variant in (".", "") else source_dir / variant
            main_dir = variant_root / current_lang / "main"
            if not main_dir.is_dir():
                continue

            for apk_dir in sorted(d for d in main_dir.iterdir() if d.is_dir()):
                target_pkg = package_map.get(apk_dir.name)
                if not target_pkg:
                    unmapped.add(apk_dir.name)
                    continue

                if (target_pkg, current_lang) in synced_pairs:
                    continue

                src_res = apk_dir / "res"
                if not src_res.is_dir():
                    continue

                dst_overlay = overlays_dir / target_pkg
                dst_res = dst_overlay / "res"
                if clean and target_pkg not in cleaned_packages:
                    if dst_res.exists():
                        shutil.rmtree(dst_res)
                    cleaned_packages.add(target_pkg)

                dst_res.mkdir(parents=True, exist_ok=True)
                shutil.copytree(
                    src_res,
                    dst_res,
                    dirs_exist_ok=True,
                    ignore=lambda _dir, names: [n for n in names if INVALID_QUALIFIER_RE.search(n)],
                )

                meta_file = dst_overlay / "overlay.json"
                if not meta_file.is_file():
                    meta_file.write_text(
                        json.dumps(
                            {
                                "targetPackage": target_pkg,
                                "priority": 100,
                                "isStatic": True,
                            },
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )

                synced_pairs.add((target_pkg, current_lang))
                synced_packages.add(target_pkg)

    for target_pkg in sorted(synced_packages):
        sanitize_values_xml(overlays_dir / target_pkg / "res")

    synced_records = [f"{pkg} ({len([1 for p, _ in synced_pairs if p == pkg])} lang(s))" for pkg in sorted(synced_packages)]
    return synced_records, sorted(unmapped)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync XML resources from Xiaomi.eu XML repository.")
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Source Git repository URL")
    parser.add_argument("--source-dir", type=Path, default=None, help="Use an existing local clone instead of cloning")
    parser.add_argument("--package-map", type=Path, default=ROOT_DIR / "package_map.json", help="Path to package_map.json")
    parser.add_argument("--overlays-dir", type=Path, default=ROOT_DIR / "overlays", help="Destination overlays root directory")
    parser.add_argument("--version", default="os4-a17", help="Target ROM version subfolder under overlays/ (e.g. os4-a17)")
    parser.add_argument("--variants", default="qcom-sdk37,mtk-sdk37", help="Comma-separated priority list of variants")
    parser.add_argument("--lang", default="Vietnamese", help="Language folder name in Xiaomi.eu repo (or 'all')")
    parser.add_argument("--no-clean", action="store_true", help="Do not remove existing res/ before copying")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.package_map.is_file():
        print(f"Error: package map not found: {args.package_map}", file=sys.stderr)
        return 1

    package_map: dict[str, str] = json.loads(args.package_map.read_text(encoding="utf-8"))
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    target_overlays_dir = args.overlays_dir / args.version if args.version else args.overlays_dir
    target_overlays_dir.mkdir(parents=True, exist_ok=True)

    if args.source_dir is not None:
        synced, unmapped = sync_overlays(
            args.source_dir,
            target_overlays_dir,
            package_map,
            variants,
            args.lang,
            clean=not args.no_clean,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="xiaomi_eu_xml_") as tmp:
            clone_dir = Path(tmp) / "repo"
            sparse_clone(args.repo_url, clone_dir, variants, args.lang)
            synced, unmapped = sync_overlays(
                clone_dir,
                target_overlays_dir,
                package_map,
                variants,
                args.lang,
                clean=not args.no_clean,
            )

    print(f"Synced {len(synced)} overlay(s) for {args.lang} into {target_overlays_dir}:")
    for item in synced:
        print(f"  + {item}")

    if unmapped:
        print(f"\nSkipped {len(unmapped)} unmapped APK(s): {', '.join(unmapped)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
