#!/usr/bin/env python3
"""Translate pending XML strings via /api/v1/translate-file (with batch fallback & parallelism)."""

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
import uuid
import xml.etree.ElementTree as ET

from string_state import (
    TARGET_XML_NAMES,
    ensure_overlay_metadata,
    load_xml_entries,
    write_resources_xml,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_API_BASE = "https://translate.hzzmonet.io.vn/api/v1"
DEFAULT_MODEL = "cx/gpt-6-luna"
MAX_FILE_BYTES = 9 * 1024 * 1024  # 9 MB safety margin under 10 MB limit
TAG_BLOCK_RE = re.compile(r"\[\[(\d+)\]\]\s*(.*?)\s*\[\[/\1\]\]", re.DOTALL)
CODE_FENCE_RE = re.compile(r"^```(?:xml)?\s*\n(.*?)\n```\s*$", re.DOTALL)


class TranslateAPIError(RuntimeError):
    """Raised when the translation API fails after retries."""


@dataclass
class TextSlot:
    entry_index: int
    setter: object
    text: str


def extract_translated_text(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("content", "xml", "translatedText", "translation", "translated", "text", "result", "output"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                return val
        data = payload.get("data")
        if isinstance(data, dict):
            return extract_translated_text(data)
    raise TranslateAPIError(f"Unexpected API response format: {payload!r}")


def strip_code_fences(text: str) -> str:
    stripped = text.strip()
    m = CODE_FENCE_RE.match(stripped)
    return m.group(1).strip() if m else stripped


def call_translate_file_api(
    filename: str,
    xml_bytes: bytes,
    api_base: str,
    token: str,
    source: str,
    target: str,
    model: str,
    timeout: int = 180,
    max_retries: int = 3,
) -> str:
    url = f"{api_base.rstrip('/')}/translate-file"
    boundary = f"----MultilangBoundary{uuid.uuid4().hex}"

    parts: list[bytes] = []
    for field_name, field_val in (("source", source), ("target", target), ("model", model)):
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'
                f"{field_val}\r\n"
            ).encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/xml\r\n\r\n"
        ).encode("utf-8")
        + xml_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "multilang-overlay-translator/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                cleaned = strip_code_fences(raw)
                if cleaned.startswith("<"):
                    return cleaned
                return strip_code_fences(extract_translated_text(json.loads(raw)))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            last_err = TranslateAPIError(f"HTTP {exc.code}: {err_body}")
            if exc.code in (400, 401, 403, 404, 413, 415, 422):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, TranslateAPIError) as exc:
            last_err = exc

        if attempt < max_retries:
            time.sleep(2 ** (attempt + 1))

    raise TranslateAPIError(str(last_err))


def call_translate_api(
    text: str,
    api_base: str,
    token: str,
    source: str,
    target: str,
    model: str,
    timeout: int = 90,
    max_retries: int = 3,
) -> str:
    if not text.strip():
        return text

    url = f"{api_base.rstrip('/')}/translate"
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
            url,
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
            if exc.code in (400, 401, 403):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, TranslateAPIError) as exc:
            last_err = exc

        if attempt < max_retries:
            time.sleep(2 ** (attempt + 1))

    raise TranslateAPIError(str(last_err))


def translate_batch_texts(
    texts: list[str],
    api_base: str,
    token: str,
    source: str,
    target: str,
    model: str,
) -> list[str]:
    if len(texts) == 1:
        return [call_translate_api(texts[0], api_base, token, source, target, model)]

    packed = "\n".join(f"[[{idx}]]{t}[[/{idx}]]" for idx, t in enumerate(texts))
    raw_reply = call_translate_api(packed, api_base, token, source, target, model)

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
            results.append(call_translate_api(orig, api_base, token, source, target, model))
    return results


def collect_text_slots(entries: list[ET.Element]) -> tuple[list[ET.Element], list[TextSlot]]:
    cloned: list[ET.Element] = [copy.deepcopy(e) for e in entries]
    slots: list[TextSlot] = []

    for entry_idx, elem in enumerate(cloned):
        if elem.tag == "string":
            if elem.text and elem.text.strip():
                slots.append(
                    TextSlot(entry_idx, lambda val, el=elem: setattr(el, "text", val), elem.text)
                )
            for child in elem:
                if child.tail and child.tail.strip():
                    slots.append(
                        TextSlot(entry_idx, lambda val, ch=child: setattr(ch, "tail", val), child.tail)
                    )
        elif elem.tag in ("string-array", "array", "plurals"):
            for item in elem:
                if item.tag == "item" and item.text and item.text.strip() and len(item) == 0:
                    slots.append(
                        TextSlot(entry_idx, lambda val, it=item: setattr(it, "text", val), item.text)
                    )
    return cloned, slots


def build_entry_batches(
    entries: list[ET.Element],
    batch_size: int,
    max_batch_chars: int,
) -> list[tuple[list[int], list[ET.Element], list[TextSlot]]]:
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


def serialize_entries_xml(entries: list[ET.Element]) -> bytes:
    root = ET.Element("resources")
    for elem in entries:
        root.append(elem)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="    ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def parse_translated_xml_entries(xml_text: str) -> dict[tuple[str, str], ET.Element]:
    root = ET.fromstring(xml_text)
    parsed: dict[tuple[str, str], ET.Element] = {}
    for child in root:
        name = child.attrib.get("name")
        if name:
            parsed[(child.tag, name)] = child
    return parsed


def split_entries_by_bytes(entries: list[ET.Element], max_bytes: int) -> list[list[ET.Element]]:
    chunks: list[list[ET.Element]] = []
    current: list[ET.Element] = []
    cur_size = 128

    for elem in entries:
        elem_size = len(ET.tostring(elem, encoding="utf-8")) + 8
        if current and cur_size + elem_size > max_bytes:
            chunks.append(current)
            current = []
            cur_size = 128
        current.append(elem)
        cur_size += elem_size

    if current:
        chunks.append(current)
    return chunks


def translate_xml_via_batches(
    pkg_name: str,
    xml_name: str,
    pending_xml: Path,
    overlay_xml: Path,
    pending_entries: list[ET.Element],
    translated_entries: dict[tuple[str, str], ET.Element],
    api_base: str,
    token: str,
    source: str,
    target: str,
    model: str,
    batch_size: int,
    max_batch_chars: int,
) -> int:
    batches = build_entry_batches(pending_entries, batch_size, max_batch_chars)
    remaining_map: dict[int, ET.Element] = {i: e for i, e in enumerate(pending_entries)}
    translated_count = 0

    for b_idx, (indices, elems, slots) in enumerate(batches, start=1):
        if slots:
            translated_texts = translate_batch_texts(
                [s.text for s in slots],
                api_base,
                token,
                source,
                target,
                model,
            )
            for slot, t_val in zip(slots, translated_texts):
                slot.setter(t_val)
        for orig_idx, t_elem in zip(indices, elems):
            key = (t_elem.tag, t_elem.attrib["name"])
            translated_entries[key] = t_elem
            remaining_map.pop(orig_idx, None)
        write_resources_xml(overlay_xml, list(translated_entries.values()))
        write_resources_xml(pending_xml, [remaining_map[i] for i in sorted(remaining_map)])
        translated_count += len(elems)
        print(
            f"  [{pkg_name}] {xml_name} (fallback batch {b_idx}/{len(batches)}): "
            f"+{len(elems)} entries | remaining: {len(remaining_map)}",
            flush=True,
        )
    return translated_count


def translate_package(
    pkg_name: str,
    pending_pkg_dir: Path,
    overlay_pkg_dir: Path,
    api_base: str,
    token: str,
    source: str,
    target: str,
    model: str,
    batch_size: int,
    max_batch_chars: int,
    use_file_api: bool,
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

        if not use_file_api:
            translated_total += translate_xml_via_batches(
                pkg_name, xml_name, pending_xml, overlay_xml, pending_entries,
                translated_entries, api_base, token, source, target, model,
                batch_size, max_batch_chars,
            )
            continue

        chunks = split_entries_by_bytes(pending_entries, MAX_FILE_BYTES)
        remaining_map: dict[tuple[str, str], ET.Element] = {
            (e.tag, e.attrib["name"]): e for e in pending_entries
        }

        for c_idx, chunk in enumerate(chunks, start=1):
            xml_bytes = serialize_entries_xml(chunk)
            size_kb = len(xml_bytes) / 1024.0
            try:
                resp_xml = call_translate_file_api(
                    xml_name, xml_bytes, api_base, token, source, target, model
                )
                parsed_map = parse_translated_xml_entries(resp_xml)
                if not parsed_map:
                    raise TranslateAPIError("Empty XML returned by /translate-file")

                matched = 0
                for key, t_elem in parsed_map.items():
                    if key in remaining_map:
                        translated_entries[key] = t_elem
                        remaining_map.pop(key, None)
                        matched += 1

                write_resources_xml(overlay_xml, list(translated_entries.values()))
                write_resources_xml(pending_xml, list(remaining_map.values()))
                translated_total += matched
                print(
                    f"  [{pkg_name}] {xml_name}: file chunk {c_idx}/{len(chunks)} "
                    f"({size_kb:.1f} KB) -> +{matched}/{len(chunk)} entries | remaining: {len(remaining_map)}",
                    flush=True,
                )
            except (ET.ParseError, TranslateAPIError) as exc:
                # Only fall back to /translate batching on format/404/415/422 errors; propagate auth/5xx
                msg = str(exc)
                if any(code in msg for code in ("HTTP 401", "HTTP 403", "HTTP 429", "HTTP 5")):
                    raise
                print(
                    f"  [{pkg_name}] {xml_name}: /translate-file fallback ({exc}), using batch /translate ...",
                    flush=True,
                )
                leftover = list(remaining_map.values())
                translated_total += translate_xml_via_batches(
                    pkg_name, xml_name, pending_xml, overlay_xml, leftover,
                    translated_entries, api_base, token, source, target, model,
                    batch_size, max_batch_chars,
                )
                break

    if pending_pkg_dir.exists() and not any((pending_pkg_dir / "res" / "values").glob("*.xml")):
        shutil.rmtree(pending_pkg_dir, ignore_errors=True)

    return translated_total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate pending XML strings and update overlays/.")
    parser.add_argument("--version", default="os4-a17", help="Version folder under pending/ and overlays/")
    parser.add_argument("--pending-dir", type=Path, default=ROOT_DIR / "pending", help="Pending root directory")
    parser.add_argument("--overlays-dir", type=Path, default=ROOT_DIR / "overlays", help="Translated overlays root directory")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="Translate API base URL (default: .../api/v1)")
    parser.add_argument("--token", default=os.environ.get("TRANSLATE_API_TOKEN", ""), help="Bearer token")
    parser.add_argument("--source", default="auto", help="Source language (default: auto)")
    parser.add_argument("--target", default="vi", help="Target language (default: vi)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model name (default: cx/gpt-6.1-sol)")
    parser.add_argument("--packages", default="", help="Comma-separated package names to translate")
    parser.add_argument("--batch-size", type=int, default=100, help="Fallback batch size (default: 100)")
    parser.add_argument("--max-batch-chars", type=int, default=15000, help="Fallback max chars (default: 15000)")
    parser.add_argument("--workers", type=int, default=6, help="Parallel package workers (default: 6)")
    parser.add_argument("--no-file-api", action="store_true", help="Disable /translate-file and use /translate batches only")
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

    total_pkgs = len(pkg_dirs)
    print(
        f"Starting translation: {total_pkgs} package(s) in {args.version} "
        f"(model={args.model}, file_api={not args.no_file_api}, workers={args.workers})",
        flush=True,
    )

    total_translated = 0
    counter_lock = threading.Lock()
    stop_event = threading.Event()
    first_error: Exception | None = None

    def run_pkg(idx: int, pkg_dir: Path) -> None:
        nonlocal total_translated, first_error
        if stop_event.is_set():
            return
        print(f"[{idx}/{total_pkgs}] Translating {pkg_dir.name} ...", flush=True)
        try:
            count = translate_package(
                pkg_dir.name,
                pkg_dir,
                overlays_ver_dir / pkg_dir.name,
                args.api_base,
                args.token,
                args.source,
                args.target,
                args.model,
                batch_size=max(1, args.batch_size),
                max_batch_chars=max(500, args.max_batch_chars),
                use_file_api=not args.no_file_api,
            )
            with counter_lock:
                total_translated += count
            print(f"[DONE {idx}/{total_pkgs}] {pkg_dir.name}: +{count} entries", flush=True)
        except TranslateAPIError as exc:
            stop_event.set()
            with counter_lock:
                if first_error is None:
                    first_error = exc
            print(f"[ERROR] {pkg_dir.name}: {exc}", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(run_pkg, idx, pkg_dir) for idx, pkg_dir in enumerate(pkg_dirs, start=1)]
        for fut in as_completed(futures):
            fut.result()

    if first_error is not None:
        print(f"Stopped early due to API error; saved {total_translated} translated entry(s).", file=sys.stderr)
        return 1

    print(f"Finished! Translated {total_translated} entry(s) into {overlays_ver_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
