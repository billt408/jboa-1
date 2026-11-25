#!/usr/bin/env python3
"""Restore redacted (***) secrets and API keys in a combined parms.ini file
from an existing live environment parms.ini file.

Usage:
    python3 restore_parms_secrets.py <combined_file> <old_parms_file> [--output <path>] [--dry-run] [--no-backup]

Examples:
    # Update combined file in place (creates <combined_file>.bak first)
    python3 restore_parms_secrets.py lib/parms.ini.combined.jboa /home/billt/jboa-1/src/lib/parms.ini

    # Write to a separate output path without modifying either input file
    python3 restore_parms_secrets.py lib/parms.ini.combined.jboa /home/billt/jboa-1/src/lib/parms.ini -o lib/parms.ini.ready

    # Preview which [section] keys will be restored (never prints secret values)
    python3 restore_parms_secrets.py lib/parms.ini.combined.jboa /home/billt/jboa-1/src/lib/parms.ini --dry-run
"""

import argparse
import configparser
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
REDACTED_SENTINEL = "***"
JSON_INI_SECTIONS = frozenset(
    {"symbols", "alpaca_api", "tradier", "twelvedata_api", "logging", "llm_configuration"}
)


def _split_value_and_comment(raw_rhs: str):
    """Split an INI right-hand side into (value_str, inline_comment_str),
    ignoring ';' or '#' characters inside single or double quotes."""
    in_quote = None
    escaped = False
    for idx, ch in enumerate(raw_rhs):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_quote == '"':
            escaped = True
            continue
        if ch in {'"', "'"}:
            if in_quote is None:
                in_quote = ch
            elif in_quote == ch:
                in_quote = None
            continue
        if in_quote is None and ch in {";", "#"}:
            return raw_rhs[:idx].strip(), raw_rhs[idx:].strip()
    return raw_rhs.strip(), ""


def _unquote(val: str) -> str:
    s = val.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {'"', "'"}:
        return s[1:-1]
    return s


def _is_redacted_scalar(val: str) -> bool:
    return _unquote(val).strip() == REDACTED_SENTINEL


def _contains_redacted_placeholder(val: str) -> bool:
    """Return True if a scalar or JSON-structured INI value contains '***'."""
    if _is_redacted_scalar(val):
        return True
    s = val.strip()
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return REDACTED_SENTINEL in s
        return _json_has_redacted(parsed)
    return False


def _json_has_redacted(obj) -> bool:
    if isinstance(obj, str):
        return obj.strip() == REDACTED_SENTINEL
    if isinstance(obj, dict):
        return any(_json_has_redacted(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_json_has_redacted(v) for v in obj)
    return False


def _merge_json_secrets(combined_obj, old_obj):
    """Recursively replace '***' leaf strings in combined_obj with values from old_obj.
    Returns (merged_obj, restored_count, unresolved_count)."""
    if isinstance(combined_obj, str):
        if combined_obj.strip() == REDACTED_SENTINEL:
            if isinstance(old_obj, str) and old_obj.strip() not in {"", REDACTED_SENTINEL}:
                return old_obj, 1, 0
            return combined_obj, 0, 1
        return combined_obj, 0, 0

    if isinstance(combined_obj, dict):
        out = {}
        restored = 0
        unresolved = 0
        old_dict = old_obj if isinstance(old_obj, dict) else {}
        for k, v in combined_obj.items():
            mv, r, u = _merge_json_secrets(v, old_dict.get(k))
            out[k] = mv
            restored += r
            unresolved += u
        return out, restored, unresolved

    if isinstance(combined_obj, list):
        out = []
        restored = 0
        unresolved = 0
        old_list = old_obj if isinstance(old_obj, list) else []
        for idx, v in enumerate(combined_obj):
            ov = old_list[idx] if idx < len(old_list) else None
            mv, r, u = _merge_json_secrets(v, ov)
            out.append(mv)
            restored += r
            unresolved += u
        return out, restored, unresolved

    return combined_obj, 0, 0


def parse_ini_values(path: Path):
    """Parse an INI file into {(section_lower, key_lower): (raw_value, section_orig, key_orig)}."""
    values = {}
    cur_sec = None
    with path.open("r", encoding="utf-8-sig") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")
            stripped = line.strip()
            if not stripped or stripped.startswith((";", "#")):
                continue
            m = SECTION_RE.match(stripped)
            if m:
                cur_sec = m.group(1).strip()
                continue
            if "=" in line and cur_sec is not None:
                key_part, rhs = line.split("=", 1)
                key = key_part.strip()
                val, _ = _split_value_and_comment(rhs)
                values[(cur_sec.lower(), key.lower())] = (val, cur_sec, key)
    return values


def _format_scalar_like_combined(combined_val: str, old_val: str) -> str:
    """Preserve quoting style of combined_val when substituting old_val."""
    c_strip = combined_val.strip()
    o_strip = old_val.strip()
    if not o_strip:
        return combined_val
    # If combined was double-quoted (e.g. "***") and old is unquoted, wrap as JSON string
    if len(c_strip) >= 2 and c_strip[0] == '"' and c_strip[-1] == '"':
        if len(o_strip) >= 2 and o_strip[0] == '"' and o_strip[-1] == '"':
            return o_strip
        return json.dumps(_unquote(o_strip))
    if len(c_strip) >= 2 and c_strip[0] == "'" and c_strip[-1] == "'":
        if len(o_strip) >= 2 and o_strip[0] == "'" and o_strip[-1] == "'":
            return o_strip
        return f"'{_unquote(o_strip)}'"
    return o_strip


def validate_ini_output(content: str, label: str):
    """Validate that merged INI text parses cleanly with ConfigParser and JsonIniParser rules."""
    cp = configparser.ConfigParser(inline_comment_prefixes=(";",), interpolation=None)
    cp.optionxform = str
    cp.read_string(content)
    for sec in JSON_INI_SECTIONS:
        if not cp.has_section(sec):
            continue
        for k, v in cp.items(sec):
            if sec == "alpaca_api" and k.lower() == "feed":
                continue
            try:
                json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Validation failed in {label} [{sec}] {k}: invalid JSON literal ({exc.msg})"
                ) from exc


def restore_secrets(
    combined_path: Path,
    old_parms_path: Path,
    output_path: Path = None,
    dry_run: bool = False,
    make_backup: bool = True,
):
    if not combined_path.is_file():
        raise FileNotFoundError(f"Combined file not found: {combined_path}")
    if not old_parms_path.is_file():
        raise FileNotFoundError(f"Old parms.ini file not found: {old_parms_path}")

    old_values = parse_ini_values(old_parms_path)
    raw_bytes = combined_path.read_bytes()
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    raw_text = raw_bytes.decode("utf-8-sig")
    default_nl = "\r\n" if "\r\n" in raw_text else "\n"

    out_lines = []
    cur_sec = None
    restored_keys = []
    unresolved_keys = []

    for raw_line in raw_text.splitlines(keepends=True):
        nl = "\r\n" if raw_line.endswith("\r\n") else ("\n" if raw_line.endswith("\n") else "")
        body = raw_line[: -len(nl)] if nl else raw_line
        stripped = body.strip()

        if not stripped or stripped.startswith((";", "#")):
            out_lines.append(raw_line)
            continue

        m = SECTION_RE.match(stripped)
        if m:
            cur_sec = m.group(1).strip()
            out_lines.append(raw_line)
            continue

        if "=" not in body or cur_sec is None:
            out_lines.append(raw_line)
            continue

        key_raw, rhs = body.split("=", 1)
        key = key_raw.strip()
        combined_val, comment = _split_value_and_comment(rhs)

        if not _contains_redacted_placeholder(combined_val):
            out_lines.append(raw_line)
            continue

        lookup = old_values.get((cur_sec.lower(), key.lower()))
        if lookup is None:
            unresolved_keys.append(f"[{cur_sec}] {key} (missing in old parms.ini)")
            out_lines.append(raw_line)
            continue

        old_val = lookup[0]
        new_val = None

        # Case 1: JSON object/array with nested "***" fields (e.g. [alpaca_api] credentials)
        c_strip = combined_val.strip()
        if (c_strip.startswith("{") and c_strip.endswith("}")) or (
            c_strip.startswith("[") and c_strip.endswith("]")
        ):
            try:
                c_obj = json.loads(c_strip)
                o_obj = json.loads(old_val.strip())
                merged_obj, r_cnt, u_cnt = _merge_json_secrets(c_obj, o_obj)
                if r_cnt > 0:
                    new_val = json.dumps(merged_obj)
                    restored_keys.append(f"[{cur_sec}] {key} ({r_cnt} JSON field(s))")
                if u_cnt > 0:
                    unresolved_keys.append(
                        f"[{cur_sec}] {key} ({u_cnt} JSON field(s) missing or still '***')"
                    )
            except json.JSONDecodeError:
                unresolved_keys.append(f"[{cur_sec}] {key} (invalid JSON in old parms.ini)")
                out_lines.append(raw_line)
                continue
        # Case 2: Scalar redacted value ("***" or ***)
        elif _is_redacted_scalar(combined_val):
            if _unquote(old_val).strip() in {"", REDACTED_SENTINEL}:
                unresolved_keys.append(f"[{cur_sec}] {key} (empty or still '***' in old parms.ini)")
                out_lines.append(raw_line)
                continue
            new_val = _format_scalar_like_combined(combined_val, old_val)
            restored_keys.append(f"[{cur_sec}] {key}")

        if new_val is None:
            out_lines.append(raw_line)
            continue

        indent = key_raw[: len(key_raw) - len(key_raw.lstrip())]
        lhs = f"{indent}{key} = {new_val}"
        if comment:
            pad = " " * max(2, 45 - len(lhs))
            rebuilt = f"{lhs}{pad}{comment}{nl or default_nl}"
        else:
            rebuilt = f"{lhs}{nl or default_nl}"
        out_lines.append(rebuilt)

    final_text = "".join(out_lines)
    validate_ini_output(final_text, str(output_path or combined_path))

    target = output_path or combined_path
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        in_place = target.resolve() == combined_path.resolve()
        if in_place and make_backup:
            bak_path = target.with_name(target.name + ".bak")
            shutil.copy2(combined_path, bak_path)

        final_bytes = (b"\xef\xbb\xbf" if has_bom else b"") + final_text.encode("utf-8")
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".parms_restore_",
                suffix=".ini",
                dir=str(target.parent),
                delete=False,
            ) as tmp_file:
                tmp_file.write(final_bytes)
                tmp_path = Path(tmp_file.name)
            try:
                orig_mode = combined_path.stat().st_mode
                os.chmod(tmp_path, orig_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp_path, target)
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    return restored_keys, unresolved_keys, target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Restore redacted ('***') secret and API key values in a combined parms.ini "
            "file using values from an existing environment parms.ini file."
        )
    )
    parser.add_argument(
        "combined_file",
        type=Path,
        help="Path to File 1: the new combined parms.ini file containing '***' placeholders",
    )
    parser.add_argument(
        "old_parms_file",
        type=Path,
        help="Path to File 2: the old live parms.ini file containing real secrets/keys",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Optional output file path (defaults to updating combined_file in place)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report which [section] keys would be restored without writing any files",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a .bak backup when updating combined_file in place",
    )
    args = parser.parse_args(argv)

    try:
        restored, unresolved, target = restore_secrets(
            combined_path=args.combined_file,
            old_parms_path=args.old_parms_file,
            output_path=args.output,
            dry_run=args.dry_run,
            make_backup=not args.no_backup,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    mode_tag = "DRY-RUN (no files written)" if args.dry_run else f"UPDATED -> {target}"
    print(f"[{mode_tag}] Restored {len(restored)} key(s):")
    for item in restored:
        print(f"  + {item}")

    if unresolved:
        print(f"WARNING: {len(unresolved)} redacted key(s) could not be restored:")
        for item in unresolved:
            print(f"  ! {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
