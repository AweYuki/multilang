#!/usr/bin/env python3
"""Translate pending XML strings using translate.hzzmonet.io.vn API with batching & parallelism."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
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
TAG_BLOCK_RE = re.compile(r"\[\[(\d+)\]\]\s*(.*?)\s*\[\[/\1\]\]", re.DOTALL)


class TranslateAPIError(RuntimeError):
    """Raised when the translation API fails after retries."""


@dataclass
class TextSlot:
    entry_index: int
    setter: object  # callable(str) -> None
    text: str


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
    timeout: int = 90,
    max_retries: int = 4,
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


def translate_batch_texts(
    texts: list[str],
    api_url: str,
    token: str,
    source: str,
    target: str,
    model: str,
) -> list[str]:
    if len(texts) == 1:
        return [call_translate_api(texts[0], api_url, token, source, target, model)]

    packed = "\n".join(f"[[{idx}]]{t}[[/{idx}]]" for idx, t in enumerate(texts))
    raw_reply = call_translate_api(packed, api_url, token, source, target, model)

    parsed: dict[int, str] = {}
    for match in TAG_BLOCK_RE.finditer(raw_reply):
        idx = int(match.group(1))
        if 0 <= idx < len(texts):
            parsed[idx] = match.group(2)

    results: list[str] = []
    for idx, orig in enumerate(texts):
        if idx in parsed and parsed[idx].strip():
            results.append(parsed[idx])
        else:
            # Fallback for any item whose wrapper tag was dropped by the model
            results.append(call_translate_api(orig, api_url, token, source, target, model))
    return results


def collect_text_slots(entries: list[ET.Element]) -> tuple[list[ET.Element], list[TextSlot]]:
    cloned: list[ET.Element] = [copy.deepcopy(e) for e in entries]
    slots: list[TextSlot] = []

    for entry_idx, elem in enumerate(cloned):
        if elem.tag == "string":
            if elem.text and elem.text.strip():
                slots.append(
                    TextSlot(
                        entry_idx,
                        lambda val, el=elem: setattr(el, "text", val),
                        elem.text,
                    )
                )
            for child in elem:
                if child.tail and child.tail.strip():
                    slots.append(
                        TextSlot(
                            entry_idx,
                            lambda val, ch=child: setattr(ch, "tail", val),
                            child.tail,
                        )
                    )
        elif elem.tag in ("string-array", "array", "plurals"):
            for item in elem:
                if item.tag == "item" and item.text and item.text.strip() and len(item) == 0:
                    slots.append(
                        TextSlot(
                            entry_idx,
                            lambda val, it=item: setattr(it, "text", val),
                            item.text,
                        )
                    )
    return cloned, slots


def build_entry_batches(
    entries: list[ET.Element],
    batch_size: int,
    max_batch_chars: int,
) -> list[tuple[list[int], list[ET.Element], list[TextSlot]]]:
    """Group entries into batches so each batch translates complete XML entries."""
    cloned, slots = collect_text_slots(entries)
    slots_by_entry: dict[int, list[TextSlot]] = {}
    for s in slots:
        slots_by_entry.setdefault(s.entry_index, []).append(s)

    batches: list[tuple[list[int], list[ET.Element], list[TextSlot]]] = []
    cur_indices: list[int] = []
    cur_slots: list[TextSlot] = []
    cur_chars = 0

    for idx in range(len(entries)):
        e_slots = slots_by_entry.get(idx, [])
        # Include ~14 chars overhead per slot for [[i]]...[[/i]]\n wrapper
        e_chars = sum(len(s.text) + 14 for s in e_slots)
        if cur_indices and (
            len(cur_slots) + len(e_slots) > batch_size or cur_chars + e_chars > max_batch_chars
        ):
            batches.append((cur_indices, [cloned[i] for i in cur_indices], cur_slots))
            cur_indices = []
            cur_slots = []
            cur_chars = 0
        cur_indices.append(idx)
        cur_slots.extend(e_slots)
        cur_chars += e_chars

    if cur_indices:
        batches.append((cur_indices, [cloned[i] for i in cur_indices], cur_slots))
    return batches


def translate_package(
    pkg_name: str,
    pending_pkg_dir: Path,
    overlay_pkg_dir: Path,
    api_url: str,
    token: str,
    source: str,
    target: str,
    model: str,
    batch_size: int,
    max_batch_chars: int,
    workers: int,
) -> int:
    pending_values = pending_pkg_dir / "res" / "values"
    overlay_values = overlay_pkg_dir / "res" / f"values-{target}"
    translated_total = 0

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

        batches = build_entry_batches(pending_entries, batch_size, max_batch_chars)
        total_batches = len(batches)
        completed_batches = 0
        remaining_map: dict[int, ET.Element] = {i: e for i, e in enumerate(pending_entries)}
        io_lock = threading.Lock()
        stop_event = threading.Event()

        def process_batch(batch: tuple[list[int], list[ET.Element], list[TextSlot]]) -> int:
            nonlocal completed_batches
            indices, elems, slots = batch
            if stop_event.is_set():
                return 0
            batch_chars = sum(len(s.text) for s in slots)
            if slots:
                translated_texts = translate_batch_texts(
                    [s.text for s in slots],
                    api_url,
                    token,
                    source,
                    target,
                    model,
                )
                for slot, t_val in zip(slots, translated_texts):
                    slot.setter(t_val)
            with io_lock:
                for orig_idx, t_elem in zip(indices, elems):
                    key = (t_elem.tag, t_elem.attrib["name"])
                    translated_entries[key] = t_elem
                    remaining_map.pop(orig_idx, None)
                write_resources_xml(overlay_xml, list(translated_entries.values()))
                write_resources_xml(
                    pending_xml,
                    [remaining_map[i] for i in sorted(remaining_map)],
                )
                completed_batches += 1
                print(
                    f"  [{pkg_name}] {xml_name}: batch {completed_batches}/{total_batches} "
                    f"(+{len(elems)} entries, {batch_chars} chars) | remaining: {len(remaining_map)}",
                    flush=True,
                )
            return len(elems)

        first_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(process_batch, b) for b in batches]
            for fut in as_completed(futures):
                try:
                    translated_total += fut.result()
                except TranslateAPIError as exc:
                    stop_event.set()
                    if first_error is None:
                        first_error = exc

        if first_error is not None:
            raise first_error

    if pending_pkg_dir.exists() and not any((pending_pkg_dir / "res" / "values").glob("*.xml")):
        shutil.rmtree(pending_pkg_dir, ignore_errors=True)

    return translated_total


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
    parser.add_argument("--batch-size", type=int, default=100, help="Max strings per API request (default: 100)")
    parser.add_argument("--max-batch-chars", type=int, default=15000, help="Max characters per batch (default: 15000)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel translation workers (default: 8)")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)

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
    total_pkgs = len(pkg_dirs)
    for idx, pkg_dir in enumerate(pkg_dirs, start=1):
        print(
            f"[TRANSLATE {idx}/{total_pkgs}] {args.version}/{pkg_dir.name} "
            f"(model={args.model}, batch={args.batch_size}, max_chars={args.max_batch_chars}, workers={args.workers}) ...",
            flush=True,
        )
        try:
            count = translate_package(
                pkg_dir.name,
                pkg_dir,
                overlays_ver_dir / pkg_dir.name,
                args.api_url,
                args.token,
                args.source,
                args.target,
                args.model,
                batch_size=max(1, args.batch_size),
                max_batch_chars=max(500, args.max_batch_chars),
                workers=max(1, args.workers),
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
