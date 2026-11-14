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

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPO_URL = "https://github.com/ingbrzy/Xiaomi.eu-HyperOS-4.0-XML-Global.git"
INVALID_QUALIFIER_RE = re.compile(r"(?:^|-)(?:mcc|mnc)\d{4,}(?:-|$)")


def sparse_clone(repo_url: str, dest: Path, variants: list[str], lang: str) -> None:
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse", repo_url, str(dest)],
        check=True,
    )
    sparse_paths = [f"{variant}/{lang}" for variant in variants]
    subprocess.run(
        ["git", "-C", str(dest), "sparse-checkout", "set", *sparse_paths],
        check=True,
    )


def sync_overlays(
    source_dir: Path,
    overlays_dir: Path,
    package_map: dict[str, str],
    variants: list[str],
    lang: str,
    clean: bool,
) -> tuple[list[str], list[str]]:
    synced_packages: set[str] = set()
    synced_records: list[str] = []
    unmapped: set[str] = set()

    for variant in variants:
        main_dir = source_dir / variant / lang / "main"
        if not main_dir.is_dir():
            print(f"[WARN] Variant path not found: {main_dir}", file=sys.stderr)
            continue

        for apk_dir in sorted(d for d in main_dir.iterdir() if d.is_dir()):
            target_pkg = package_map.get(apk_dir.name)
            if not target_pkg:
                unmapped.add(apk_dir.name)
                continue

            if target_pkg in synced_packages:
                continue

            src_res = apk_dir / "res"
            if not src_res.is_dir():
                continue

            dst_overlay = overlays_dir / target_pkg
            dst_res = dst_overlay / "res"
            if clean and dst_res.exists():
                shutil.rmtree(dst_res)
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

            synced_packages.add(target_pkg)
            synced_records.append(f"{apk_dir.name} -> {target_pkg} ({variant})")

    return synced_records, sorted(unmapped)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync XML resources from Xiaomi.eu XML repository.")
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Source Git repository URL")
    parser.add_argument("--source-dir", type=Path, default=None, help="Use an existing local clone instead of cloning")
    parser.add_argument("--package-map", type=Path, default=ROOT_DIR / "package_map.json", help="Path to package_map.json")
    parser.add_argument("--overlays-dir", type=Path, default=ROOT_DIR / "overlays", help="Destination overlays directory")
    parser.add_argument("--variants", default="qcom-sdk37,mtk-sdk37", help="Comma-separated priority list of variants")
    parser.add_argument("--lang", default="Vietnamese", help="Language folder name in Xiaomi.eu repo")
    parser.add_argument("--no-clean", action="store_true", help="Do not remove existing res/ before copying")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.package_map.is_file():
        print(f"Error: package map not found: {args.package_map}", file=sys.stderr)
        return 1

    package_map: dict[str, str] = json.loads(args.package_map.read_text(encoding="utf-8"))
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    args.overlays_dir.mkdir(parents=True, exist_ok=True)

    if args.source_dir is not None:
        synced, unmapped = sync_overlays(
            args.source_dir,
            args.overlays_dir,
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
                args.overlays_dir,
                package_map,
                variants,
                args.lang,
                clean=not args.no_clean,
            )

    print(f"Synced {len(synced)} overlay(s) for {args.lang}:")
    for item in synced:
        print(f"  + {item}")

    if unmapped:
        print(f"\nSkipped {len(unmapped)} unmapped APK(s): {', '.join(unmapped)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
