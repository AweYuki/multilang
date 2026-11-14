#!/usr/bin/env python3
"""Download and unpack a HyperOS/MIUI ROM to extract untranslated XML strings."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from string_state import TARGET_XML_NAMES, compute_pending_for_version

ROOT_DIR = Path(__file__).resolve().parent.parent


def run_cmd(cmd: list[str], label: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {proc.returncode}):\n{proc.stdout.strip()}")
    return proc


def download_rom(url: str, dest: Path) -> Path:
    print(f"[1/5] Downloading ROM from {url} ...")
    subprocess.run(
        ["curl", "-L", "--fail", "--retry", "3", "--retry-delay", "5", "-o", str(dest), url],
        check=True,
    )
    return dest


def extract_payload_from_zip(zip_path: Path, payload_out: Path) -> Path:
    print(f"[2/5] Extracting payload.bin from {zip_path.name} ...")
    with zipfile.ZipFile(zip_path) as zf:
        matches = [n for n in zf.namelist() if n.endswith("payload.bin")]
        if not matches:
            raise RuntimeError(f"payload.bin not found inside {zip_path}")
        with zf.open(matches[0]) as src, payload_out.open("wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
    return payload_out


def unpack_payload(payload_bin: Path, images_dir: Path, partitions: list[str], dumper_bin: str) -> None:
    print(f"[3/5] Unpacking partitions ({', '.join(partitions)}) with {dumper_bin} ...")
    images_dir.mkdir(parents=True, exist_ok=True)
    run_cmd(
        [
            dumper_bin,
            "-p",
            ",".join(partitions),
            "-o",
            str(images_dir),
            str(payload_bin),
        ],
        "payload-dumper-go",
    )


def unpack_tgz_super(tgz_path: Path, images_dir: Path, partitions: list[str], work_dir: Path) -> None:
    print(f"[2/5] Extracting super.img from {tgz_path.name} ...")
    super_dir = work_dir / "tgz_extract"
    super_dir.mkdir(parents=True, exist_ok=True)
    run_cmd(
        ["tar", "-xzf", str(tgz_path), "-C", str(super_dir), "--wildcards", "*/images/super.img", "images/super.img"],
        "tar extract super.img",
        check=False,
    )
    super_imgs = list(super_dir.rglob("super.img"))
    if not super_imgs:
        raise RuntimeError("super.img not found inside .tgz archive")

    raw_super = work_dir / "super.raw.img"
    simg2img = shutil.which("simg2img")
    if simg2img:
        proc = run_cmd([simg2img, str(super_imgs[0]), str(raw_super)], "simg2img", check=False)
        if proc.returncode != 0:
            shutil.move(str(super_imgs[0]), str(raw_super))
    else:
        shutil.move(str(super_imgs[0]), str(raw_super))
    shutil.rmtree(super_dir, ignore_errors=True)

    lpunpack = shutil.which("lpunpack")
    if not lpunpack:
        raise RuntimeError("lpunpack not found in PATH; required for fastboot .tgz super.img")

    print(f"[3/5] Unpacking super.raw.img with lpunpack ...")
    images_dir.mkdir(parents=True, exist_ok=True)
    run_cmd([lpunpack, str(raw_super), str(images_dir)], "lpunpack")
    raw_super.unlink(missing_ok=True)

    # Keep only requested partitions (handling _a / _b suffixes)
    wanted = set(partitions)
    for img in list(images_dir.glob("*.img")):
        stem = img.stem.removesuffix("_a").removesuffix("_b")
        if stem not in wanted:
            img.unlink(missing_ok=True)


def extract_apks_from_image(img_path: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted = False

    extract_erofs = shutil.which("extract.erofs")
    if extract_erofs:
        proc = run_cmd([extract_erofs, "-i", str(img_path), "-o", str(out_dir), "-x"], f"extract.erofs {img_path.name}", check=False)
        if proc.returncode == 0 and any(out_dir.rglob("*.apk")):
            extracted = True

    if not extracted:
        seven_z = shutil.which("7z") or shutil.which("7zz")
        if seven_z:
            proc = run_cmd([seven_z, "x", "-y", f"-o{out_dir}", str(img_path)], f"7z {img_path.name}", check=False)
            if proc.returncode == 0 and any(out_dir.rglob("*.apk")):
                extracted = True

    if not extracted and os.geteuid() == 0 or shutil.which("sudo"):
        mnt = out_dir.parent / f"mnt_{img_path.stem}"
        mnt.mkdir(parents=True, exist_ok=True)
        mount_cmd = ["sudo", "mount", "-o", "ro,loop", str(img_path), str(mnt)]
        proc = run_cmd(mount_cmd, f"mount {img_path.name}", check=False)
        if proc.returncode == 0:
            try:
                for apk in mnt.rglob("*.apk"):
                    rel = apk.relative_to(mnt)
                    dest = out_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(apk, dest)
                extracted = True
            finally:
                run_cmd(["sudo", "umount", str(mnt)], f"umount {img_path.name}", check=False)
                mnt.rmdir()

    if not extracted:
        print(f"[WARN] Could not extract APKs from {img_path.name}", file=sys.stderr)
        return []

    apks: list[Path] = []
    for apk in sorted(out_dir.rglob("*.apk")):
        parts = apk.parts
        if any(p in ("app", "priv-app", "framework") for p in parts):
            apks.append(apk)
    return apks


def decode_and_collect_apk(
    apk_path: Path,
    apktool_cmd: list[str],
    decode_root: Path,
    out_version_dir: Path,
) -> tuple[str, str] | None:
    tmp_out = decode_root / f"{apk_path.stem}_{abs(hash(str(apk_path)))}"
    if tmp_out.exists():
        shutil.rmtree(tmp_out)

    proc = run_cmd(
        [*apktool_cmd, "d", "-s", "-f", "-o", str(tmp_out), str(apk_path)],
        f"apktool {apk_path.name}",
        check=False,
    )
    if proc.returncode != 0:
        shutil.rmtree(tmp_out, ignore_errors=True)
        return None

    try:
        manifest = tmp_out / "AndroidManifest.xml"
        if not manifest.is_file():
            return None
        root = ET.parse(manifest).getroot()
        pkg = root.attrib.get("package", "").strip()
        if not pkg:
            return None

        src_values = tmp_out / "res" / "values"
        if not src_values.is_dir():
            return None

        copied: list[str] = []
        dst_values = out_version_dir / pkg / "res" / "values"
        for xml_name in TARGET_XML_NAMES:
            src_xml = src_values / xml_name
            if src_xml.is_file():
                dst_values.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_xml, dst_values / xml_name)
                copied.append(xml_name)

        if not copied:
            return None

        meta_file = out_version_dir / pkg / "source.json"
        meta_file.write_text(
            json.dumps({"package": pkg, "apkName": apk_path.name, "files": copied}, indent=2) + "\n",
            encoding="utf-8",
        )
        return pkg, apk_path.name
    finally:
        shutil.rmtree(tmp_out, ignore_errors=True)


def resolve_apktool_cmd(apktool_arg: str | None) -> list[str]:
    if apktool_arg:
        p = Path(apktool_arg)
        if p.suffix == ".jar":
            return ["java", "-jar", str(p)]
        return [str(p)]
    found = shutil.which("apktool")
    if found:
        return [found]
    raise RuntimeError("apktool not found in PATH; pass --apktool /path/to/apktool(.jar)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download/unpack a HyperOS ROM and extract untranslated strings.")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--rom-url", help="Direct URL to ROM .zip or .tgz")
    group.add_argument("--rom-file", type=Path, help="Local path to ROM .zip or .tgz")
    parser.add_argument("--version", default="os4-a17", help="Version folder under untranslated/ (default: os4-a17)")
    parser.add_argument("--out-dir", type=Path, default=ROOT_DIR / "untranslated", help="Untranslated output root directory")
    parser.add_argument("--overlays-dir", type=Path, default=ROOT_DIR / "overlays", help="Translated overlays root directory")
    parser.add_argument("--pending-dir", type=Path, default=ROOT_DIR / "pending", help="Pending (to-translate) output root directory")
    parser.add_argument("--target-lang", default="vi", help="Target language code in overlays/ (default: vi)")
    parser.add_argument("--partitions", default="system,system_ext,product,mi_ext", help="Comma-separated partitions")
    parser.add_argument("--payload-dumper", default="payload-dumper-go", help="Path to payload-dumper-go binary")
    parser.add_argument("--apktool", default=None, help="Path to apktool binary or apktool.jar")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="Parallel apktool workers")
    parser.add_argument("--clean", action="store_true", help="Clean untranslated/<version> before extracting")
    parser.add_argument(
        "--sync-pending-only",
        action="store_true",
        help="Only recompute pending/<version> from existing untranslated/<version> and overlays/<version>",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_version_dir = args.out_dir / args.version
    overlays_version_dir = args.overlays_dir / args.version
    pending_version_dir = args.pending_dir / args.version

    if args.sync_pending_only:
        if pending_version_dir.exists():
            shutil.rmtree(pending_version_dir)
        pkg_count, entry_count = compute_pending_for_version(
            out_version_dir,
            overlays_version_dir,
            pending_version_dir,
            prev_untranslated_dir=None,
            target_lang=args.target_lang,
        )
        print(f"Computed pending for {args.version}: {pkg_count} package(s), {entry_count} entry(s) in {pending_version_dir}")
        return 0

    if not args.rom_url and not args.rom_file:
        print("Error: one of --rom-url, --rom-file, or --sync-pending-only is required.", file=sys.stderr)
        return 1

    apktool_cmd = resolve_apktool_cmd(args.apktool)
    partitions = [p.strip() for p in args.partitions.split(",") if p.strip()]

    with tempfile.TemporaryDirectory(prefix="hyperos_rom_") as tmp:
        work_dir = Path(tmp)
        prev_snapshot: Path | None = None
        if out_version_dir.is_dir():
            prev_snapshot = work_dir / "prev_untranslated"
            shutil.copytree(out_version_dir, prev_snapshot)

        if args.clean and out_version_dir.exists():
            shutil.rmtree(out_version_dir)
        out_version_dir.mkdir(parents=True, exist_ok=True)

        if args.rom_url:
            ext = ".tgz" if ".tgz" in args.rom_url.lower() or ".tar.gz" in args.rom_url.lower() else ".zip"
            rom_path = download_rom(args.rom_url, work_dir / f"rom{ext}")
        else:
            assert args.rom_file is not None
            if not args.rom_file.is_file():
                raise RuntimeError(f"ROM file not found: {args.rom_file}")
            rom_path = args.rom_file

        images_dir = work_dir / "images"
        if rom_path.suffix.lower() == ".zip":
            payload_bin = extract_payload_from_zip(rom_path, work_dir / "payload.bin")
            if args.rom_url:
                rom_path.unlink(missing_ok=True)
            unpack_payload(payload_bin, images_dir, partitions, args.payload_dumper)
            payload_bin.unlink(missing_ok=True)
        else:
            unpack_tgz_super(rom_path, images_dir, partitions, work_dir)
            if args.rom_url:
                rom_path.unlink(missing_ok=True)

        print("[4/5] Extracting APKs from partition images ...")
        apks_dir = work_dir / "apks"
        all_apks: list[Path] = []
        for img in sorted(images_dir.glob("*.img")):
            part_apks = extract_apks_from_image(img, apks_dir / img.stem)
            print(f"  - {img.name}: {len(part_apks)} APK(s)")
            all_apks.extend(part_apks)
            img.unlink(missing_ok=True)

        if not all_apks:
            raise RuntimeError("No APKs found in extracted partition images.")

        print(f"[5/5] Decoding {len(all_apks)} APK(s) with apktool ...")
        decode_root = work_dir / "decoded"
        decode_root.mkdir(parents=True, exist_ok=True)
        extracted_pkgs: list[tuple[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            futures = [
                pool.submit(decode_and_collect_apk, apk, apktool_cmd, decode_root, out_version_dir)
                for apk in all_apks
            ]
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    extracted_pkgs.append(res)

        if pending_version_dir.exists():
            shutil.rmtree(pending_version_dir)
        pkg_count, entry_count = compute_pending_for_version(
            out_version_dir,
            overlays_version_dir,
            pending_version_dir,
            prev_untranslated_dir=prev_snapshot,
            target_lang=args.target_lang,
        )

    print(
        f"Done! Extracted {len(extracted_pkgs)} package(s) into {out_version_dir}; "
        f"pending delta: {pkg_count} package(s), {entry_count} entry(s) in {pending_version_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
