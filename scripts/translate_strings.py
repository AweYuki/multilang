#!/usr/bin/env python3
"""Translate pending XML strings using translate.hzzmonet.io.vn API with checkpointing."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from string_state import (
    TARGET_XML_NAMES,
    ensure_overlay_metadata,
    load_xml_entries,
    write_resources_xml,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_API_URL = "https://translate.hzzmonet.io.vn/api/v1/translate"
DEFAULT_MODEL = "cx/gpt-6.1-sol"


class TranslateAPIError(RuntimeError):
    """Raised when the translation API fails after retries."""


def extract_translated_text(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("translatedText", "translation", "translated", "text", "result", "output"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
        data = payload.get("data")
        if isinstance(data, dict):
            return extract_translated_text(data)
    raise TranslateAPIError(f"Unexpected API response format: {payload!r}")


def call_translate_api(
    text: str,
    api_url: str,
    token: str,
    source: str,
    target: str,
    model: str,
    timeout: int = 60,
    max_retries: int = 2,
) -> str:
    if not text.strip():
        return text

    body = json.dumps(
        {
            "text": text,
            "source": source,
            "target": target,
            "model": model,
        }
    ).encode("utf-8")

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            api_url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "multilang-overlay-translator/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return extract_translated_text(json.loads(raw))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            last_err = TranslateAPIError(f"HTTP {exc.code}: {err_body}")
            if exc.code in (401, 403, 400):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, TranslateAPIError) as exc:
            last_err = exc

        if attempt < max_retries:
            time.sleep(2 ** (attempt + 1))

    raise TranslateAPIError(str(last_err))


def translate_element(
    elem: ET.Element,
    api_url: str,
    token: str,
    source: str,
    target: str,
    model: str,
) -> ET.Element:
    translated = copy.deepcopy(elem)

    if translated.tag == "string":
        # Preserve inline child tags (e.g. <xliff:g>) if present; translate text segments
        if len(translated) == 0:
            if translated.text:
                translated.text = call_translate_api(translated.text, api_url, token, source, target, model)
        else:
            if translated.text and translated.text.strip():
                translated.text = call_translate_api(translated.text, api_url, token, source, target, model)
            for child in translated:
                if child.tail and child.tail.strip():
                    child.tail = call_translate_api(child.tail, api_url, token, source, target, model)
        return translated

    if translated.tag in ("string-array", "array", "plurals"):
        for item in translated:
            if item.tag == "item" and item.text and item.text.strip() and len(item) == 0:
                item.text = call_translate_api(item.text, api_url, token, source, target, model)
        return translated

    return translated


def flush_state(
    overlay_xml: Path,
    translated_entries: dict[tuple[str, str], ET.Element],
    pending_xml: Path,
    remaining_pending: list[ET.Element],
) -> None:
    write_resources_xml(overlay_xml, list(translated_entries.values()))
    write_resources_xml(pending_xml, remaining_pending)


def translate_package(
    pkg_name: str,
    pending_pkg_dir: Path,
    overlay_pkg_dir: Path,
    api_url: str,
    token: str,
    source: str,
    target: str,
    model: str,
    save_every: int = 20,
) -> tuple[int, bool]:
    pending_values = pending_pkg_dir / "res" / "values"
    overlay_values = overlay_pkg_dir / "res" / f"values-{target}"
    translated_count = 0

    for xml_name in TARGET_XML_NAMES:
        pending_xml = pending_values / xml_name
        if not pending_xml.is_file():
            continue

        pending_entries = list(load_xml_entries(pending_xml).values())
        if not pending_entries:
            pending_xml.unlink(missing_ok=True)
            continue

        overlay_xml = overlay_values / xml_name
        translated_entries = load_xml_entries(overlay_xml)
        ensure_overlay_metadata(overlay_pkg_dir, pkg_name)

        idx = 0
        try:
            while idx < len(pending_entries):
                elem = pending_entries[idx]
                key = (elem.tag, elem.attrib["name"])
                t_elem = translate_element(elem, api_url, token, source, target, model)
                translated_entries[key] = t_elem
                translated_count += 1
                idx += 1

                if translated_count % save_every == 0:
                    flush_state(overlay_xml, translated_entries, pending_xml, pending_entries[idx:])
        except TranslateAPIError:
            flush_state(overlay_xml, translated_entries, pending_xml, pending_entries[idx:])
            raise

        flush_state(overlay_xml, translated_entries, pending_xml, [])

    # Clean up empty pending package directory
    if pending_pkg_dir.exists() and not any((pending_pkg_dir / "res" / "values").glob("*.xml")):
        shutil.rmtree(pending_pkg_dir, ignore_errors=True)

    return translated_count, True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate pending XML strings and update overlays/.")
    parser.add_argument("--version", default="os4-a17", help="Version folder under pending/ and overlays/")
    parser.add_argument("--pending-dir", type=Path, default=ROOT_DIR / "pending", help="Pending root directory")
    parser.add_argument("--overlays-dir", type=Path, default=ROOT_DIR / "overlays", help="Translated overlays root directory")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Translate API endpoint URL")
    parser.add_argument("--token", default=os.environ.get("TRANSLATE_API_TOKEN", ""), help="Bearer token")
    parser.add_argument("--source", default="auto", help="Source language (default: auto)")
    parser.add_argument("--target", default="vi", help="Target language (default: vi)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name (default: cx/gpt-6.1-sol)")
    parser.add_argument("--packages", default="", help="Comma-separated package names to translate")
    parser.add_argument("--save-every", type=int, default=20, help="Checkpoint interval in entries")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.token:
        print("Error: missing API token. Set TRANSLATE_API_TOKEN or pass --token.", file=sys.stderr)
        return 1

    pending_ver_dir = args.pending_dir / args.version
    overlays_ver_dir = args.overlays_dir / args.version
    if not pending_ver_dir.is_dir():
        print(f"No pending directory found at {pending_ver_dir}; nothing to translate.")
        return 0

    pkg_dirs = sorted(d for d in pending_ver_dir.iterdir() if d.is_dir())
    if args.packages.strip():
        wanted = {p.strip() for p in args.packages.split(",") if p.strip()}
        pkg_dirs = [d for d in pkg_dirs if d.name in wanted]

    if not pkg_dirs:
        print("No pending packages to translate.")
        return 0

    total_translated = 0
    for pkg_dir in pkg_dirs:
        print(f"[TRANSLATE] {args.version}/{pkg_dir.name} (model={args.model}) ...")
        try:
            count, _ = translate_package(
                pkg_dir.name,
                pkg_dir,
                overlays_ver_dir / pkg_dir.name,
                args.api_url,
                args.token,
                args.source,
                args.target,
                args.model,
                save_every=max(1, args.save_every),
            )
            total_translated += count
            print(f"  [OK] {pkg_dir.name}: translated {count} entry(s)")
        except TranslateAPIError as exc:
            print(f"  [ERROR] API failed on {pkg_dir.name}: {exc}", file=sys.stderr)
            print(f"Saved partial progress ({total_translated} entry(s) translated so far).", file=sys.stderr)
            return 1

    print(f"Finished! Translated {total_translated} entry(s) into {overlays_ver_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
