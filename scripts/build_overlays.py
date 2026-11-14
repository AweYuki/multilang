#!/usr/bin/env python3
"""Build Runtime Resource Overlay (RRO) APKs for HyperOS CN language packs."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

ROOT_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Toolchain:
    aapt2: Path
    zipalign: Path
    apksigner: Path
    android_jar: Path


@dataclass(frozen=True)
class KeystoreConfig:
    path: Path
    store_pass: str
    alias: str
    key_pass: str


def parse_version_tuple(name: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", name)
    return tuple(int(p) for p in parts) if parts else (0,)


def find_sdk_root() -> Path | None:
    candidates = [
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        str(Path.home() / "Android" / "Sdk"),
        "/opt/android-sdk",
        "/usr/lib/android-sdk",
    ]
    for raw in candidates:
        if raw:
            p = Path(raw).expanduser()
            if p.is_dir():
                return p
    return None


def resolve_toolchain(compile_sdk: int) -> Toolchain:
    sdk_root = find_sdk_root()

    def find_tool(tool_name: str) -> Path | None:
        in_path = shutil.which(tool_name)
        if in_path:
            return Path(in_path)
        if sdk_root:
            bt_dir = sdk_root / "build-tools"
            if bt_dir.is_dir():
                versions = sorted(
                    (d for d in bt_dir.iterdir() if d.is_dir()),
                    key=lambda d: parse_version_tuple(d.name),
                    reverse=True,
                )
                for ver_dir in versions:
                    candidate = ver_dir / tool_name
                    if candidate.is_file():
                        return candidate
        return None

    aapt2 = find_tool("aapt2")
    zipalign = find_tool("zipalign")
    apksigner = find_tool("apksigner")

    android_jar: Path | None = None
    if sdk_root:
        candidate_jar = sdk_root / "platforms" / f"android-{compile_sdk}" / "android.jar"
        if candidate_jar.is_file():
            android_jar = candidate_jar

    missing = []
    if not aapt2:
        missing.append("aapt2")
    if not zipalign:
        missing.append("zipalign")
    if not apksigner:
        missing.append("apksigner")
    if not android_jar:
        missing.append(f"platforms/android-{compile_sdk}/android.jar")

    if missing:
        raise RuntimeError(
            "Missing Android SDK tools/files: "
            + ", ".join(missing)
            + ". Set ANDROID_HOME / ANDROID_SDK_ROOT or add build-tools to PATH."
        )

    assert aapt2 and zipalign and apksigner and android_jar
    return Toolchain(
        aapt2=aapt2,
        zipalign=zipalign,
        apksigner=apksigner,
        android_jar=android_jar,
    )


def ensure_keystore(build_dir: Path, args: argparse.Namespace) -> KeystoreConfig:
    ks_path_raw = args.keystore or os.environ.get("KEYSTORE_PATH")
    if ks_path_raw:
        ks_path = Path(ks_path_raw)
        if not ks_path.is_file():
            raise RuntimeError(f"Keystore file not found: {ks_path}")
        return KeystoreConfig(
            path=ks_path,
            store_pass=args.keystore_pass or os.environ.get("KEYSTORE_PASS", "android"),
            alias=args.key_alias or os.environ.get("KEY_ALIAS", "androiddebugkey"),
            key_pass=args.key_pass or os.environ.get("KEY_PASS", "android"),
        )

    debug_ks = build_dir / "debug.keystore"
    if not debug_ks.is_file():
        debug_ks.parent.mkdir(parents=True, exist_ok=True)
        keytool = shutil.which("keytool")
        if not keytool:
            raise RuntimeError("`keytool` not found in PATH; cannot generate debug keystore.")
        subprocess.run(
            [
                keytool,
                "-genkeypair",
                "-v",
                "-keystore",
                str(debug_ks),
                "-storepass",
                "android",
                "-alias",
                "androiddebugkey",
                "-keypass",
                "android",
                "-keyalg",
                "RSA",
                "-keysize",
                "2048",
                "-validity",
                "10000",
                "-dname",
                "CN=Android Debug,O=Android,C=US",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return KeystoreConfig(
        path=debug_ks,
        store_pass="android",
        alias="androiddebugkey",
        key_pass="android",
    )


def validate_xml_tree(res_dir: Path) -> list[Path]:
    xml_files = sorted(res_dir.rglob("*.xml"))
    if not xml_files:
        raise ValueError(f"No XML resource files found under {res_dir}")
    for xml_file in xml_files:
        try:
            ET.parse(xml_file)
        except ET.ParseError as exc:
            raise ValueError(f"Invalid XML in {xml_file}: {exc}") from exc
    return xml_files


def sanitize_package_segment(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.]", "_", name)
    return cleaned.strip(".") or "overlay"


def build_manifest_xml(overlay_dir: Path, config: dict, work_dir: Path) -> Path:
    custom_manifest = overlay_dir / "AndroidManifest.xml"
    if custom_manifest.is_file():
        ET.parse(custom_manifest)
        return custom_manifest

    meta: dict = {}
    meta_file = overlay_dir / "overlay.json"
    if meta_file.is_file():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))

    target_package = meta.get("targetPackage") or overlay_dir.name
    prefix = config.get("overlayPackagePrefix", "com.aweyuki.multilang").rstrip(".")
    overlay_package = meta.get("overlayPackage") or f"{prefix}.{sanitize_package_segment(overlay_dir.name)}"
    priority = int(meta.get("priority", config.get("defaultPriority", 100)))
    is_static = bool(meta.get("isStatic", config.get("defaultIsStatic", True)))
    version_code = int(meta.get("versionCode", 1))
    version_name = str(meta.get("versionName", "1.0"))

    overlay_attrs = [
        f"android:targetPackage={quoteattr(target_package)}",
        f"android:isStatic={quoteattr('true' if is_static else 'false')}",
        f"android:priority={quoteattr(str(priority))}",
    ]
    if meta.get("targetName"):
        overlay_attrs.append(f"android:targetName={quoteattr(str(meta['targetName']))}")
    if meta.get("requiredSystemPropertyName"):
        overlay_attrs.append(
            f"android:requiredSystemPropertyName={quoteattr(str(meta['requiredSystemPropertyName']))}"
        )
    if meta.get("requiredSystemPropertyValue"):
        overlay_attrs.append(
            f"android:requiredSystemPropertyValue={quoteattr(str(meta['requiredSystemPropertyValue']))}"
        )

    manifest_content = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android"\n'
        f"    package={quoteattr(overlay_package)}\n"
        f"    android:versionCode={quoteattr(str(version_code))}\n"
        f"    android:versionName={quoteattr(version_name)}>\n"
        "    <overlay\n"
        + "\n".join(f"        {attr}" for attr in overlay_attrs)
        + " />\n"
        '    <application android:hasCode="false" />\n'
        "</manifest>\n"
    )

    generated_path = work_dir / "AndroidManifest.xml"
    generated_path.write_text(manifest_content, encoding="utf-8")
    return generated_path


def run_cmd(cmd: list[str], label: str) -> None:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {proc.returncode}):\n{proc.stdout.strip()}")


def build_single_overlay(
    overlay_dir: Path,
    config: dict,
    build_dir: Path,
    out_dir: Path,
    toolchain: Toolchain | None,
    keystore: KeystoreConfig | None,
    validate_only: bool,
) -> Path | None:
    res_dir = overlay_dir / "res"
    if not res_dir.is_dir():
        raise ValueError(f"Missing res/ directory in {overlay_dir}")

    validate_xml_tree(res_dir)

    work_dir = build_dir / overlay_dir.name
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = build_manifest_xml(overlay_dir, config, work_dir)
    if validate_only:
        return None

    assert toolchain is not None and keystore is not None
    compiled_zip = work_dir / "compiled.zip"
    unaligned_apk = work_dir / "unaligned.apk"
    aligned_apk = work_dir / "aligned.apk"
    out_apk = out_dir / f"{overlay_dir.name}.apk"

    min_sdk = str(config.get("minSdk", 33))
    target_sdk = str(config.get("targetSdk", 35))

    run_cmd(
        [str(toolchain.aapt2), "compile", "--dir", str(res_dir), "-o", str(compiled_zip)],
        f"[{overlay_dir.name}] aapt2 compile",
    )
    run_cmd(
        [
            str(toolchain.aapt2),
            "link",
            "-o",
            str(unaligned_apk),
            "-I",
            str(toolchain.android_jar),
            "--manifest",
            str(manifest_path),
            "--min-sdk-version",
            min_sdk,
            "--target-sdk-version",
            target_sdk,
            "--auto-add-overlay",
            "--no-resource-deduping",
            "--no-resource-removal",
            str(compiled_zip),
        ],
        f"[{overlay_dir.name}] aapt2 link",
    )
    run_cmd(
        [str(toolchain.zipalign), "-f", "-p", "4", str(unaligned_apk), str(aligned_apk)],
        f"[{overlay_dir.name}] zipalign",
    )
    run_cmd(
        [
            str(toolchain.apksigner),
            "sign",
            "--ks",
            str(keystore.path),
            "--ks-pass",
            f"pass:{keystore.store_pass}",
            "--ks-key-alias",
            keystore.alias,
            "--key-pass",
            f"pass:{keystore.key_pass}",
            "--out",
            str(out_apk),
            str(aligned_apk),
        ],
        f"[{overlay_dir.name}] apksigner",
    )
    return out_apk


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build HyperOS CN language RRO overlay APKs using aapt2, zipalign, and apksigner."
    )
    parser.add_argument("--config", type=Path, default=ROOT_DIR / "config.json", help="Path to config.json")
    parser.add_argument("--overlays-dir", type=Path, default=ROOT_DIR / "overlays", help="Overlays source directory")
    parser.add_argument("--build-dir", type=Path, default=ROOT_DIR / "build", help="Intermediate build directory")
    parser.add_argument("--out", type=Path, default=ROOT_DIR / "dist", help="Output directory for signed APKs")
    parser.add_argument("--only", type=str, default="", help="Comma-separated overlay folder names to build")
    parser.add_argument("--compile-sdk", type=int, default=None, help="Override compileSdk version")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="Parallel build workers")
    parser.add_argument("--keystore", type=str, default=None, help="Path to signing keystore")
    parser.add_argument("--keystore-pass", type=str, default=None, help="Keystore password")
    parser.add_argument("--key-alias", type=str, default=None, help="Signing key alias")
    parser.add_argument("--key-pass", type=str, default=None, help="Signing key password")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate XML resources and generate manifests without invoking Android SDK tools",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.config.is_file():
        print(f"Error: config file not found: {args.config}", file=sys.stderr)
        return 1

    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.compile_sdk is not None:
        config["compileSdk"] = args.compile_sdk

    if not args.overlays_dir.is_dir():
        print(f"Error: overlays directory not found: {args.overlays_dir}", file=sys.stderr)
        return 1

    overlay_dirs = sorted(d for d in args.overlays_dir.iterdir() if d.is_dir() and not d.name.startswith("."))
    if args.only.strip():
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        overlay_dirs = [d for d in overlay_dirs if d.name in wanted]
        missing = wanted - {d.name for d in overlay_dirs}
        if missing:
            print(f"Error: requested overlays not found: {', '.join(sorted(missing))}", file=sys.stderr)
            return 1

    if not overlay_dirs:
        print("No overlays found to build.", file=sys.stderr)
        return 1

    toolchain: Toolchain | None = None
    keystore: KeystoreConfig | None = None
    if not args.validate_only:
        toolchain = resolve_toolchain(int(config.get("compileSdk", 35)))
        keystore = ensure_keystore(args.build_dir, args)
        args.out.mkdir(parents=True, exist_ok=True)
    else:
        args.build_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    built: list[Path] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(
                build_single_overlay,
                ov_dir,
                config,
                args.build_dir,
                args.out,
                toolchain,
                keystore,
                args.validate_only,
            ): ov_dir.name
            for ov_dir in overlay_dirs
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                apk_path = fut.result()
                if args.validate_only:
                    print(f"[OK] Validated {name}")
                else:
                    assert apk_path is not None
                    built.append(apk_path)
                    print(f"[OK] Built {apk_path}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"[FAIL] {name}: {exc}")

    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    if args.validate_only:
        print(f"Validated {len(overlay_dirs)} overlay(s) successfully.")
    else:
        print(f"Built {len(built)} overlay APK(s) in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
