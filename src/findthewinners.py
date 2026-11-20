#!/usr/bin/env python3
"""Standalone Option Strike Winner Analyzer (findthewinners.py).

Uses the strike analyzer logic from multiStreamRSI (without requiring the
[feature_toggles] strike_analyzer_enabled toggle or any running web server/UI)
to evaluate candidate option strikes (-2 ITM to +4 OTM) from option buy time
through the end of the NYSE trading session.

Outputs a nicely formatted report file to disk and prints two summaries at
the end of execution:
1. A winning-contract-only summary per symbol with detailed reasons explaining
   why each contract won (Buy Time vs End of Session).
2. An overall strategy summary across all signals and symbols answering:
   - Did we buy the correct contract?
   - What is the optimal duration to hold before selling?
"""

from __future__ import annotations

import argparse
import configparser
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time as clock_time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc
MINUTE = timedelta(minutes=1)
MARKET_TZ = ZoneInfo("America/New_York")
OPEN_TIME = clock_time(9, 30)
CLOSE_TIME = clock_time(16, 0)

_TIMEZONE_ALIASES = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "PACIFIC": "America/Los_Angeles",
    "US/PACIFIC": "America/Los_Angeles",
    "AMERICA/LOS_ANGELES": "America/Los_Angeles",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "EASTERN": "America/New_York",
    "US/EASTERN": "America/New_York",
    "AMERICA/NEW_YORK": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CT": "America/Chicago",
    "CENTRAL": "America/Chicago",
    "US/CENTRAL": "America/Chicago",
    "AMERICA/CHICAGO": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MT": "America/Denver",
    "MOUNTAIN": "America/Denver",
    "US/MOUNTAIN": "America/Denver",
    "AMERICA/DENVER": "America/Denver",
    "UTC": "UTC",
    "GMT": "UTC",
    "Z": "UTC",
}

_CHAIN_TZ_ABBR_MAP = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "UTC": "UTC",
    "GMT": "UTC",
}

OPTION_CONTRACT_SYMBOL_RE = re.compile(
    r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$"
)
_OPTIONCHAIN_BUY_RE = re.compile(
    r"^optionchain-(.+?)-([A-Za-z0-9._-]+)-BUY-rsi([0-9.naNA]+)-(\d{8}T\d{6})_([A-Za-z]+)-([0-9a-fA-F]+)\.json$"
)
_OPTIONCHAIN_SELL_RE = re.compile(
    r"^optionchain-(.+?)-([A-Za-z0-9._-]+)-SELL-(.+?)-rsi([0-9.naNA]+)-(\d{8}T\d{6})_([A-Za-z]+)-([0-9a-fA-F]+)\.json$"
)
_OPTION_LOG_HEADER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?)\s+"
    r"(INFO|WARNING|ERROR|DEBUG|CRITICAL)\s+(\S+)(?:\s+(.*))?$"
)
_OPTION_KV_KEY_RE = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_%]*)=")

_STRIKE_RANK_LABELS = {
    -2: "-2 ITM",
    -1: "-1 ITM",
    0: "0 ATM",
    1: "+1 OTM",
    2: "+2 OTM",
    3: "+3 OTM",
    4: "+4 OTM",
}

HOLD_DURATION_BUCKETS = (
    ("5m", 5, "5 min"),
    ("10m", 10, "10 min"),
    ("15m", 15, "15 min"),
    ("20m", 20, "20 min"),
    ("30m", 30, "30 min"),
    ("45m", 45, "45 min"),
    ("60m", 60, "60 min"),
    ("90m", 90, "90 min"),
    ("120m", 120, "120 min"),
    ("180m", 180, "180 min"),
)

try:
    import pandas_market_calendars as mcal  # type: ignore

    _MARKET_CALENDAR = mcal.get_calendar("NYSE")
except Exception:
    _MARKET_CALENDAR = None


def _resolve_display_timezone(spec: str | None) -> tuple[ZoneInfo, str]:
    raw = str(spec or "PST").strip() or "PST"
    canonical = _TIMEZONE_ALIASES.get(raw.upper(), raw)
    try:
        return ZoneInfo(canonical), canonical
    except Exception:
        return ZoneInfo("America/Los_Angeles"), "America/Los_Angeles"


def _opt_safe_float(val, default=None):
    if val is None or isinstance(val, bool):
        return default
    try:
        num = float(val)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default


def _avg_or_none(vals: list[float], dec: int = 2):
    return round(sum(vals) / len(vals), dec) if vals else None


def _median_or_none(vals: list[float], dec: int = 2):
    if not vals:
        return None
    s = sorted(float(v) for v in vals)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return round(s[mid], dec)
    return round((s[mid - 1] + s[mid]) / 2.0, dec)


def _quantile_or_none(vals: list[float], q: float, dec: int = 1):
    if not vals:
        return None
    s = sorted(float(v) for v in vals)
    if len(s) == 1:
        return round(s[0], dec)
    pos = max(0.0, min(1.0, q)) * (len(s) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return round(s[lo], dec)
    frac = pos - lo
    return round(s[lo] * (1.0 - frac) + s[hi] * frac, dec)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def minute_utc(value: datetime) -> datetime:
    return as_utc(value).replace(second=0, microsecond=0)


def _opt_parse_utc(raw):
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return as_utc(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return as_utc(dt)
        except ValueError:
            return None
    return None


def _parse_occ_contract(symbol: str):
    sym = str(symbol or "").strip().upper()
    m = OPTION_CONTRACT_SYMBOL_RE.match(sym)
    if not m:
        return False, sym, "EQUITY", None, None
    underlying, yy, mm, dd, cp, strike_raw = m.groups()
    right = "CALL" if cp == "C" else "PUT"
    strike = round(int(strike_raw) / 1000.0, 4)
    expiry = f"20{yy}-{mm}-{dd}"
    return True, underlying, right, strike, expiry


@lru_cache(maxsize=512)
def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """Return NYSE regular-session (open_utc, close_utc) for a trading date."""
    if _MARKET_CALENDAR is not None:
        try:
            schedule = _MARKET_CALENDAR.schedule(start_date=day, end_date=day)
            if schedule.empty:
                return None
            row = schedule.iloc[0]
            return (
                as_utc(row["market_open"].to_pydatetime()),
                as_utc(row["market_close"].to_pydatetime()),
            )
        except Exception:
            pass

    if day.weekday() >= 5:
        return None
    open_dt = datetime.combine(day, OPEN_TIME, tzinfo=MARKET_TZ).astimezone(UTC)
    close_dt = datetime.combine(day, CLOSE_TIME, tzinfo=MARKET_TZ).astimezone(UTC)
    return (open_dt, close_dt)


def session_bounds_for_dt(dt_utc: datetime) -> tuple[datetime, datetime]:
    """Return (session_open_utc, session_close_utc) for the local NYSE date of dt_utc."""
    local_day = as_utc(dt_utc).astimezone(MARKET_TZ).date()
    bounds = session_bounds(local_day)
    if bounds is not None:
        return bounds
    open_dt = datetime.combine(local_day, OPEN_TIME, tzinfo=MARKET_TZ).astimezone(UTC)
    close_dt = datetime.combine(local_day, CLOSE_TIME, tzinfo=MARKET_TZ).astimezone(UTC)
    return (open_dt, close_dt)


def _strip_ini_value(raw: str) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    if (txt.startswith('"') and txt.endswith('"')) or (
        txt.startswith("'") and txt.endswith("'")
    ):
        if len(txt) >= 2:
            return txt[1:-1].strip()
    for sep in (" #", " ;", "\t#", "\t;"):
        if sep in txt:
            txt = txt.split(sep, 1)[0].strip()
    return txt


def load_standalone_config(ini_path: Path | None) -> dict:
    """Load display timezone and Alpaca credentials from parms.ini + environment."""
    cfg_out = {
        "ini_path": str(ini_path) if ini_path else None,
        "display_timezone": os.getenv("DISPLAY_TIMEZONE", "PST").strip() or "PST",
        "alpaca_key": os.getenv("ALPACA_API_KEY", "").strip(),
        "alpaca_secret": os.getenv("ALPACA_SECRET_KEY", "").strip(),
        "alpaca_paper": os.getenv("ALPACA_PAPER", "true").strip().lower()
        in {"1", "true", "yes"},
    }

    if ini_path is None or not ini_path.is_file():
        return cfg_out

    raw_text = ini_path.read_text(encoding="utf-8-sig", errors="replace")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(raw_text)
    except Exception:
        parser = None

    if parser is not None:
        for sec in ("display", "feature_toggles"):
            if parser.has_option(sec, "timezone"):
                val = _strip_ini_value(parser.get(sec, "timezone", fallback=""))
                if val:
                    cfg_out["display_timezone"] = val
                    break
        if parser.has_option("option_buyer", "paper"):
            raw_paper = _strip_ini_value(
                parser.get("option_buyer", "paper", fallback="true")
            ).lower()
            cfg_out["alpaca_paper"] = raw_paper in {"1", "true", "yes"}

        for sec in ("CREDENTIALS", "credentials", "alpaca_api", "alpaca"):
            if parser.has_section(sec):
                for key_opt in ("api_key", "alpaca_key", "key_id"):
                    if not cfg_out["alpaca_key"] and parser.has_option(sec, key_opt):
                        cfg_out["alpaca_key"] = _strip_ini_value(
                            parser.get(sec, key_opt, fallback="")
                        )
                for sec_opt in ("secret_key", "alpaca_secret"):
                    if not cfg_out["alpaca_secret"] and parser.has_option(sec, sec_opt):
                        cfg_out["alpaca_secret"] = _strip_ini_value(
                            parser.get(sec, sec_opt, fallback="")
                        )

    # Fallback: scan JSON-style CREDENTIALS block if configparser didn't populate keys
    if not cfg_out["alpaca_key"] or not cfg_out["alpaca_secret"]:
        for key_name, target in (
            ("api_key", "alpaca_key"),
            ("secret_key", "alpaca_secret"),
        ):
            if cfg_out[target]:
                continue
            m = re.search(
                rf'["\']?{key_name}["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                raw_text,
            )
            if m:
                cfg_out[target] = m.group(1).strip()

    return cfg_out


def _parse_chain_tz_stamp(stamp_str: str, tz_abbr: str, display_tz: ZoneInfo):
    try:
        naive = datetime.strptime(stamp_str, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    tz_name = _CHAIN_TZ_ABBR_MAP.get(str(tz_abbr or "").strip().upper())
    try:
        tz = ZoneInfo(tz_name) if tz_name else display_tz
    except Exception:
        tz = display_tz
    return naive.replace(tzinfo=tz).astimezone(UTC)


def _parse_optionchain_file_meta(path: Path, payload: dict, display_tz: ZoneInfo):
    """Extract (signal_side, symbol, rsi, event_dt_utc, source, expiry, trigger)."""
    name = path.name
    side = None
    sym = None
    rsi = None
    dt_utc = None
    source = None
    trigger = None

    m_buy = _OPTIONCHAIN_BUY_RE.match(name)
    if m_buy:
        source, sym, rsi_raw, stamp_s, tz_s, _ = m_buy.groups()
        side = "BUY"
        rsi = _opt_safe_float(rsi_raw, None)
        dt_utc = _parse_chain_tz_stamp(stamp_s, tz_s, display_tz)
    else:
        m_sell = _OPTIONCHAIN_SELL_RE.match(name)
        if m_sell:
            source, sym, trigger, rsi_raw, stamp_s, tz_s, _ = m_sell.groups()
            side = "SELL"
            rsi = _opt_safe_float(rsi_raw, None)
            dt_utc = _parse_chain_tz_stamp(stamp_s, tz_s, display_tz)

    if isinstance(payload, dict):
        if side is None:
            raw_sig = str(
                payload.get("signal")
                or payload.get("side")
                or payload.get("action")
                or ""
            ).strip().upper()
            if raw_sig in {"BUY", "SELL"}:
                side = raw_sig
        if payload.get("underlying"):
            sym = str(payload.get("underlying")).strip().upper()
        ts_resolved = None
        for ts_k in ("sell_utc_timestamp", "utc_timestamp"):
            parsed_ts = _opt_parse_utc(payload.get(ts_k))
            if parsed_ts is not None:
                ts_resolved = parsed_ts
                break
        if ts_resolved is None:
            raw_ts = payload.get("timestamp")
            if isinstance(raw_ts, str) and raw_ts.strip():
                try:
                    parsed_raw = datetime.fromisoformat(
                        raw_ts.strip().replace("Z", "+00:00")
                    )
                    if parsed_raw.tzinfo is not None:
                        ts_resolved = as_utc(parsed_raw)
                    elif dt_utc is None:
                        ts_resolved = parsed_raw.replace(tzinfo=display_tz).astimezone(
                            UTC
                        )
                except ValueError:
                    pass
        if ts_resolved is not None:
            dt_utc = ts_resolved
        if trigger is None and payload.get("exit_trigger"):
            trigger = str(payload.get("exit_trigger"))

    if dt_utc is None:
        try:
            dt_utc = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            dt_utc = datetime.now(UTC)

    expiry = None
    if isinstance(payload, dict):
        exp_raw = str(payload.get("expiration_date") or "").strip()
        if exp_raw:
            expiry = exp_raw[:10]
        if not expiry:
            for item in payload.get("chain_snapshot") or []:
                if isinstance(item, dict) and item.get("contract"):
                    is_o, _, _, _, exp_p = _parse_occ_contract(item["contract"])
                    if is_o and exp_p:
                        expiry = exp_p
                        break

    return {
        "signal": side,
        "symbol": str(sym or "").strip().upper(),
        "rsi": rsi,
        "dt_utc": as_utc(dt_utc),
        "source": source
        or (payload.get("provider") if isinstance(payload, dict) else None),
        "expiry": expiry,
        "trigger": trigger,
    }


def _load_minute_summary_kinematics_index(log_root: Path) -> dict:
    """Load {(minute_utc_iso, symbol): kinematics_dict} from minute-summary logs."""
    if not log_root.exists() or not log_root.is_dir():
        return {}

    files = sorted(
        list(log_root.glob("minute-summary-*.jsonl"))
        + list(log_root.glob("minute-summary-*.json"))
    )
    idx: dict[tuple[str, str], dict] = {}
    prov_priority = {
        "TRADINGVIEW": 5,
        "ALPACA": 4,
        "YAHOOFINANCE": 3,
        "TRADIER": 2,
        "TASTYTRADE": 1,
    }

    for p in files:
        if not p.is_file():
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    dt = _opt_parse_utc(
                        rec.get("minute_start_utc")
                        or rec.get("minute_utc")
                        or rec.get("minute")
                    )
                    if dt is None:
                        continue
                    min_iso = minute_utc(dt).isoformat()
                    top_provider = str(rec.get("provider") or "").strip().upper()
                    syms_block = (
                        rec.get("symbols")
                        if isinstance(rec.get("symbols"), dict)
                        else {}
                    )
                    for sym_k, sym_val in syms_block.items():
                        if not isinstance(sym_val, dict):
                            continue
                        sym_u = str(sym_k).strip().upper()
                        # Shape A: {close, rsi, kinematics, ...} directly under symbol
                        if "close" in sym_val or "rsi" in sym_val or "status" in sym_val:
                            cand = sym_val
                            best_p = top_provider or str(
                                cand.get("provider") or "UNKNOWN"
                            ).upper()
                        else:
                            # Shape B: {provider_name: {close, rsi, ...}}
                            best_p = None
                            cand = None
                            for prov_name in (
                                "TRADINGVIEW",
                                "ALPACA",
                                "YAHOOFINANCE",
                                "TRADIER",
                                "TASTYTRADE",
                            ):
                                c_item = sym_val.get(prov_name)
                                if isinstance(c_item, dict) and (
                                    _opt_safe_float(c_item.get("close"), None)
                                    is not None
                                    or _opt_safe_float(c_item.get("rsi"), None)
                                    is not None
                                ):
                                    best_p, cand = prov_name, c_item
                                    if _opt_safe_float(c_item.get("rsi"), None) is not None:
                                        break
                            if cand is None:
                                for prov_name, c_item in sym_val.items():
                                    if isinstance(c_item, dict):
                                        best_p, cand = str(prov_name).upper(), c_item
                                        break
                        if not isinstance(cand, dict):
                            continue

                        close_v = _opt_safe_float(cand.get("close"), None)
                        rsi_v = _opt_safe_float(cand.get("rsi"), None)
                        if close_v is None and rsi_v is None:
                            continue

                        kin_nested = (
                            cand.get("kinematics")
                            if isinstance(cand.get("kinematics"), dict)
                            else {}
                        )
                        v_val = _opt_safe_float(
                            cand.get("smoothed_velocity")
                            if cand.get("smoothed_velocity") is not None
                            else (
                                cand.get("price_velocity")
                                if cand.get("price_velocity") is not None
                                else kin_nested.get("price_velocity_1m")
                            ),
                            None,
                        )
                        a_val = _opt_safe_float(
                            cand.get("smoothed_acceleration")
                            if cand.get("smoothed_acceleration") is not None
                            else (
                                cand.get("price_acceleration")
                                if cand.get("price_acceleration") is not None
                                else kin_nested.get("price_acceleration_1m")
                            ),
                            None,
                        )
                        regime_val = cand.get("regime") or kin_nested.get("regime")

                        new_entry = {
                            "provider": best_p,
                            "close": close_v,
                            "rsi": rsi_v,
                            "v": v_val,
                            "a": a_val,
                            "regime": regime_val,
                        }
                        existing = idx.get((min_iso, sym_u))
                        if existing is None:
                            idx[(min_iso, sym_u)] = new_entry
                        else:
                            new_score = (
                                1 if rsi_v is not None else 0,
                                prov_priority.get(best_p or "", 0),
                            )
                            old_score = (
                                1 if existing.get("rsi") is not None else 0,
                                prov_priority.get(existing.get("provider") or "", 0),
                            )
                            if new_score > old_score:
                                idx[(min_iso, sym_u)] = new_entry
        except OSError:
            continue

    return idx


def _infer_spot_from_chain_snapshot(
    payload: dict, dt_utc: datetime, symbol: str, kin_index: dict
) -> tuple[float | None, str]:
    """Recover spot price at chain capture time."""
    if isinstance(payload, dict):
        sp = _opt_safe_float(payload.get("spot_ask_price"), None)
        if sp is not None and sp > 0:
            return round(sp, 4), "snapshot"

    base_min = minute_utc(as_utc(dt_utc))
    for offset in (0, -1, 1, -2):
        m_iso = (base_min + timedelta(minutes=offset)).isoformat()
        kin = kin_index.get((m_iso, symbol))
        if kin and kin.get("close") is not None and kin["close"] > 0:
            return round(float(kin["close"]), 4), "minute_summary"

    chain = payload.get("chain_snapshot") if isinstance(payload, dict) else None
    pairs = []
    for c in chain or []:
        if not isinstance(c, dict):
            continue
        k = _opt_safe_float(c.get("strike"), None)
        d = _opt_safe_float(c.get("distance_from_spot"), None)
        if k is not None and d is not None and k > 0 and d >= 0:
            pairs.append((k, d))
    if not pairs:
        return None, "missing"

    candidates = []
    for k, d in pairs[:30]:
        if k + d > 0:
            candidates.append(round(k + d, 4))
        if k - d > 0:
            candidates.append(round(k - d, 4))
    if not candidates:
        return None, "missing"

    best_s = None
    best_err = None
    for cand in set(candidates):
        err = sum(abs(abs(k - cand) - d) for k, d in pairs)
        if best_err is None or err < best_err:
            best_err = err
            best_s = cand
    if best_s is not None and best_s > 0:
        return round(best_s, 4), "inferred_from_chain"
    return None, "missing"


def _parse_option_log_timestamp(ts_full: str):
    if not ts_full:
        return None, 0
    s = ts_full.strip()
    ms = 0
    base = s
    if "," in s:
        base, ms_part = s.split(",", 1)
        try:
            ms = int(ms_part[:3].ljust(3, "0"))
        except ValueError:
            ms = 0
    try:
        naive = datetime.strptime(base, "%Y-%m-%d %H:%M:%S").replace(
            microsecond=ms * 1000
        )
        return naive, ms
    except ValueError:
        return None, ms


def _read_logged_trades(
    log_root: Path, display_tz: ZoneInfo, include_dry_run: bool = False
) -> list[dict]:
    """Reconstruct executed (and optionally dry-run) option trades from logs."""
    buys: list[dict] = []
    sells: list[dict] = []

    for subdir, stream in (("option_buyer", "BUY"), ("option_seller", "SELL")):
        d = log_root / subdir
        if not d.exists() or not d.is_dir():
            continue
        for path in sorted(d.glob("*.log")):
            if not path.is_file():
                continue
            try:
                raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            file_recs = []
            utc_votes = 0
            disp_votes = 0
            for raw in raw_lines:
                line = raw.strip()
                if not line:
                    continue
                if stream == "BUY":
                    is_target = "OPTION_BUY_SUBMITTED" in line or (
                        include_dry_run and "OPTION_BUY_DRY_RUN" in line
                    )
                else:
                    is_target = "OPTION_SELL_SUBMITTED" in line or (
                        include_dry_run and "OPTION_SELL_DRY_RUN" in line
                    )
                if not is_target:
                    continue
                m = _OPTION_LOG_HEADER_RE.match(line)
                if not m:
                    continue
                ts_full, _, event, rest = (
                    m.group(1),
                    m.group(2),
                    m.group(3),
                    (m.group(4) or "").strip(),
                )
                kv_m = list(_OPTION_KV_KEY_RE.finditer(rest))
                kv = {}
                for idx, km in enumerate(kv_m):
                    k = km.group(1)
                    v_start = km.end()
                    v_end = (
                        kv_m[idx + 1].start() if idx + 1 < len(kv_m) else len(rest)
                    )
                    kv[k] = rest[v_start:v_end].strip()

                naive, _ = _parse_option_log_timestamp(ts_full)
                if naive is None:
                    continue
                dt_utc = naive.replace(tzinfo=UTC)
                dt_disp = naive.replace(tzinfo=display_tz)
                hint = None
                raw_min = kv.get("minute")
                if raw_min and raw_min not in {"n/a", "none", "None"}:
                    p_min = _opt_parse_utc(raw_min)
                    if p_min is not None:
                        diff_u = abs((dt_utc - p_min).total_seconds())
                        diff_d = abs((dt_disp - p_min).total_seconds())
                        if diff_u + 1800.0 < diff_d and diff_u < 7200.0:
                            hint = "utc"
                            utc_votes += 1
                        elif diff_d + 1800.0 < diff_u and diff_d < 7200.0:
                            hint = "display"
                            disp_votes += 1

                file_recs.append((event, kv, naive, hint, "DRY_RUN" in event))

            file_is_utc = utc_votes > 0 and utc_votes > disp_votes
            for event, kv, naive, hint, is_dry in file_recs:
                use_utc = (hint == "utc") or (hint is None and file_is_utc)
                exec_dt = (
                    naive.replace(tzinfo=UTC)
                    if use_utc
                    else naive.replace(tzinfo=display_tz).astimezone(UTC)
                )
                if stream == "BUY":
                    und = str(kv.get("symbol") or "").strip().upper()
                    con = str(kv.get("contract") or "").strip().upper()
                else:
                    und = str(kv.get("underlying") or "").strip().upper()
                    con = str(
                        kv.get("contract") or kv.get("symbol") or ""
                    ).strip().upper()
                if con and (not und or und in {"NONE", "N/A"}):
                    _, und, _, _, _ = _parse_occ_contract(con)
                if not con or con in {"NONE", "N/A"}:
                    continue
                _, parsed_und, _, _, expiry = _parse_occ_contract(con)
                if not und or und in {"NONE", "N/A"}:
                    und = parsed_und

                qty_val = _opt_safe_float(kv.get("qty"), 1.0) or 1.0
                ask_px = _opt_safe_float(kv.get("ask"), None)
                bid_px = _opt_safe_float(kv.get("bid"), None)
                lim_px = _opt_safe_float(kv.get("limit_price"), None)
                tot_buy = _opt_safe_float(kv.get("total_purchase"), None)
                tot_sell = _opt_safe_float(kv.get("total_sale"), None)

                unit_px = None
                if stream == "BUY":
                    if tot_buy is not None and tot_buy > 0 and qty_val > 0:
                        unit_px = round(tot_buy / (qty_val * 100.0), 4)
                    else:
                        unit_px = lim_px or ask_px or bid_px
                    buys.append(
                        {
                            "contract": con,
                            "underlying": und,
                            "expiry": expiry,
                            "dt_utc": exec_dt,
                            "price": unit_px,
                            "qty": qty_val,
                            "dry_run": is_dry,
                            "reason": kv.get("trigger") or event,
                        }
                    )
                else:
                    if tot_sell is not None and tot_sell > 0 and qty_val > 0:
                        unit_px = round(tot_sell / (qty_val * 100.0), 4)
                    else:
                        unit_px = lim_px or bid_px or ask_px
                    sells.append(
                        {
                            "contract": con,
                            "underlying": und,
                            "expiry": expiry,
                            "dt_utc": exec_dt,
                            "price": unit_px,
                            "qty": qty_val,
                            "dry_run": is_dry,
                            "reason": kv.get("kind") or kv.get("trigger") or event,
                        }
                    )

    buys.sort(key=lambda r: r["dt_utc"])
    sells.sort(key=lambda r: r["dt_utc"])

    trades = []
    used_sells = set()
    for b in buys:
        matched_sell = None
        for idx, s in enumerate(sells):
            if idx in used_sells:
                continue
            if s["contract"] != b["contract"]:
                continue
            if s["dt_utc"] >= b["dt_utc"]:
                matched_sell = s
                used_sells.add(idx)
                break

        entry_p = b.get("price")
        exit_p = matched_sell.get("price") if matched_sell else None
        sold_dt = matched_sell.get("dt_utc") if matched_sell else None
        hold_min = (
            round((sold_dt - b["dt_utc"]).total_seconds() / 60.0, 2)
            if sold_dt is not None
            else None
        )
        pnl_pct = (
            round(((exit_p - entry_p) / entry_p) * 100.0, 2)
            if (entry_p and exit_p and entry_p > 0)
            else None
        )
        pnl_dollars = (
            round((exit_p - entry_p) * 100.0 * b["qty"], 2)
            if (entry_p and exit_p)
            else None
        )
        trades.append(
            {
                "status": "closed" if matched_sell else "open",
                "contract": b["contract"],
                "underlying": b["underlying"],
                "expiry": b["expiry"],
                "entry_price": entry_p,
                "exit_price": exit_p,
                "purchased_dt": b["dt_utc"],
                "sold_dt": sold_dt,
                "hold_minutes": hold_min,
                "total_pnl_pct": pnl_pct,
                "total_pnl_dollars": pnl_dollars,
                "dry_run": b["dry_run"],
                "reason": (
                    matched_sell.get("reason") if matched_sell else b.get("reason")
                ),
            }
        )
    return trades


def _fetch_option_bars_alpaca(
    symbol_ranges: dict[str, tuple[datetime, datetime]],
    api_key: str,
    api_secret: str,
) -> tuple[dict[str, dict[datetime, dict]], bool, str]:
    """Fetch 1m option bars for each contract using alpaca-py or REST fallback."""
    bars_out: dict[str, dict[datetime, dict]] = {s: {} for s in symbol_ranges}
    if not symbol_ranges:
        return bars_out, True, "No contracts to fetch"
    if not api_key or not api_secret:
        return (
            bars_out,
            False,
            "Alpaca credentials unavailable (set ALPACA_API_KEY/ALPACA_SECRET_KEY or lib/parms.ini)",
        )

    norm_ranges = {
        sym: (minute_utc(as_utc(r[0])), minute_utc(as_utc(r[1])))
        for sym, r in symbol_ranges.items()
    }

    def _add_bar(per_min: dict, ts_raw, close_raw, high_raw, low_raw=None):
        ts = _opt_parse_utc(ts_raw)
        c_p = _opt_safe_float(close_raw, None)
        h_p = _opt_safe_float(high_raw, None)
        l_p = _opt_safe_float(low_raw, None)
        if ts is not None and c_p is not None and c_p > 0:
            mk = minute_utc(ts)
            h_eff = h_p if (h_p is not None and h_p >= c_p) else c_p
            l_eff = l_p if (l_p is not None and 0 < l_p <= c_p) else c_p
            per_min[mk] = {
                "c": round(float(c_p), 4),
                "h": round(float(h_eff), 4),
                "l": round(float(l_eff), 4),
            }

    # Try alpaca-py first (same SDK used in multiStreamRSI)
    used_sdk = False
    try:
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient  # type: ignore
        except ImportError:
            from alpaca.data.historical import OptionHistoricalDataClient  # type: ignore
        from alpaca.data.requests import OptionBarsRequest  # type: ignore
        from alpaca.data.timeframe import TimeFrame  # type: ignore

        client = OptionHistoricalDataClient(api_key, api_secret)
        used_sdk = True
        items = list(norm_ranges.items())
        chunk_size = 10
        for i in range(0, len(items), chunk_size):
            chunk = items[i : i + chunk_size]
            chunk_syms = [s for s, _ in chunk]
            min_start = min(r[0] for _, r in chunk)
            max_end = max(r[1] for _, r in chunk) + MINUTE
            try:
                req = OptionBarsRequest(
                    symbol_or_symbols=chunk_syms,
                    timeframe=TimeFrame.Minute,
                    start=min_start,
                    end=max_end,
                )
                resp = client.get_option_bars(req)
                raw_dict = getattr(resp, "data", resp)
                if isinstance(raw_dict, dict):
                    for raw_sym, b_list in raw_dict.items():
                        s_u = str(raw_sym).strip().upper()
                        per_min = bars_out.setdefault(s_u, {})
                        for b in b_list or []:
                            if isinstance(b, dict):
                                _add_bar(
                                    per_min,
                                    b.get("timestamp") or b.get("t"),
                                    b.get("close") or b.get("c"),
                                    b.get("high") or b.get("h"),
                                    b.get("low") or b.get("l"),
                                )
                            else:
                                _add_bar(
                                    per_min,
                                    getattr(b, "timestamp", None),
                                    getattr(b, "close", None),
                                    getattr(b, "high", None),
                                    getattr(b, "low", None),
                                )
            except Exception:
                for s_u, (s_min, e_min) in chunk:
                    try:
                        req = OptionBarsRequest(
                            symbol_or_symbols=s_u,
                            timeframe=TimeFrame.Minute,
                            start=s_min,
                            end=e_min + MINUTE,
                        )
                        resp = client.get_option_bars(req)
                        raw_dict = getattr(resp, "data", resp)
                        if isinstance(raw_dict, dict):
                            b_list = raw_dict.get(s_u) or (
                                next(iter(raw_dict.values()), []) if raw_dict else []
                            )
                            per_min = bars_out.setdefault(s_u, {})
                            for b in b_list or []:
                                if isinstance(b, dict):
                                    _add_bar(
                                        per_min,
                                        b.get("timestamp") or b.get("t"),
                                        b.get("close") or b.get("c"),
                                        b.get("high") or b.get("h"),
                                        b.get("low") or b.get("l"),
                                    )
                                else:
                                    _add_bar(
                                        per_min,
                                        getattr(b, "timestamp", None),
                                        getattr(b, "close", None),
                                        getattr(b, "high", None),
                                        getattr(b, "low", None),
                                    )
                    except Exception:
                        continue
    except ImportError:
        used_sdk = False

    if not used_sdk:
        # Pure stdlib REST fallback: https://data.alpaca.markets/v1beta1/options/bars
        items = list(norm_ranges.items())
        chunk_size = 10
        base_url = "https://data.alpaca.markets/v1beta1/options/bars"
        for i in range(0, len(items), chunk_size):
            chunk = items[i : i + chunk_size]
            chunk_syms = [s for s, _ in chunk]
            min_start = min(r[0] for _, r in chunk)
            max_end = max(r[1] for _, r in chunk) + MINUTE
            page_token = None
            while True:
                params = {
                    "symbols": ",".join(chunk_syms),
                    "timeframe": "1Min",
                    "start": min_start.isoformat().replace("+00:00", "Z"),
                    "end": max_end.isoformat().replace("+00:00", "Z"),
                    "limit": "10000",
                }
                if page_token:
                    params["page_token"] = page_token
                url = f"{base_url}?{urllib.parse.urlencode(params)}"
                req = urllib.request.Request(
                    url,
                    headers={
                        "APCA-API-KEY-ID": api_key,
                        "APCA-API-SECRET-KEY": api_secret,
                        "Accept": "application/json",
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=20.0) as resp:
                        raw_data = json.loads(resp.read().decode("utf-8"))
                except Exception as exc:
                    return (
                        bars_out,
                        False,
                        f"Alpaca REST option bars request failed: {exc}",
                    )
                for raw_sym, b_list in (raw_data.get("bars") or {}).items():
                    s_u = str(raw_sym).strip().upper()
                    per_min = bars_out.setdefault(s_u, {})
                    for b in b_list or []:
                        if isinstance(b, dict):
                            _add_bar(
                                per_min,
                                b.get("t") or b.get("timestamp"),
                                b.get("c") or b.get("close"),
                                b.get("h") or b.get("high"),
                                b.get("l") or b.get("low"),
                            )
                page_token = raw_data.get("next_page_token")
                if not page_token:
                    break

    # Trim to each symbol's requested range
    for s_u, (s_min, e_min) in norm_ranges.items():
        bars_out[s_u] = {
            mk: bval
            for mk, bval in bars_out.get(s_u, {}).items()
            if s_min <= mk <= e_min
        }

    with_bars = sum(1 for v in bars_out.values() if v)
    method = "alpaca-py" if used_sdk else "Alpaca REST"
    return (
        bars_out,
        True,
        f"{method} 1m bars fetched ({with_bars}/{len(bars_out)} contracts returned bars)",
    )


def _lookup_bar_near(con_bars: dict, target_min: datetime):
    if not con_bars:
        return None
    if target_min in con_bars:
        return con_bars[target_min].get("c")
    for delta_m in (-1, 1, -2, 2, -3):
        cand = con_bars.get(target_min + timedelta(minutes=delta_m))
        if cand and cand.get("c"):
            return cand["c"]
    return None


def _lookup_session_end_bar(
    con_bars: dict,
    buy_min: datetime,
    eval_end_dt: datetime,
    sell_chain: dict | None,
    contract: str,
    session_close_dt: datetime,
) -> tuple[float | None, datetime | None, str]:
    """Resolve the end-of-session option price between buy_min and eval_end_dt.

    In 1m bar data, the closing minute of a 16:00 ET regular session is the
    15:59 ET bar (eval_end_dt - 1m) or eval_end_dt. If the option did not trade
    in that exact minute, fall back to the latest bar in (buy_min, eval_end_dt].
    """
    if con_bars:
        close_bar_target = minute_utc(eval_end_dt) - MINUTE
        for probe in (
            minute_utc(eval_end_dt),
            close_bar_target,
            close_bar_target - MINUTE,
        ):
            if probe > buy_min and probe in con_bars and con_bars[probe].get("c"):
                return con_bars[probe]["c"], probe, "session_close_bar"

        valid_mins = sorted(
            bm
            for bm, bval in con_bars.items()
            if buy_min < bm <= eval_end_dt and bval.get("c")
        )
        if valid_mins:
            last_bm = valid_mins[-1]
            mins_before = max(
                0, int(round((session_close_dt - last_bm).total_seconds() / 60.0))
            )
            tag = (
                "session_close_bar"
                if mins_before <= 2
                else f"last_session_bar(-{mins_before}m)"
            )
            return con_bars[last_bm]["c"], last_bm, tag

    if sell_chain and contract in sell_chain:
        snap = sell_chain[contract]
        s_bid = _opt_safe_float(snap.get("bid"), None)
        s_ask = _opt_safe_float(snap.get("ask"), None)
        s_mid = _opt_safe_float(snap.get("mid"), None)
        s_last = _opt_safe_float(snap.get("last_price"), None)
        px = None
        if s_bid and s_bid > 0 and s_ask and s_ask > 0:
            px = round((s_bid + s_ask) / 2.0, 4)
        elif s_mid and s_mid > 0:
            px = s_mid
        elif s_last and s_last > 0:
            px = s_last
        elif s_bid and s_bid > 0:
            px = s_bid
        if px is not None:
            return px, None, "sell_chain_fallback"

    return None, None, "unavailable"


def analyze_session_winners(
    log_root: Path,
    display_tz: ZoneInfo,
    api_key: str = "",
    api_secret: str = "",
    symbol_filter: str = "",
    date_filter: str = "",
    limit: int | None = None,
    include_dry_run: bool = False,
    bars_override: dict | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """Run strike analyzer logic from option buy time to end of session."""
    chain_dir = log_root / "optionchains"
    sym_q = str(symbol_filter or "").strip().upper()
    date_q = str(date_filter or "").strip()
    ref_now = as_utc(now_utc or datetime.now(UTC))

    if not chain_dir.exists() or not chain_dir.is_dir():
        return {
            "log_root": str(log_root),
            "chain_dir": str(chain_dir),
            "symbol_filter": sym_q,
            "date_filter": date_q,
            "bars_available": False,
            "bars_note": f"Option chain directory not found: {chain_dir}",
            "summary": {
                "total_signals": 0,
                "unique_symbols": 0,
                "executed_signals": 0,
                "by_rank": [],
                "best_rank_session": None,
                "overall_strategy": None,
            },
            "events": [],
            "winners_by_symbol": [],
        }

    kin_index = _load_minute_summary_kinematics_index(log_root)
    all_trades = _read_logged_trades(
        log_root, display_tz=display_tz, include_dry_run=include_dry_run
    )

    buy_records = []
    sell_records = []

    for path in sorted(chain_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        meta = _parse_optionchain_file_meta(path, payload, display_tz=display_tz)
        if not meta["symbol"]:
            continue
        if sym_q and meta["symbol"] != sym_q:
            continue
        if date_q:
            local_day_str = meta["dt_utc"].astimezone(MARKET_TZ).date().isoformat()
            disp_day_str = meta["dt_utc"].astimezone(display_tz).date().isoformat()
            if date_q not in {local_day_str, disp_day_str}:
                continue
        if meta["signal"] == "BUY":
            buy_records.append((meta["dt_utc"], path, payload, meta))
        elif meta["signal"] == "SELL":
            sell_records.append((meta["dt_utc"], path, payload, meta))

    buy_records.sort(key=lambda item: item[0], reverse=True)
    if limit is not None and limit > 0:
        buy_records = buy_records[:limit]

    sell_by_sym_exp: dict[tuple[str, str], list] = {}
    for s_dt, _, s_payload, s_meta in sell_records:
        key = (s_meta["symbol"], s_meta["expiry"] or "")
        by_con = {}
        for c in s_payload.get("chain_snapshot") or []:
            if isinstance(c, dict) and c.get("contract"):
                by_con[str(c["contract"]).strip().upper()] = c
        sell_by_sym_exp.setdefault(key, []).append((s_dt, s_meta, by_con))

    prepared_events = []
    bar_ranges: dict[str, tuple[datetime, datetime]] = {}

    for signal_dt, path, payload, meta in buy_records:
        sym = meta["symbol"]
        spot, spot_source = _infer_spot_from_chain_snapshot(
            payload, signal_dt, sym, kin_index
        )
        if spot is None or spot <= 0:
            continue

        raw_opt_type = str(payload.get("option_type") or "").strip().upper()
        right = "PUT" if raw_opt_type == "PUT" else "CALL"
        chain = [
            dict(c)
            for c in (payload.get("chain_snapshot") or [])
            if isinstance(c, dict)
            and _opt_safe_float(c.get("strike"), None) is not None
            and c.get("contract")
        ]
        if not raw_opt_type and chain:
            _, _, first_r, _, _ = _parse_occ_contract(chain[0]["contract"])
            if first_r in {"CALL", "PUT"}:
                right = first_r

        # Match executed trade by underlying + purchase time [-120s, +600s]
        matched_trade = None
        best_tdiff = None
        for tr in all_trades:
            if tr["underlying"] != sym:
                continue
            p_dt = tr.get("purchased_dt")
            if p_dt is None:
                continue
            diff = (p_dt - signal_dt).total_seconds()
            if -120.0 <= diff <= 600.0:
                if best_tdiff is None or abs(diff) < best_tdiff:
                    best_tdiff = abs(diff)
                    matched_trade = tr

        # Determine effective option buy time and NYSE session close
        option_buy_dt = (
            matched_trade["purchased_dt"]
            if (matched_trade and matched_trade.get("purchased_dt"))
            else signal_dt
        )
        buy_time_source = "executed_trade" if matched_trade else "signal_snapshot"
        buy_min = minute_utc(option_buy_dt)

        session_open_dt, session_close_dt = session_bounds_for_dt(option_buy_dt)
        session_in_progress = ref_now < session_close_dt
        eval_end_dt = (
            min(session_close_dt, minute_utc(ref_now) + MINUTE)
            if session_in_progress
            else session_close_dt
        )
        if eval_end_dt <= buy_min:
            eval_end_dt = session_close_dt

        # Match SELL exit-time option chain snapshot (prefer closest to session end if no trade sell)
        matched_sell_chain = None
        exp_key = (sym, meta["expiry"] or "")
        sell_cands = sell_by_sym_exp.get(exp_key, [])
        if sell_cands:
            best_sdiff = None
            for cand_dt, _, by_con in sell_cands:
                if buy_min <= cand_dt <= session_close_dt + timedelta(minutes=5):
                    sd = abs((session_close_dt - cand_dt).total_seconds())
                    if best_sdiff is None or sd < best_sdiff:
                        best_sdiff = sd
                        matched_sell_chain = by_con

        # Assign OTM ranks matching get_one_contract (+2 == 2nd OTM)
        if right == "CALL":
            itm_atm = sorted(
                [c for c in chain if float(c["strike"]) <= spot],
                key=lambda c: float(c["strike"]),
                reverse=True,
            )
            otm = sorted(
                [c for c in chain if float(c["strike"]) > spot],
                key=lambda c: float(c["strike"]),
            )
        else:
            itm_atm = sorted(
                [c for c in chain if float(c["strike"]) >= spot],
                key=lambda c: float(c["strike"]),
            )
            otm = sorted(
                [c for c in chain if float(c["strike"]) < spot],
                key=lambda c: float(c["strike"]),
                reverse=True,
            )

        ranked_cands = []
        for idx, c in enumerate(itm_atm[:3]):
            ranked_cands.append((-idx, c))
        for idx, c in enumerate(otm[:4], start=1):
            ranked_cands.append((idx, c))
        ranked_cands.sort(key=lambda pair: pair[0])

        start_req = buy_min - MINUTE
        end_req = eval_end_dt + MINUTE
        cand_list = []
        for rank, c in ranked_cands:
            con = str(c.get("contract") or "").strip().upper()
            if con and end_req > start_req:
                prev_r = bar_ranges.get(con)
                if prev_r is None:
                    bar_ranges[con] = (start_req, end_req)
                else:
                    bar_ranges[con] = (
                        min(prev_r[0], start_req),
                        max(prev_r[1], end_req),
                    )
            cand_list.append((rank, c, con))

        base_kin = (
            kin_index.get((buy_min.isoformat(), sym))
            or kin_index.get(((buy_min - MINUTE).isoformat(), sym))
            or {}
        )
        # Underlying spot at end of session from minute summaries (if present)
        end_min_probe = minute_utc(eval_end_dt)
        spot_eod = None
        for off_m in (0, -1, -2, -3, -5):
            k_eod = kin_index.get(((end_min_probe + timedelta(minutes=off_m)).isoformat(), sym))
            if k_eod and k_eod.get("close") and k_eod["close"] > 0:
                spot_eod = round(float(k_eod["close"]), 4)
                break

        prepared_events.append(
            {
                "event_id": path.name,
                "filename": path.name,
                "symbol": sym,
                "right": right,
                "expiry": meta["expiry"],
                "rsi": meta["rsi"] if meta["rsi"] is not None else base_kin.get("rsi"),
                "signal_dt": signal_dt,
                "option_buy_dt": option_buy_dt,
                "buy_time_source": buy_time_source,
                "buy_min": buy_min,
                "session_open_dt": session_open_dt,
                "session_close_dt": session_close_dt,
                "eval_end_dt": eval_end_dt,
                "session_in_progress": session_in_progress,
                "spot_price": spot,
                "spot_source": spot_source,
                "spot_eod": spot_eod,
                "kinematics": base_kin,
                "matched_trade": matched_trade,
                "matched_sell_chain": matched_sell_chain,
                "cand_list": cand_list,
            }
        )

    if bars_override is not None:
        bars_map = bars_override
        bars_available = True
        bars_note = f"Using supplied option bar dataset ({len(bars_map)} contracts)"
    else:
        bars_map, bars_available, bars_note = _fetch_option_bars_alpaca(
            bar_ranges, api_key=api_key, api_secret=api_secret
        )

    rank_stats = {
        rk: {
            "otm_rank": rk,
            "label": _STRIKE_RANK_LABELS[rk],
            "asks": [],
            "spreads": [],
            "deltas": [],
            "ret_15m": [],
            "ret_60m": [],
            "ret_session": [],
            "pnl_session": [],
            "ret_peak": [],
            "winner_count": 0,
        }
        for rk in range(-2, 5)
    }

    events_out = []
    executed_count = 0

    for ev in prepared_events:
        tr = ev["matched_trade"]
        if tr is not None:
            executed_count += 1
        matched_con = tr.get("contract") if tr else None
        sell_chain = ev["matched_sell_chain"]

        candidates_out = []
        for rk, c, con in ev["cand_list"]:
            bid = _opt_safe_float(c.get("bid"), 0.0) or 0.0
            ask = _opt_safe_float(c.get("ask"), 0.0) or 0.0
            mid = _opt_safe_float(c.get("mid"), None)
            if mid is None and bid > 0 and ask > 0:
                mid = round((bid + ask) / 2.0, 4)
            spread_pct = _opt_safe_float(c.get("spread_pct"), None)
            if spread_pct is None and mid and mid > 0 and ask >= bid:
                spread_pct = round(((ask - bid) / mid) * 100.0, 2)
            delta_v = _opt_safe_float(c.get("delta"), None)
            iv_v = _opt_safe_float(c.get("iv"), None)
            oi_raw = _opt_safe_float(c.get("open_interest"), None)
            oi_v = int(oi_raw) if oi_raw is not None else None

            con_bars = bars_map.get(con, {})
            entry_bar_px = _lookup_bar_near(con_bars, ev["buy_min"])
            entry_ref = (
                ask
                if ask > 0
                else (
                    mid
                    if (mid and mid > 0)
                    else (_opt_safe_float(c.get("last_price"), None) or entry_bar_px)
                )
            )
            entry_source = (
                "chain_ask"
                if ask > 0
                else ("chain_mid" if (mid and mid > 0) else "bar_or_last")
            )

            # Intraday checkpoints for hold-duration analysis
            horizons = {}
            for h_key, h_mins, _ in HOLD_DURATION_BUCKETS:
                t_min = ev["buy_min"] + timedelta(minutes=h_mins)
                px = (
                    _lookup_bar_near(con_bars, t_min)
                    if t_min <= ev["eval_end_dt"]
                    else None
                )
                if px is not None and entry_ref and entry_ref > 0:
                    ret_p = round(((px - entry_ref) / entry_ref) * 100.0, 2)
                    pnl_d = round((px - entry_ref) * 100.0, 2)
                    horizons[h_key] = {
                        "price": round(px, 4),
                        "ret_pct": ret_p,
                        "pnl_dollars": pnl_d,
                    }
                    if h_key in {"15m", "60m"}:
                        rank_stats[rk][f"ret_{h_key}"].append(ret_p)
                else:
                    horizons[h_key] = None

            # Core metric: Option Buy Time vs End of Session
            eod_px, eod_bar_dt, eod_source = _lookup_session_end_bar(
                con_bars,
                ev["buy_min"],
                ev["eval_end_dt"],
                sell_chain,
                con,
                ev["session_close_dt"],
            )
            session_ret_pct = None
            session_pnl_dollars = None
            session_price_diff = None
            if eod_px is not None and entry_ref and entry_ref > 0:
                session_price_diff = round(eod_px - entry_ref, 4)
                session_ret_pct = round(
                    ((eod_px - entry_ref) / entry_ref) * 100.0, 2
                )
                session_pnl_dollars = round((eod_px - entry_ref) * 100.0, 2)
                rank_stats[rk]["ret_session"].append(session_ret_pct)
                rank_stats[rk]["pnl_session"].append(session_pnl_dollars)

            # Peak & Max Drawdown between buy_min and eval_end_dt
            peak_px = None
            peak_dt = None
            trough_px = None
            trough_dt = None
            for bm, bval in sorted(con_bars.items()):
                if ev["buy_min"] < bm <= ev["eval_end_dt"]:
                    cand_h = bval.get("h") or bval.get("c")
                    cand_l = bval.get("l") or bval.get("c")
                    if cand_h and (peak_px is None or cand_h > peak_px):
                        peak_px = cand_h
                        peak_dt = bm
                    if cand_l and (trough_px is None or cand_l < trough_px):
                        trough_px = cand_l
                        trough_dt = bm

            peak_ret_pct = None
            max_dd_pct = None
            if peak_px is not None and entry_ref and entry_ref > 0:
                peak_ret_pct = round(((peak_px - entry_ref) / entry_ref) * 100.0, 2)
                rank_stats[rk]["ret_peak"].append(peak_ret_pct)
            if trough_px is not None and entry_ref and entry_ref > 0:
                max_dd_pct = round(((trough_px - entry_ref) / entry_ref) * 100.0, 2)

            peak_minutes = (
                max(0.0, round((peak_dt - ev["buy_min"]).total_seconds() / 60.0, 1))
                if peak_dt is not None
                else None
            )
            trough_minutes = (
                max(0.0, round((trough_dt - ev["buy_min"]).total_seconds() / 60.0, 1))
                if trough_dt is not None
                else None
            )

            if ask > 0:
                rank_stats[rk]["asks"].append(ask)
            if spread_pct is not None:
                rank_stats[rk]["spreads"].append(spread_pct)
            if delta_v is not None:
                rank_stats[rk]["deltas"].append(delta_v)

            candidates_out.append(
                {
                    "otm_rank": rk,
                    "label": _STRIKE_RANK_LABELS.get(rk, f"{rk:+d}"),
                    "contract": con,
                    "strike": round(float(c["strike"]), 4),
                    "bid": round(bid, 4),
                    "ask": round(ask, 4),
                    "mid": mid,
                    "spread_pct": spread_pct,
                    "delta": delta_v,
                    "iv": iv_v,
                    "open_interest": oi_v,
                    "entry_price": round(float(entry_ref), 4)
                    if entry_ref is not None
                    else None,
                    "entry_source": entry_source,
                    "session_end_price": round(float(eod_px), 4)
                    if eod_px is not None
                    else None,
                    "session_end_bar_utc": eod_bar_dt.isoformat()
                    if eod_bar_dt
                    else None,
                    "session_end_source": eod_source,
                    "session_price_diff": session_price_diff,
                    "session_return_pct": session_ret_pct,
                    "session_pnl_dollars": session_pnl_dollars,
                    "peak_price": round(float(peak_px), 4)
                    if peak_px is not None
                    else None,
                    "peak_return_pct": peak_ret_pct,
                    "peak_time_utc": peak_dt.isoformat() if peak_dt else None,
                    "peak_minutes": peak_minutes,
                    "trough_minutes": trough_minutes,
                    "max_drawdown_pct": max_dd_pct,
                    "is_current_pick": rk == 2,
                    "is_matched_contract": bool(matched_con and matched_con == con),
                    "horizons": horizons,
                }
            )

        # Rank candidates for this BUY event by Buy Time -> End of Session return %
        scored = [
            c for c in candidates_out if c.get("session_return_pct") is not None
        ]
        scored.sort(
            key=lambda c: (
                c["session_return_pct"],
                c["session_pnl_dollars"] or -1e9,
                -(c["spread_pct"] if c["spread_pct"] is not None else 999.0),
                -abs(c["otm_rank"] - 2),
            ),
            reverse=True,
        )
        ev_winner = scored[0] if scored else None
        ev_runner_up = scored[1] if len(scored) > 1 else None
        if ev_winner is not None and ev_winner["otm_rank"] in rank_stats:
            rank_stats[ev_winner["otm_rank"]]["winner_count"] += 1

        hold_to_close_mins = round(
            (ev["eval_end_dt"] - ev["option_buy_dt"]).total_seconds() / 60.0, 1
        )
        spot_chg_pct = (
            round(((ev["spot_eod"] - ev["spot_price"]) / ev["spot_price"]) * 100.0, 2)
            if (ev["spot_eod"] is not None and ev["spot_price"] > 0)
            else None
        )

        events_out.append(
            {
                "event_id": ev["event_id"],
                "filename": ev["filename"],
                "symbol": ev["symbol"],
                "right": ev["right"],
                "expiry": ev["expiry"],
                "rsi": round(float(ev["rsi"]), 2) if ev["rsi"] is not None else None,
                "signal_dt_utc": ev["signal_dt"].isoformat(),
                "option_buy_dt_utc": ev["option_buy_dt"].isoformat(),
                "option_buy_display": ev["option_buy_dt"]
                .astimezone(display_tz)
                .strftime("%Y-%m-%d %H:%M:%S %Z"),
                "buy_time_source": ev["buy_time_source"],
                "session_close_dt_utc": ev["session_close_dt"].isoformat(),
                "session_close_display": ev["session_close_dt"]
                .astimezone(display_tz)
                .strftime("%Y-%m-%d %H:%M:%S %Z"),
                "eval_end_dt_utc": ev["eval_end_dt"].isoformat(),
                "eval_end_display": ev["eval_end_dt"]
                .astimezone(display_tz)
                .strftime("%Y-%m-%d %H:%M:%S %Z"),
                "session_in_progress": ev["session_in_progress"],
                "hold_to_close_minutes": hold_to_close_mins,
                "spot_price": ev["spot_price"],
                "spot_source": ev["spot_source"],
                "spot_eod": ev["spot_eod"],
                "spot_change_pct": spot_chg_pct,
                "kinematics": ev["kinematics"],
                "matched_trade": tr,
                "winner": ev_winner,
                "runner_up": ev_runner_up,
                "candidates": candidates_out,
            }
        )

    # Aggregate scoreboard by strike rank
    def _avg_or_none(vals, dec=2):
        return round(sum(vals) / len(vals), dec) if vals else None

    by_rank_out = []
    best_rank_session = None
    for rk in range(-2, 5):
        st = rank_stats[rk]
        cnt = max(len(st["asks"]), len(st["ret_session"]), len(st["ret_15m"]))
        if cnt == 0:
            continue
        avg_sess = _avg_or_none(st["ret_session"])
        wins_sess = sum(1 for v in st["ret_session"] if v > 0)
        win_rate_sess = (
            round((wins_sess / len(st["ret_session"])) * 100.0, 1)
            if st["ret_session"]
            else None
        )
        row = {
            "otm_rank": rk,
            "label": st["label"],
            "count": cnt,
            "avg_entry_ask": _avg_or_none(st["asks"]),
            "avg_spread_pct": _avg_or_none(st["spreads"], 1),
            "avg_delta": _avg_or_none(st["deltas"], 2),
            "avg_ret_15m": _avg_or_none(st["ret_15m"]),
            "avg_ret_60m": _avg_or_none(st["ret_60m"]),
            "avg_ret_session": avg_sess,
            "avg_pnl_session": _avg_or_none(st["pnl_session"]),
            "avg_ret_peak": _avg_or_none(st["ret_peak"]),
            "win_rate_session": win_rate_sess,
            "winner_count": st["winner_count"],
        }
        by_rank_out.append(row)
        if avg_sess is not None and (
            best_rank_session is None or avg_sess > best_rank_session["avg_ret"]
        ):
            best_rank_session = {
                "otm_rank": rk,
                "label": st["label"],
                "avg_ret": avg_sess,
            }

    # Select ONE winning contract per symbol across all analyzed BUY events
    events_by_sym: dict[str, list[dict]] = {}
    for ev in events_out:
        events_by_sym.setdefault(ev["symbol"], []).append(ev)

    winners_by_symbol = []
    for sym in sorted(events_by_sym.keys()):
        sym_events = events_by_sym[sym]
        best_pair = None
        for ev in sym_events:
            for c in ev["candidates"]:
                if c.get("session_return_pct") is None:
                    continue
                score = (
                    c["session_return_pct"],
                    c["session_pnl_dollars"] or -1e9,
                    -(c["spread_pct"] if c["spread_pct"] is not None else 999.0),
                    -abs(c["otm_rank"] - 2),
                )
                if best_pair is None or score > best_pair[0]:
                    best_pair = (score, ev, c)

        if best_pair is None:
            latest_ev = sym_events[0]
            winners_by_symbol.append(
                {
                    "symbol": sym,
                    "signals_analyzed": len(sym_events),
                    "has_winner": False,
                    "reason_unavailable": "No option bars or session-end prices available for candidate strikes.",
                    "event": latest_ev,
                    "winner": None,
                    "runner_up": None,
                    "default_plus2": None,
                    "reasons": [
                        f"Analyzed {len(sym_events)} BUY signal(s), but Alpaca 1m option bars/session-end quotes were unavailable."
                    ],
                }
            )
            continue

        _, win_ev, win_c = best_pair
        # Runner-up among other candidates in the winning event (or across symbol events)
        same_ev_others = [
            c
            for c in win_ev["candidates"]
            if c["contract"] != win_c["contract"]
            and c.get("session_return_pct") is not None
        ]
        same_ev_others.sort(
            key=lambda c: (
                c["session_return_pct"],
                c["session_pnl_dollars"] or -1e9,
            ),
            reverse=True,
        )
        runner_up = same_ev_others[0] if same_ev_others else None
        plus2_cand = next(
            (c for c in win_ev["candidates"] if c["otm_rank"] == 2), None
        )

        # Build detailed human-readable reasons explaining why this contract won
        reasons = []
        s_ret = win_c["session_return_pct"]
        s_pnl = win_c["session_pnl_dollars"]
        reasons.append(
            f"Highest Buy-Time-to-Session-End return for {sym}: "
            f"{s_ret:+.2f}% ({_fmt_dollars(s_pnl, 2)} per 1 contract) "
            f"from ${win_c['entry_price']:.2f} ({win_c['entry_source']}) at buy time "
            f"to ${win_c['session_end_price']:.2f} ({win_c['session_end_source']}) at session end."
        )
        reasons.append(
            f"Ranked #1 of {len(same_ev_others) + 1} scored strikes (-2 ITM to +4 OTM) "
            f"in {win_ev['option_buy_display']} signal"
            + (
                f" (best across {len(sym_events)} BUY signals for {sym})."
                if len(sym_events) > 1
                else "."
            )
        )
        if runner_up is not None:
            margin_pct = round(s_ret - runner_up["session_return_pct"], 2)
            margin_usd = round(
                (s_pnl or 0.0) - (runner_up["session_pnl_dollars"] or 0.0), 2
            )
            reasons.append(
                f"Beat #2 runner-up {runner_up['contract']} ({runner_up['label']}, "
                f"{runner_up['session_return_pct']:+.2f}%) by {margin_pct:+.2f} percentage points "
                f"({_fmt_dollars(margin_usd, 2)}/contract)."
            )
        if plus2_cand is not None:
            if plus2_cand["contract"] == win_c["contract"]:
                reasons.append(
                    "Confirmed the default +2 OTM selection rule was optimal at session close."
                )
            elif plus2_cand.get("session_return_pct") is not None:
                diff_p2 = round(s_ret - plus2_cand["session_return_pct"], 2)
                reasons.append(
                    f"Outperformed default +2 OTM ({plus2_cand['contract']}, "
                    f"{plus2_cand['session_return_pct']:+.2f}%) by {diff_p2:+.2f} percentage points."
                )
        if win_c.get("peak_return_pct") is not None:
            pk_t = (
                _opt_parse_utc(win_c["peak_time_utc"])
                .astimezone(display_tz)
                .strftime("%H:%M %Z")
                if win_c.get("peak_time_utc")
                else "n/a"
            )
            dd_txt = (
                f", max drawdown {win_c['max_drawdown_pct']:+.2f}%"
                if win_c.get("max_drawdown_pct") is not None
                else ""
            )
            reasons.append(
                f"Intraday trajectory reached a session peak of ${win_c['peak_price']:.2f} "
                f"({win_c['peak_return_pct']:+.2f}% at {pk_t}){dd_txt}."
            )
        if win_ev.get("spot_eod") is not None and win_ev.get("spot_change_pct") is not None:
            reasons.append(
                f"Underlying {sym} moved from ${win_ev['spot_price']:.2f} at buy time "
                f"to ${win_ev['spot_eod']:.2f} at session end ({win_ev['spot_change_pct']:+.2f}%), "
                f"giving this {win_c['label']} (${win_c['strike']:.2f} {win_ev['right']}) strike strong leverage."
            )
        meta_bits = []
        if win_c.get("delta") is not None:
            meta_bits.append(f"Delta {win_c['delta']:.2f}")
        if win_c.get("iv") is not None:
            meta_bits.append(f"IV {win_c['iv'] * 100.0:.1f}%")
        if win_c.get("spread_pct") is not None:
            meta_bits.append(f"Entry Spread {win_c['spread_pct']:.1f}%")
        if win_c.get("open_interest") is not None:
            meta_bits.append(f"OI {win_c['open_interest']}")
        if win_ev.get("rsi") is not None:
            meta_bits.append(f"Buy RSI {win_ev['rsi']:.2f}")
        if (win_ev.get("kinematics") or {}).get("regime"):
            meta_bits.append(f"Regime {win_ev['kinematics']['regime']}")
        if meta_bits:
            reasons.append("Entry profile: " + " | ".join(meta_bits) + ".")

        winners_by_symbol.append(
            {
                "symbol": sym,
                "signals_analyzed": len(sym_events),
                "has_winner": True,
                "event": win_ev,
                "winner": win_c,
                "runner_up": runner_up,
                "default_plus2": plus2_cand,
                "reasons": reasons,
            }
        )

    overall_strategy = _compute_overall_strategy_summary(
        events_out=events_out,
        winners_by_symbol=winners_by_symbol,
        by_rank_out=by_rank_out,
        best_rank_session=best_rank_session,
    )

    return {
        "log_root": str(log_root),
        "chain_dir": str(chain_dir),
        "symbol_filter": sym_q,
        "date_filter": date_q,
        "bars_available": bars_available,
        "bars_note": bars_note,
        "summary": {
            "total_signals": len(events_out),
            "unique_symbols": len(winners_by_symbol),
            "executed_signals": executed_count,
            "by_rank": by_rank_out,
            "best_rank_session": best_rank_session,
            "overall_strategy": overall_strategy,
        },
        "events": events_out,
        "winners_by_symbol": winners_by_symbol,
    }


def _summarize_peak_timing(cands: list[dict]) -> dict | None:
    mins = [c["peak_minutes"] for c in cands if c.get("peak_minutes") is not None]
    if not mins:
        return None
    pk_rets = [c["peak_return_pct"] for c in cands if c.get("peak_return_pct") is not None]
    eod_rets = [c["session_return_pct"] for c in cands if c.get("session_return_pct") is not None]
    givebacks = [
        round(c["peak_return_pct"] - c["session_return_pct"], 2)
        for c in cands
        if c.get("peak_return_pct") is not None and c.get("session_return_pct") is not None
    ]
    return {
        "count": len(mins),
        "avg_minutes": _avg_or_none(mins, 1),
        "median_minutes": _median_or_none(mins, 1),
        "p25_minutes": _quantile_or_none(mins, 0.25, 1),
        "p75_minutes": _quantile_or_none(mins, 0.75, 1),
        "min_minutes": round(min(mins), 1),
        "max_minutes": round(max(mins), 1),
        "avg_peak_ret_pct": _avg_or_none(pk_rets, 2),
        "avg_eod_ret_pct": _avg_or_none(eod_rets, 2),
        "avg_giveback_pct": _avg_or_none(givebacks, 2),
    }


def _compute_overall_strategy_summary(
    events_out: list[dict],
    winners_by_symbol: list[dict],
    by_rank_out: list[dict],
    best_rank_session: dict | None,
) -> dict:
    """Compute cross-signal, cross-symbol answers to:
    1) Did we buy the correct contract?
    2) What is the optimal duration to hold before selling?
    """
    scored_events = [ev for ev in events_out if ev.get("winner") is not None]

    # Identify each event's effective bought contract (matched executed trade if present, else +2 OTM default)
    effective_pairs: list[tuple[dict, dict, str]] = []
    executed_pairs: list[tuple[dict, dict | None, dict]] = []
    plus2_pairs: list[tuple[dict, dict]] = []
    winner_cands: list[dict] = []
    all_scored_cands: list[dict] = []

    rank_win_counts: dict[int, tuple[str, int]] = {}

    for ev in scored_events:
        w = ev["winner"]
        winner_cands.append(w)
        w_rk = w["otm_rank"]
        prev_label, prev_cnt = rank_win_counts.get(w_rk, (w["label"], 0))
        rank_win_counts[w_rk] = (prev_label, prev_cnt + 1)

        cands = ev.get("candidates") or []
        for c in cands:
            if c.get("session_return_pct") is not None:
                all_scored_cands.append(c)

        matched_cand = next(
            (c for c in cands if c.get("is_matched_contract") and c.get("session_return_pct") is not None),
            None,
        )
        plus2_cand = next(
            (c for c in cands if c.get("otm_rank") == 2 and c.get("session_return_pct") is not None),
            None,
        )
        if plus2_cand is not None:
            plus2_pairs.append((ev, plus2_cand))

        if ev.get("matched_trade") is not None:
            executed_pairs.append((ev, matched_cand, ev["matched_trade"]))

        if matched_cand is not None:
            effective_pairs.append((ev, matched_cand, "executed"))
        elif plus2_cand is not None:
            effective_pairs.append((ev, plus2_cand, "+2_rule"))

    # --- 1. Contract Selection ("Did we buy the correct contract?") ---
    exec_scored = [(ev, mc, tr) for ev, mc, tr in executed_pairs if mc is not None]
    exec_was_winner = sum(1 for ev, mc, _ in exec_scored if mc["contract"] == ev["winner"]["contract"])
    exec_closed = [tr for _, _, tr in executed_pairs if tr.get("status") == "closed"]
    exec_open = [tr for _, _, tr in executed_pairs if tr.get("status") != "closed"]
    exec_realized_rets = [tr["total_pnl_pct"] for tr in exec_closed if tr.get("total_pnl_pct") is not None]
    exec_realized_pnls = [tr["total_pnl_dollars"] for tr in exec_closed if tr.get("total_pnl_dollars") is not None]
    exec_eod_rets = [mc["session_return_pct"] for _, mc, _ in exec_scored]
    exec_eod_pnls = [mc["session_pnl_dollars"] for _, mc, _ in exec_scored if mc.get("session_pnl_dollars") is not None]
    exec_win_eod_rets = [ev["winner"]["session_return_pct"] for ev, _, _ in exec_scored]
    exec_win_eod_pnls = [
        ev["winner"]["session_pnl_dollars"]
        for ev, _, _ in exec_scored
        if ev["winner"].get("session_pnl_dollars") is not None
    ]

    eff_eod_rets = [bc["session_return_pct"] for _, bc, _ in effective_pairs]
    eff_eod_pnls = [bc["session_pnl_dollars"] for _, bc, _ in effective_pairs if bc.get("session_pnl_dollars") is not None]
    eff_peak_rets = [bc["peak_return_pct"] for _, bc, _ in effective_pairs if bc.get("peak_return_pct") is not None]
    eff_win_eod_rets = [ev["winner"]["session_return_pct"] for ev, _, _ in effective_pairs]
    eff_win_eod_pnls = [
        ev["winner"]["session_pnl_dollars"]
        for ev, _, _ in effective_pairs
        if ev["winner"].get("session_pnl_dollars") is not None
    ]
    eff_was_winner = sum(1 for ev, bc, _ in effective_pairs if bc["contract"] == ev["winner"]["contract"])
    eff_pos_eod = sum(1 for r in eff_eod_rets if r > 0)
    eff_exec_src_cnt = sum(1 for _, _, src in effective_pairs if src == "executed")
    eff_rule_src_cnt = sum(1 for _, _, src in effective_pairs if src == "+2_rule")

    plus2_was_winner = sum(1 for ev, pc in plus2_pairs if pc["contract"] == ev["winner"]["contract"])
    plus2_eod_rets = [pc["session_return_pct"] for _, pc in plus2_pairs]
    plus2_eod_pnls = [pc["session_pnl_dollars"] for _, pc in plus2_pairs if pc.get("session_pnl_dollars") is not None]

    # Symbol-level match: on each unique symbol's winning event, was the bought/+2 OTM contract the symbol winner?
    sym_scored = [item for item in winners_by_symbol if item.get("has_winner") and item.get("winner")]
    sym_bought_best = 0
    for item in sym_scored:
        ev = item["event"]
        win_con = item["winner"]["contract"]
        matched_or_p2 = next(
            (c for c in ev["candidates"] if c.get("is_matched_contract")),
            item.get("default_plus2"),
        )
        if matched_or_p2 and matched_or_p2.get("contract") == win_con:
            sym_bought_best += 1

    eff_avg_ret = _avg_or_none(eff_eod_rets, 2)
    eff_win_avg_ret = _avg_or_none(eff_win_eod_rets, 2)
    eff_avg_pnl = _avg_or_none(eff_eod_pnls, 2)
    eff_win_avg_pnl = _avg_or_none(eff_win_eod_pnls, 2)
    eff_total_pnl = round(sum(eff_eod_pnls), 2) if eff_eod_pnls else None
    eff_win_total_pnl = round(sum(eff_win_eod_pnls), 2) if eff_win_eod_pnls else None

    missed_ret_gap = (
        round(eff_win_avg_ret - eff_avg_ret, 2)
        if (eff_win_avg_ret is not None and eff_avg_ret is not None)
        else None
    )
    missed_pnl_gap = (
        round(eff_win_avg_pnl - eff_avg_pnl, 2)
        if (eff_win_avg_pnl is not None and eff_avg_pnl is not None)
        else None
    )
    missed_total_pnl_gap = (
        round(eff_win_total_pnl - eff_total_pnl, 2)
        if (eff_win_total_pnl is not None and eff_total_pnl is not None)
        else None
    )

    winning_rank_dist = sorted(
        [
            {
                "otm_rank": rk,
                "label": lbl,
                "wins": cnt,
                "share_pct": round((cnt / len(scored_events)) * 100.0, 1) if scored_events else 0.0,
            }
            for rk, (lbl, cnt) in rank_win_counts.items()
        ],
        key=lambda row: (row["wins"], -abs(row["otm_rank"] - 2)),
        reverse=True,
    )

    eff_match_pct = (
        round((eff_was_winner / len(effective_pairs)) * 100.0, 1)
        if effective_pairs
        else None
    )
    exec_match_pct = (
        round((exec_was_winner / len(exec_scored)) * 100.0, 1)
        if exec_scored
        else None
    )
    sym_match_pct = (
        round((sym_bought_best / len(sym_scored)) * 100.0, 1)
        if sym_scored
        else None
    )

    # Verdict logic
    if not effective_pairs:
        verdict_headline = "INSUFFICIENT DATA — No scored BUY signals with bought/+2 OTM option bars."
        verdict_details = []
    else:
        basis_txt = (
            "Executed contracts"
            if eff_rule_src_cnt == 0
            else (
                "Default +2 OTM contracts"
                if eff_exec_src_cnt == 0
                else f"Bought contracts ({eff_exec_src_cnt} executed + {eff_rule_src_cnt} +2 OTM baseline)"
            )
        )
        if eff_match_pct is not None and eff_match_pct >= 50.0 and (
            best_rank_session is None
            or best_rank_session["otm_rank"] == 2
            or (missed_ret_gap is not None and missed_ret_gap <= 5.0)
        ):
            verdict_headline = (
                f"YES (Mostly Optimal) — {basis_txt} won #1 at EOD in "
                f"{eff_was_winner}/{len(effective_pairs)} signals ({eff_match_pct:.1f}%)."
            )
        else:
            best_lbl = best_rank_session["label"] if best_rank_session else (winning_rank_dist[0]["label"] if winning_rank_dist else "other strikes")
            verdict_headline = (
                f"NO (Sub-Optimal Strike Selection) — {basis_txt} ranked #1 in only "
                f"{eff_was_winner}/{len(effective_pairs)} signals ({eff_match_pct:.1f}%), "
                f"while {best_lbl} delivered the highest average EOD return."
            )

        verdict_details = [
            f"Across {len(effective_pairs)} scored BUY signals ({len(sym_scored)} symbols): bought/+2 OTM averaged "
            f"{_fmt_pct(eff_avg_ret, 2)} ({_fmt_dollars(eff_avg_pnl, 2)}/ctr) at EOD vs optimal winners at "
            f"{_fmt_pct(eff_win_avg_ret, 2)} ({_fmt_dollars(eff_win_avg_pnl, 2)}/ctr) — leaving "
            f"{_fmt_pct(missed_ret_gap, 2)} ({_fmt_dollars(missed_pnl_gap, 2)}/ctr) on the table per signal."
        ]
        if best_rank_session:
            verdict_details.append(
                f"Highest-returning strike rank across all data/symbols was {best_rank_session['label']} "
                f"({_fmt_pct(best_rank_session['avg_ret'], 2)} avg EOD return)."
            )
        if winning_rank_dist:
            top_w = winning_rank_dist[0]
            verdict_details.append(
                f"Most frequent #1 signal winner: {top_w['label']} "
                f"({top_w['wins']}/{len(scored_events)} signals, {top_w['share_pct']:.1f}%)."
            )

    contract_selection = {
        "executed_trades_count": len(executed_pairs),
        "executed_scored_count": len(exec_scored),
        "executed_closed_count": len(exec_closed),
        "executed_open_count": len(exec_open),
        "executed_was_winner_count": exec_was_winner,
        "executed_match_rate_pct": exec_match_pct,
        "executed_avg_realized_ret_pct": _avg_or_none(exec_realized_rets, 2),
        "executed_avg_realized_pnl_dollars": _avg_or_none(exec_realized_pnls, 2),
        "executed_avg_eod_ret_pct": _avg_or_none(exec_eod_rets, 2),
        "executed_avg_eod_pnl_dollars": _avg_or_none(exec_eod_pnls, 2),
        "executed_winner_avg_eod_ret_pct": _avg_or_none(exec_win_eod_rets, 2),
        "executed_winner_avg_eod_pnl_dollars": _avg_or_none(exec_win_eod_pnls, 2),
        "effective_bought_count": len(effective_pairs),
        "effective_executed_source_count": eff_exec_src_cnt,
        "effective_plus2_source_count": eff_rule_src_cnt,
        "effective_was_winner_count": eff_was_winner,
        "effective_match_rate_pct": eff_match_pct,
        "effective_positive_eod_count": eff_pos_eod,
        "effective_positive_eod_rate_pct": (
            round((eff_pos_eod / len(eff_eod_rets)) * 100.0, 1) if eff_eod_rets else None
        ),
        "effective_avg_eod_ret_pct": eff_avg_ret,
        "effective_med_eod_ret_pct": _median_or_none(eff_eod_rets, 2),
        "effective_avg_eod_pnl_dollars": eff_avg_pnl,
        "effective_total_eod_pnl_dollars": eff_total_pnl,
        "effective_avg_peak_ret_pct": _avg_or_none(eff_peak_rets, 2),
        "optimal_winner_avg_eod_ret_pct": eff_win_avg_ret,
        "optimal_winner_avg_eod_pnl_dollars": eff_win_avg_pnl,
        "optimal_winner_total_eod_pnl_dollars": eff_win_total_pnl,
        "missed_eod_ret_gap_pct": missed_ret_gap,
        "missed_eod_pnl_gap_dollars": missed_pnl_gap,
        "missed_eod_total_pnl_gap_dollars": missed_total_pnl_gap,
        "plus2_scored_count": len(plus2_pairs),
        "plus2_was_winner_count": plus2_was_winner,
        "plus2_match_rate_pct": (
            round((plus2_was_winner / len(plus2_pairs)) * 100.0, 1) if plus2_pairs else None
        ),
        "plus2_avg_eod_ret_pct": _avg_or_none(plus2_eod_rets, 2),
        "plus2_avg_eod_pnl_dollars": _avg_or_none(plus2_eod_pnls, 2),
        "symbol_scored_count": len(sym_scored),
        "symbol_bought_was_best_count": sym_bought_best,
        "symbol_match_rate_pct": sym_match_pct,
        "winning_rank_distribution": winning_rank_dist,
        "best_rank_session": best_rank_session,
        "verdict_headline": verdict_headline,
        "verdict_details": verdict_details,
    }

    # --- 2. Hold Duration Sweep ("What is the optimal duration to hold before selling?") ---
    bought_cands = [bc for _, bc, _ in effective_pairs]

    def _horizon_stats(cands: list[dict], h_key: str | None) -> dict:
        exact_rets, exact_pnls = [], []
        capped_rets, capped_pnls = [], []
        for c in cands:
            if h_key is None:
                sr = c.get("session_return_pct")
                sp = c.get("session_pnl_dollars")
                if sr is not None:
                    exact_rets.append(sr)
                    capped_rets.append(sr)
                if sp is not None:
                    exact_pnls.append(sp)
                    capped_pnls.append(sp)
            else:
                h_obj = (c.get("horizons") or {}).get(h_key)
                if h_obj and h_obj.get("ret_pct") is not None:
                    exact_rets.append(h_obj["ret_pct"])
                    capped_rets.append(h_obj["ret_pct"])
                    if h_obj.get("pnl_dollars") is not None:
                        exact_pnls.append(h_obj["pnl_dollars"])
                        capped_pnls.append(h_obj["pnl_dollars"])
                elif c.get("session_return_pct") is not None:
                    # If session ends before h_mins, a "hold Xm or EOD" rule exits at EOD
                    capped_rets.append(c["session_return_pct"])
                    if c.get("session_pnl_dollars") is not None:
                        capped_pnls.append(c["session_pnl_dollars"])

        exact_wins = sum(1 for r in exact_rets if r > 0)
        cap_wins = sum(1 for r in capped_rets if r > 0)
        return {
            "exact_n": len(exact_rets),
            "exact_avg_ret": _avg_or_none(exact_rets, 2),
            "exact_med_ret": _median_or_none(exact_rets, 2),
            "exact_win_rate": round((exact_wins / len(exact_rets)) * 100.0, 1) if exact_rets else None,
            "exact_avg_pnl": _avg_or_none(exact_pnls, 2),
            "capped_n": len(capped_rets),
            "capped_avg_ret": _avg_or_none(capped_rets, 2),
            "capped_med_ret": _median_or_none(capped_rets, 2),
            "capped_win_rate": round((cap_wins / len(capped_rets)) * 100.0, 1) if capped_rets else None,
            "capped_avg_pnl": _avg_or_none(capped_pnls, 2),
            "capped_total_pnl": round(sum(capped_pnls), 2) if capped_pnls else None,
        }

    duration_specs = [(k, m, f"Hold {lbl}") for k, m, lbl in HOLD_DURATION_BUCKETS] + [
        ("EOD", None, "Hold to EOD")
    ]
    duration_buckets = []
    for key, mins, label in duration_specs:
        b_st = _horizon_stats(bought_cands, None if key == "EOD" else key)
        w_st = _horizon_stats(winner_cands, None if key == "EOD" else key)
        a_st = _horizon_stats(all_scored_cands, None if key == "EOD" else key)
        duration_buckets.append(
            {
                "key": key,
                "minutes": mins,
                "label": label,
                "bought": b_st,
                "winner": w_st,
                "all": a_st,
            }
        )

    def _pick_best_bucket(cohort_key: str) -> dict | None:
        valid = [
            b
            for b in duration_buckets
            if b[cohort_key]["capped_avg_ret"] is not None and b[cohort_key]["capped_n"] > 0
        ]
        if not valid:
            return None
        # Prefer highest capped average return (apples-to-apples across all signals),
        # tie-breaking on capped win rate, exact sample coverage, and shorter duration.
        best = max(
            valid,
            key=lambda b: (
                b[cohort_key]["capped_avg_ret"],
                b[cohort_key]["capped_win_rate"] or -1.0,
                b[cohort_key]["capped_avg_pnl"] or -1e9,
                b[cohort_key]["exact_n"],
                -(b["minutes"] if b["minutes"] is not None else 10_000),
            ),
        )
        return {
            "key": best["key"],
            "minutes": best["minutes"],
            "label": best["label"],
            **best[cohort_key],
        }

    opt_bought = _pick_best_bucket("bought")
    opt_winner = _pick_best_bucket("winner")
    opt_all = _pick_best_bucket("all")

    bought_peak = _summarize_peak_timing(bought_cands)
    winner_peak = _summarize_peak_timing(winner_cands)
    all_peak = _summarize_peak_timing(all_scored_cands)

    exec_hold_vals = [
        tr["hold_minutes"]
        for tr in exec_closed
        if tr.get("hold_minutes") is not None
    ]
    actual_hold_summary = (
        {
            "count": len(exec_hold_vals),
            "avg_hold_minutes": _avg_or_none(exec_hold_vals, 1),
            "median_hold_minutes": _median_or_none(exec_hold_vals, 1),
            "min_hold_minutes": round(min(exec_hold_vals), 1),
            "max_hold_minutes": round(max(exec_hold_vals), 1),
            "avg_realized_ret_pct": _avg_or_none(exec_realized_rets, 2),
            "avg_realized_pnl_dollars": _avg_or_none(exec_realized_pnls, 2),
        }
        if exec_hold_vals
        else None
    )

    duration_rec = []
    if opt_bought is not None:
        eod_b = next((b for b in duration_buckets if b["key"] == "EOD"), None)
        eod_ret_b = eod_b["bought"]["capped_avg_ret"] if eod_b else None
        diff_vs_eod = (
            round(opt_bought["capped_avg_ret"] - eod_ret_b, 2)
            if eod_ret_b is not None
            else None
        )
        if opt_bought["key"] == "EOD":
            duration_rec.append(
                f"Best hold duration for bought/+2 OTM contracts across all data & symbols is "
                f"Hold to EOD ({_fmt_pct(opt_bought['capped_avg_ret'], 2)} avg return, "
                f"{opt_bought['capped_win_rate']:.1f}% win rate, {_fmt_dollars(opt_bought['capped_avg_pnl'], 2)}/ctr)."
            )
        else:
            duration_rec.append(
                f"Optimal hold duration for bought/+2 OTM contracts across all data & symbols is "
                f"{opt_bought['minutes']} minutes (or EOD if shorter): "
                f"{_fmt_pct(opt_bought['capped_avg_ret'], 2)} avg return, "
                f"{opt_bought['capped_win_rate']:.1f}% win rate, "
                f"{_fmt_dollars(opt_bought['capped_avg_pnl'], 2)}/ctr"
                + (
                    f" — beating Hold-to-EOD ({_fmt_pct(eod_ret_b, 2)}) by {_fmt_pct(diff_vs_eod, 2)}."
                    if diff_vs_eod is not None
                    else "."
                )
            )
    if opt_winner is not None and (
        opt_bought is None or opt_winner["key"] != opt_bought["key"] or opt_winner["capped_avg_ret"] != opt_bought["capped_avg_ret"]
    ):
        duration_rec.append(
            f"For winning strikes (#1 EOD winners), the best checkpoint is {opt_winner['label']} "
            f"({_fmt_pct(opt_winner['capped_avg_ret'], 2)} avg return, "
            f"{opt_winner['capped_win_rate']:.1f}% win rate)."
        )
    if bought_peak is not None:
        duration_rec.append(
            f"Bought/+2 OTM contracts hit their intraday peak at a median of "
            f"{bought_peak['median_minutes']:.0f}m (avg {bought_peak['avg_minutes']:.0f}m, "
            f"IQR {bought_peak['p25_minutes']:.0f}m–{bought_peak['p75_minutes']:.0f}m) "
            f"with an avg peak of {_fmt_pct(bought_peak['avg_peak_ret_pct'], 2)}"
            + (
                f", giving back {bought_peak['avg_giveback_pct']:.2f} pct pts by EOD "
                f"({_fmt_pct(bought_peak['avg_eod_ret_pct'], 2)})."
                if (bought_peak.get("avg_giveback_pct") is not None and bought_peak["avg_giveback_pct"] > 0)
                else "."
            )
        )
    if actual_hold_summary is not None and opt_bought is not None:
        duration_rec.append(
            f"Actual closed trades were held an avg of {actual_hold_summary['avg_hold_minutes']:.1f}m "
            f"(median {actual_hold_summary['median_hold_minutes']:.1f}m) for "
            f"{_fmt_pct(actual_hold_summary['avg_realized_ret_pct'], 2)} "
            f"({_fmt_dollars(actual_hold_summary['avg_realized_pnl_dollars'], 2)}/ctr), vs optimal "
            f"{opt_bought['label']} at {_fmt_pct(opt_bought['capped_avg_ret'], 2)}."
        )

    hold_duration = {
        "duration_buckets": duration_buckets,
        "optimal_bought_duration": opt_bought,
        "optimal_winner_duration": opt_winner,
        "optimal_all_duration": opt_all,
        "bought_peak_timing": bought_peak,
        "winner_peak_timing": winner_peak,
        "all_peak_timing": all_peak,
        "executed_actual_hold": actual_hold_summary,
        "recommendations": duration_rec,
    }

    return {
        "contract_selection": contract_selection,
        "hold_duration": hold_duration,
    }


def _fmt_opt(val, decimals=2, prefix="", suffix="") -> str:
    if val is None:
        return "--"
    return f"{prefix}{float(val):.{decimals}f}{suffix}"


def _fmt_pct(val, decimals=2) -> str:
    if val is None:
        return "--"
    n = float(val)
    return f"{n:+.{decimals}f}%"


def _fmt_dollars(val, decimals=2) -> str:
    if val is None:
        return "--"
    n = float(val)
    sign = "+" if n >= 0 else "-"
    return f"{sign}${abs(n):.{decimals}f}"


def format_overall_strategy_summary(
    analysis: dict, display_tz: ZoneInfo | None = None, tz_label: str = ""
) -> str:
    """Format the overall cross-symbol strategy summary answering:
    1) Did we buy the correct contract?
    2) What is the optimal duration to hold before selling across all data/symbols?
    """
    summary = analysis.get("summary") or {}
    overall = summary.get("overall_strategy") or {}
    cs = overall.get("contract_selection") or {}
    hd = overall.get("hold_duration") or {}

    lines = []
    sep = "=" * 96
    sub = "-" * 96
    lines.append(sep)
    lines.append(
        "  OVERALL STRATEGY SUMMARY  (Did We Buy the Right Contract & Optimal Hold Duration?)"
    )
    lines.append(sep)
    lines.append(
        f"  Scope : {summary.get('total_signals', 0)} BUY signals across "
        f"{summary.get('unique_symbols', 0)} symbols "
        f"({summary.get('executed_signals', 0)} matched executed trades)"
    )

    if not overall or cs.get("effective_bought_count", 0) == 0:
        lines.append(
            "  No scored BUY signals with option bar/session-end data are available yet "
            "to evaluate contract accuracy or hold durations."
        )
        lines.append(sep)
        return "\n".join(lines)

    # Part A: Did we buy the correct contract?
    lines.append("")
    lines.append("  1. DID WE BUY THE CORRECT CONTRACT?")
    lines.append("  " + "-" * 92)
    lines.append(f"  Verdict : {cs.get('verdict_headline')}")
    for det in cs.get("verdict_details") or []:
        lines.append(f"            {det}")

    lines.append("")
    lines.append(
        f"  {'COHORT / COMPARISON':<38} {'COUNT':>6} {'#1 WINS':>9} {'WIN%':>7} "
        f"{'AVG EOD':>9} {'AVG PNL':>10} {'TOT PNL':>11}"
    )
    lines.append("  " + "-" * 92)

    if cs.get("executed_trades_count", 0) > 0:
        ex_win_txt = (
            f"{cs['executed_was_winner_count']}/{cs['executed_scored_count']}"
            if cs.get("executed_scored_count")
            else "--"
        )
        ex_m_pct = (
            f"{cs['executed_match_rate_pct']:.1f}%"
            if cs.get("executed_match_rate_pct") is not None
            else "--"
        )
        if cs.get("executed_closed_count", 0) > 0:
            lines.append(
                f"  {'Executed Trades (Actual Exit)':<38} {cs['executed_closed_count']:>6} {'--':>9} {'--':>7} "
                f"{_fmt_pct(cs.get('executed_avg_realized_ret_pct'), 2):>9} "
                f"{_fmt_dollars(cs.get('executed_avg_realized_pnl_dollars'), 2):>10} {'--':>11}"
            )
        lines.append(
            f"  {'Executed Contracts (Held to EOD)':<38} {cs['executed_scored_count']:>6} {ex_win_txt:>9} {ex_m_pct:>7} "
            f"{_fmt_pct(cs.get('executed_avg_eod_ret_pct'), 2):>9} "
            f"{_fmt_dollars(cs.get('executed_avg_eod_pnl_dollars'), 2):>10} {'--':>11}"
        )

    eff_cnt = cs.get("effective_bought_count", 0)
    eff_win_txt = f"{cs.get('effective_was_winner_count', 0)}/{eff_cnt}"
    eff_m_pct = (
        f"{cs['effective_match_rate_pct']:.1f}%"
        if cs.get("effective_match_rate_pct") is not None
        else "--"
    )
    lines.append(
        f"  {'All Signals: Bought / +2 OTM Baseline':<38} {eff_cnt:>6} {eff_win_txt:>9} {eff_m_pct:>7} "
        f"{_fmt_pct(cs.get('effective_avg_eod_ret_pct'), 2):>9} "
        f"{_fmt_dollars(cs.get('effective_avg_eod_pnl_dollars'), 2):>10} "
        f"{_fmt_dollars(cs.get('effective_total_eod_pnl_dollars'), 2):>11}"
    )
    lines.append(
        f"  {'All Signals: #1 Optimal Winner Strike':<38} {eff_cnt:>6} {f'{eff_cnt}/{eff_cnt}':>9} {'100.0%':>7} "
        f"{_fmt_pct(cs.get('optimal_winner_avg_eod_ret_pct'), 2):>9} "
        f"{_fmt_dollars(cs.get('optimal_winner_avg_eod_pnl_dollars'), 2):>10} "
        f"{_fmt_dollars(cs.get('optimal_winner_total_eod_pnl_dollars'), 2):>11}"
    )
    lines.append(
        f"  {'Missed Upside (Optimal - Bought)':<38} {eff_cnt:>6} {'--':>9} {'--':>7} "
        f"{_fmt_pct(cs.get('missed_eod_ret_gap_pct'), 2):>9} "
        f"{_fmt_dollars(cs.get('missed_eod_pnl_gap_dollars'), 2):>10} "
        f"{_fmt_dollars(cs.get('missed_eod_total_pnl_gap_dollars'), 2):>11}"
    )

    sym_sc = cs.get("symbol_scored_count", 0)
    if sym_sc > 0:
        lines.append(
            f"  Symbol Match Rate : {cs.get('symbol_bought_was_best_count', 0)}/{sym_sc} symbols "
            f"({cs.get('symbol_match_rate_pct', 0.0):.1f}%) had the bought/+2 OTM contract finish #1 overall."
        )
    if cs.get("effective_positive_eod_rate_pct") is not None:
        lines.append(
            f"  Profitable at EOD : {cs.get('effective_positive_eod_count', 0)}/{eff_cnt} bought/+2 OTM signals "
            f"({cs['effective_positive_eod_rate_pct']:.1f}%) finished green at EOD "
            f"(median {_fmt_pct(cs.get('effective_med_eod_ret_pct'), 2)})."
        )
    w_dist = cs.get("winning_rank_distribution") or []
    if w_dist:
        dist_str = ", ".join(
            f"{d['label']}: {d['wins']} ({d['share_pct']:.0f}%)" for d in w_dist
        )
        lines.append(f"  #1 Winner Ranks   : {dist_str}")

    # Part B: Optimal duration to hold before selling
    lines.append("")
    lines.append("  2. WHAT IS THE OPTIMAL DURATION TO HOLD BEFORE SELLING?")
    lines.append("  " + "-" * 92)
    for rec in hd.get("recommendations") or []:
        lines.append(f"  * {rec}")

    bp = hd.get("bought_peak_timing")
    wp = hd.get("winner_peak_timing")
    if bp or wp:
        lines.append("")
        if bp:
            lines.append(
                f"  Bought/+2 Time-to-Peak : Median {bp['median_minutes']:.0f}m  |  "
                f"Avg {bp['avg_minutes']:.1f}m  |  P25–P75 {bp['p25_minutes']:.0f}m–{bp['p75_minutes']:.0f}m  |  "
                f"Avg Peak {_fmt_pct(bp['avg_peak_ret_pct'], 2)} vs EOD {_fmt_pct(bp['avg_eod_ret_pct'], 2)}"
            )
        if wp:
            lines.append(
                f"  Winner Time-to-Peak    : Median {wp['median_minutes']:.0f}m  |  "
                f"Avg {wp['avg_minutes']:.1f}m  |  P25–P75 {wp['p25_minutes']:.0f}m–{wp['p75_minutes']:.0f}m  |  "
                f"Avg Peak {_fmt_pct(wp['avg_peak_ret_pct'], 2)} vs EOD {_fmt_pct(wp['avg_eod_ret_pct'], 2)}"
            )

    buckets = hd.get("duration_buckets") or []
    opt_b = hd.get("optimal_bought_duration")
    opt_key = opt_b["key"] if opt_b else None
    if buckets:
        lines.append("")
        lines.append(
            f"  {'HOLD DURATION':<16} {'N(EXACT)':>8} {'BOUGHT AVG':>11} {'BOUGHT MED':>11} "
            f"{'WIN%':>7} {'AVG PNL':>10} {'WINNER AVG':>11} {'ALL AVG':>10}"
        )
        lines.append("  " + "-" * 92)
        for b in buckets:
            bs = b["bought"]
            ws = b["winner"]
            al = b["all"]
            if bs["capped_n"] == 0 and al["capped_n"] == 0:
                continue
            star = "*" if (opt_key and b["key"] == opt_key) else " "
            lbl = f"{star}{b['label']}"
            n_txt = f"{bs['exact_n']}/{bs['capped_n']}" if bs["exact_n"] != bs["capped_n"] else str(bs["capped_n"])
            win_r = f"{bs['capped_win_rate']:.1f}%" if bs["capped_win_rate"] is not None else "--"
            lines.append(
                f"  {lbl:<16} {n_txt:>8} "
                f"{_fmt_pct(bs['capped_avg_ret'], 2):>11} "
                f"{_fmt_pct(bs['capped_med_ret'], 2):>11} "
                f"{win_r:>7} "
                f"{_fmt_dollars(bs['capped_avg_pnl'], 2):>10} "
                f"{_fmt_pct(ws['capped_avg_ret'], 2):>11} "
                f"{_fmt_pct(al['capped_avg_ret'], 2):>10}"
            )
        lines.append(
            "  (* = optimal hold duration for bought/+2 OTM; late-day signals shorter than Xm exit at EOD)"
        )

    lines.append(sep)
    return "\n".join(lines)


def format_winners_only_summary(
    analysis: dict, display_tz: ZoneInfo, tz_label: str
) -> str:
    """Format the end-of-execution summary showing ONLY the winning contract per symbol."""
    winners = analysis.get("winners_by_symbol") or []
    lines = []
    sep = "=" * 96
    sub = "-" * 96
    lines.append(sep)
    lines.append(
        "  WINNING CONTRACT SUMMARY BY SYMBOL  (Option Buy Time vs End of Session)"
    )
    lines.append(sep)

    if not winners:
        lines.append("  No BUY option-chain signals were found to determine winners.")
        lines.append(sep)
        return "\n".join(lines)

    # Compact winners-only overview table
    lines.append(
        f"  {'SYMBOL':<7} {'WINNING CONTRACT':<23} {'RANK':<8} {'BUY TIME (' + tz_label + ')':<19} "
        f"{'ENTRY':>8} {'EOD':>8} {'SESS RET':>10} {'PNL/CTR':>10}"
    )
    lines.append("  " + "-" * 92)
    for item in winners:
        sym = item["symbol"]
        if not item.get("has_winner") or not item.get("winner"):
            lines.append(
                f"  {sym:<7} {'(no bar data)':<23} {'--':<8} {'--':<19} "
                f"{'--':>8} {'--':>8} {'--':>10} {'--':>10}"
            )
            continue
        ev = item["event"]
        w = item["winner"]
        buy_short = (
            _opt_parse_utc(ev["option_buy_dt_utc"])
            .astimezone(display_tz)
            .strftime("%Y-%m-%d %H:%M")
        )
        lines.append(
            f"  {sym:<7} {w['contract']:<23} {w['label']:<8} {buy_short:<19} "
            f"{_fmt_opt(w['entry_price'], 2, '$'):>8} "
            f"{_fmt_opt(w['session_end_price'], 2, '$'):>8} "
            f"{_fmt_pct(w['session_return_pct'], 2):>10} "
            f"{_fmt_dollars(w['session_pnl_dollars'], 2):>10}"
        )

    lines.append("")
    lines.append("  DETAILED WINNER BREAKDOWN (WHY EACH CONTRACT WON)")
    lines.append(sub)

    for idx, item in enumerate(winners, start=1):
        sym = item["symbol"]
        ev = item["event"]
        if not item.get("has_winner") or not item.get("winner"):
            lines.append(f"  [{idx}] {sym} — No Winning Contract Determined")
            lines.append(f"      Reason: {item.get('reason_unavailable', 'No data')}")
            lines.append("")
            continue

        w = item["winner"]
        eod_bar_str = (
            _opt_parse_utc(w["session_end_bar_utc"])
            .astimezone(display_tz)
            .strftime("%Y-%m-%d %H:%M %Z")
            if w.get("session_end_bar_utc")
            else ev["eval_end_display"]
        )
        sess_note = " (Session In Progress)" if ev.get("session_in_progress") else ""
        lines.append(
            f"  [{idx}] {sym}  -->  WINNER: {w['contract']}  "
            f"({w['label']} | ${w['strike']:.2f} {ev['right']} | Exp {ev.get('expiry') or '--'})"
        )
        lines.append(
            f"      Buy Time      : {ev['option_buy_display']} "
            f"[{ev['buy_time_source']}]  |  Spot: ${ev['spot_price']:.2f}"
        )
        lines.append(
            f"      Session End   : {ev['session_close_display']}{sess_note} "
            f"(Evaluated bar: {eod_bar_str}, {ev['hold_to_close_minutes']:.0f}m from buy)"
        )
        lines.append(
            f"      Buy vs EOD    : Entry {_fmt_opt(w['entry_price'], 2, '$')} "
            f"(Bid {_fmt_opt(w['bid'], 2, '$')} / Ask {_fmt_opt(w['ask'], 2, '$')})  -->  "
            f"EOD {_fmt_opt(w['session_end_price'], 2, '$')}  |  "
            f"Return: {_fmt_pct(w['session_return_pct'], 2)}  |  "
            f"P&L/Contract: {_fmt_dollars(w['session_pnl_dollars'], 2)}"
        )
        if ev.get("matched_trade"):
            mt = ev["matched_trade"]
            lines.append(
                f"      Executed Trade: {mt.get('contract')} ({mt.get('status', 'open').upper()}) "
                f"Entry {_fmt_opt(mt.get('entry_price'), 2, '$')} / "
                f"Exit {_fmt_opt(mt.get('exit_price'), 2, '$')} "
                f"({_fmt_pct(mt.get('total_pnl_pct'), 1)})"
            )
        lines.append("      Why It Won    :")
        for r_line in item["reasons"]:
            lines.append(f"        - {r_line}")
        lines.append("")

    lines.append(sep)
    return "\n".join(lines)


def format_full_report(
    analysis: dict, display_tz: ZoneInfo, tz_label: str, run_at: datetime
) -> str:
    """Build the complete nicely formatted text report saved to disk."""
    summary = analysis.get("summary") or {}
    by_rank = summary.get("by_rank") or []
    events = analysis.get("events") or []

    lines = []
    sep = "=" * 96
    sub = "-" * 96

    lines.append(sep)
    lines.append("  FINDTHEWINNERS.PY — OPTION STRIKE ANALYSIS REPORT (BUY TIME VS END OF SESSION)")
    lines.append(sep)
    lines.append(
        f"  Generated At   : {run_at.astimezone(display_tz).strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )
    lines.append(f"  Logs Directory : {analysis.get('log_root')}")
    lines.append(f"  Chains Source  : {analysis.get('chain_dir')}")
    lines.append(
        f"  Filters        : symbol={analysis.get('symbol_filter') or 'ALL'} | "
        f"date={analysis.get('date_filter') or 'ALL'}"
    )
    lines.append(
        f"  Signals Scanned: {summary.get('total_signals', 0)} BUY signals across "
        f"{summary.get('unique_symbols', 0)} unique symbols "
        f"({summary.get('executed_signals', 0)} matched executed trades)"
    )
    lines.append(f"  Market Bars    : {analysis.get('bars_note')}")
    lines.append("")

    # Section 1: Overall Strategy Summary (Contract Correctness & Optimal Hold Duration)
    lines.append(format_overall_strategy_summary(analysis, display_tz, tz_label))
    lines.append("")

    # Section 2: Aggregate Strike-Rank Scoreboard
    lines.append(sub)
    lines.append("  AGGREGATE STRIKE-RANK SCOREBOARD (-2 ITM TO +4 OTM | BUY TIME -> SESSION END)")
    lines.append(sub)
    if by_rank:
        lines.append(
            f"  {'RANK':<9} {'N':>4} {'AVG ASK':>8} {'SPRD%':>7} {'DELTA':>7} "
            f"{'AVG +15m':>9} {'AVG +60m':>9} {'AVG SESS':>9} {'AVG PNL':>10} "
            f"{'AVG PEAK':>9} {'WIN%':>7} {'#1 WINS':>8}"
        )
        lines.append("  " + "-" * 92)
        for r in by_rank:
            tag = "*" if r["otm_rank"] == 2 else " "
            lines.append(
                f" {tag}{r['label']:<8} {r['count']:>4} "
                f"{_fmt_opt(r['avg_entry_ask'], 2, '$'):>8} "
                f"{_fmt_opt(r['avg_spread_pct'], 1, '', '%'):>7} "
                f"{_fmt_opt(r['avg_delta'], 2):>7} "
                f"{_fmt_pct(r['avg_ret_15m'], 1):>9} "
                f"{_fmt_pct(r['avg_ret_60m'], 1):>9} "
                f"{_fmt_pct(r['avg_ret_session'], 2):>9} "
                f"{_fmt_dollars(r['avg_pnl_session'], 2):>10} "
                f"{_fmt_pct(r['avg_ret_peak'], 1):>9} "
                f"{_fmt_opt(r['win_rate_session'], 1, '', '%'):>7} "
                f"{r['winner_count']:>8}"
            )
        lines.append("  (* denotes current +2 OTM default buy rule)")
    else:
        lines.append("  No aggregate strike-rank metrics available.")
    lines.append("")

    # Section 3: Per-Signal Detailed Breakdown
    lines.append(sub)
    lines.append("  DETAILED STRIKE COMPARISON PER BUY SIGNAL")
    lines.append(sub)
    if events:
        for ev in events:
            w = ev.get("winner")
            win_con = w["contract"] if w else "--"
            lines.append(
                f"  SIGNAL: {ev['symbol']} ({ev['right']})  |  "
                f"Buy Time: {ev['option_buy_display']}  -->  Session End: {ev['session_close_display']}"
            )
            lines.append(
                f"  Spot: ${ev['spot_price']:.2f} ({ev['spot_source']})  |  "
                f"EOD Spot: {_fmt_opt(ev['spot_eod'], 2, '$')} ({_fmt_pct(ev['spot_change_pct'], 2)})  |  "
                f"RSI: {_fmt_opt(ev['rsi'], 2)}  |  Exp: {ev.get('expiry') or '--'}  |  "
                f"Event Winner: {win_con}"
            )
            lines.append(
                f"  {'RANK':<8} {'CONTRACT':<22} {'STRIKE':>8} {'ENTRY':>8} {'EOD':>8} "
                f"{'SESS RET':>9} {'PNL/CTR':>10} {'PEAK RET':>9} {'SPRD%':>7} {'DELTA':>7} {'NOTE'}"
            )
            lines.append("  " + "-" * 92)
            for c in ev["candidates"]:
                notes = []
                if w and c["contract"] == w["contract"]:
                    notes.append("WINNER")
                if c.get("is_matched_contract"):
                    notes.append("BOUGHT")
                elif c.get("is_current_pick"):
                    notes.append("+2_RULE")
                lines.append(
                    f"  {c['label']:<8} {c['contract']:<22} "
                    f"{_fmt_opt(c['strike'], 2, '$'):>8} "
                    f"{_fmt_opt(c['entry_price'], 2, '$'):>8} "
                    f"{_fmt_opt(c['session_end_price'], 2, '$'):>8} "
                    f"{_fmt_pct(c['session_return_pct'], 2):>9} "
                    f"{_fmt_dollars(c['session_pnl_dollars'], 2):>10} "
                    f"{_fmt_pct(c['peak_return_pct'], 1):>9} "
                    f"{_fmt_opt(c['spread_pct'], 1, '', '%'):>7} "
                    f"{_fmt_opt(c['delta'], 2):>7} "
                    f"{','.join(notes)}"
                )
            lines.append("")
    else:
        lines.append("  No BUY option chain snapshots found matching filters.")
        lines.append("")

    # Section 4: Final Summary ONLY showing winning contract per symbol
    lines.append(format_winners_only_summary(analysis, display_tz, tz_label))
    return "\n".join(lines) + "\n"


def _resolve_default_paths(
    logs_dir_arg: str | None, ini_arg: str | None
) -> tuple[Path, Path | None]:
    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd()

    ini_path = None
    if ini_arg:
        ini_path = Path(ini_arg).expanduser().resolve()
    else:
        for cand in (
            cwd / "lib" / "parms.ini",
            script_dir / "lib" / "parms.ini",
            cwd / "parms.ini",
            script_dir / "parms.ini",
        ):
            if cand.is_file():
                ini_path = cand
                break

    if logs_dir_arg:
        log_root = Path(logs_dir_arg).expanduser().resolve()
    else:
        log_root = (
            (cwd / "logs")
            if (cwd / "logs").exists()
            else (script_dir / "logs")
        )
    return log_root, ini_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Standalone option strike analyzer comparing option buy time vs end of "
            "session across -2 ITM to +4 OTM strikes, writing a formatted report file "
            "and a winning-contract-only summary per symbol."
        )
    )
    parser.add_argument(
        "--logs-dir",
        default=None,
        help="Path to logs directory containing optionchains/ and minute-summary-*.jsonl (default: ./logs)",
    )
    parser.add_argument(
        "--ini",
        default=None,
        help="Path to parms.ini for Alpaca credentials and display timezone (default: ./lib/parms.ini)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output report file path (default: <logs-dir>/strike_analysis/winners_<timestamp>.txt)",
    )
    parser.add_argument(
        "--symbol",
        default="",
        help="Optional underlying symbol filter (e.g. AAPL, NVDA)",
    )
    parser.add_argument(
        "--date",
        default="",
        help="Optional trading date filter YYYY-MM-DD",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of most-recent BUY signals to analyze",
    )
    parser.add_argument(
        "--include-dry-run",
        action="store_true",
        help="Include OPTION_BUY_DRY_RUN / OPTION_SELL_DRY_RUN log records when matching trades",
    )
    parser.add_argument(
        "--no-alpaca",
        action="store_true",
        help="Skip Alpaca option bar requests (use only local snapshots/sell chains)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log_root, ini_path = _resolve_default_paths(args.logs_dir, args.ini)
    cfg = load_standalone_config(ini_path)
    display_tz, _ = _resolve_display_timezone(cfg["display_timezone"])
    run_at = datetime.now(UTC)
    tz_label = run_at.astimezone(display_tz).tzname() or cfg["display_timezone"]

    api_key = "" if args.no_alpaca else cfg["alpaca_key"]
    api_secret = "" if args.no_alpaca else cfg["alpaca_secret"]

    analysis = analyze_session_winners(
        log_root=log_root,
        display_tz=display_tz,
        api_key=api_key,
        api_secret=api_secret,
        symbol_filter=args.symbol,
        date_filter=args.date,
        limit=args.limit,
        include_dry_run=args.include_dry_run,
        now_utc=run_at,
    )

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
    else:
        stamp = run_at.astimezone(display_tz).strftime("%Y%m%d-%H%M%S")
        out_path = log_root / "strike_analysis" / f"winners_{stamp}.txt"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    report_text = format_full_report(analysis, display_tz, tz_label, run_at)
    out_path.write_text(report_text, encoding="utf-8")

    print(f"Report written to: {out_path}")
    print()
    print(format_winners_only_summary(analysis, display_tz, tz_label))
    print()
    print(format_overall_strategy_summary(analysis, display_tz, tz_label))
    return 0


if __name__ == "__main__":
    sys.exit(main())
