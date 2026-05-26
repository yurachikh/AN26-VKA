#!/usr/bin/env python3
"""
Мониторинг суточной активности парка авиакомпании по ADS-B.

По умолчанию проверяет весь парк Vulkan Air (UR-CQD/CQE/CQV/CQZ) за
вчерашний день UTC. Для каждой регистрации:

1. Резолвит hex-код через adsbdb.com (кэшируется в fleet.json).
2. Тянет суточный трек с открытых зеркал (adsb.lol → airplanes.live →
   adsb.fi). URL-схема — стандартная для readsb/tar1090, та же что и
   у ADS-B Exchange, поэтому работает прозрачно.
3. Разбивает трек на отрезки по флагу «start of new leg».
4. Определяет аэропорты вылета/посадки по координатам (офлайн-база
   airportsdata).

Использование:
    python check_fleet.py                                # вчера, весь парк
    python check_fleet.py --date 2026-05-25
    python check_fleet.py --fleet UR-CQE UR-CQV          # часть парка
    python check_fleet.py --fleet N628TS --date 2026-05-25  # любой борт
"""

import argparse
import gzip
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

DEFAULT_FLEET = ["UR-CQD", "UR-CQE", "UR-CQV", "UR-CQZ"]

CACHE_FILE = Path(__file__).parent / "fleet.json"

# Зеркала с одинаковой URL-схемой readsb/tar1090. Перебираем по очереди.
TRACE_SOURCES = [
    "https://globe.adsb.lol/globe_history/{yyyy}/{mm}/{dd}/traces/{last2}/trace_full_{hex}.json",
    "https://globe.airplanes.live/globe_history/{yyyy}/{mm}/{dd}/traces/{last2}/trace_full_{hex}.json",
    "https://globe.adsb.fi/globe_history/{yyyy}/{mm}/{dd}/traces/{last2}/trace_full_{hex}.json",
]


def http_get_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "fleet-monitor/1.0 (+github actions)",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        else:
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return json.loads(raw)


# ---------- кэш регистрация → hex --------------------------------------

def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def resolve_hex(reg: str, cache: dict) -> dict:
    """Регистрация → {hex, type, owner}. Кэшируется на диске."""
    reg = reg.upper().strip()
    cached = cache.get(reg)
    if cached and cached.get("hex"):
        return cached
    # adsbdb принимает регистрацию и в формате с дефисом, и без него.
    url = f"https://api.adsbdb.com/v0/aircraft/{reg}"
    try:
        data = http_get_json(url, timeout=15)
        ac = (data or {}).get("response", {}).get("aircraft", {})
        info = {
            "hex": (ac.get("mode_s") or "").lower() or None,
            "type": ac.get("type"),
            "icao_type": ac.get("icao_type"),
            "owner": ac.get("registered_owner"),
        }
        cache[reg] = info
        return info
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {reg}: adsbdb не отдал данные ({e})", file=sys.stderr)
        cache.setdefault(reg, {})
        cache[reg]["error"] = str(e)
        return cache[reg]


# ---------- получение трека --------------------------------------------

def fetch_trace(hex_id: str, date) -> dict | None:
    hex_id = hex_id.lower()
    params = {
        "yyyy": date.strftime("%Y"),
        "mm": date.strftime("%m"),
        "dd": date.strftime("%d"),
        "last2": hex_id[-2:],
        "hex": hex_id,
    }
    for tmpl in TRACE_SOURCES:
        url = tmpl.format(**params)
        try:
            return http_get_json(url, timeout=30)
        except urllib.error.HTTPError:
            continue
        except Exception:
            continue
    return None


# ---------- разбор трека -----------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def nearest_airport(lat, lon, airports, max_km: float = 15):
    best, best_d = None, float("inf")
    for ap in airports.values():
        try:
            d = haversine_km(lat, lon, ap["lat"], ap["lon"])
        except Exception:
            continue
        if d < best_d:
            best_d = d
            best = ap
    if best is not None and best_d <= max_km:
        return best, best_d
    return None, best_d


def parse_legs(trace: dict) -> list[list[dict]]:
    points = trace.get("trace", [])
    t0 = trace.get("timestamp", 0)
    legs: list[list[dict]] = []
    cur: list[dict] = []
    for p in points:
        if len(p) < 7:
            continue
        secs, lat, lon, alt = p[0], p[1], p[2], p[3]
        flags = p[6] or 0
        is_new_leg = (flags & 2) != 0
        pt = {
            "time": datetime.fromtimestamp(t0 + secs, tz=timezone.utc),
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "on_ground": alt == "ground",
        }
        if is_new_leg and cur:
            legs.append(cur)
            cur = []
        cur.append(pt)
    if cur:
        legs.append(cur)
    return [leg for leg in legs if len(leg) >= 2]


def label(pt, airports) -> str:
    ap, d = nearest_airport(pt["lat"], pt["lon"], airports)
    if not ap:
        return f"({pt['lat']:.2f},{pt['lon']:.2f})"
    s = f"{ap['icao']} {ap['name']}"
    if d > 5:
        s += f" [≈{d:.0f}км]"
    return s


# ---------- отчёт ------------------------------------------------------

def report_aircraft(reg: str, info: dict, date, airports) -> list[str]:
    out = []
    hex_id = info.get("hex")
    typ = info.get("type") or info.get("icao_type") or ""
    head = f"\n{reg}"
    if typ and hex_id:
        head += f"  ({typ}, hex {hex_id})"
    elif hex_id:
        head += f"  (hex {hex_id})"
    out.append(head)

    if not hex_id:
        out.append("   не удалось определить hex-код. Проверьте регистрацию.")
        return out

    trace = fetch_trace(hex_id, date)
    if trace is None:
        out.append("   данных ADS-B за день нет (не светил транспондер или вне покрытия).")
        return out

    legs = parse_legs(trace)
    if not legs:
        out.append("   полётов не обнаружено (стоял на земле).")
        return out

    for leg in legs:
        s, e = leg[0], leg[-1]
        dur = int((e["time"] - s["time"]).total_seconds() / 60)
        out.append(
            f"   {s['time']:%H:%M}→{e['time']:%H:%M} UTC  "
            f"{label(s, airports)}  →  {label(e, airports)}  ({dur} мин)"
        )
    return out


def main():
    p = argparse.ArgumentParser(description="Суточная сводка по парку.")
    p.add_argument("--fleet", nargs="*", default=DEFAULT_FLEET,
                   help="Список регистраций (по умолчанию — парк Vulkan Air)")
    p.add_argument("--date", help="YYYY-MM-DD UTC (по умолчанию — вчера)")
    p.add_argument("--title", default="Vulkan Air",
                   help="Заголовок отчёта (по умолчанию: Vulkan Air)")
    args = p.parse_args()

    try:
        import airportsdata
    except ImportError:
        print("Нужен пакет: pip install airportsdata", file=sys.stderr)
        sys.exit(1)
    airports = airportsdata.load("ICAO")

    date = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else (datetime.now(timezone.utc) - timedelta(days=1)).date()
    )

    cache = load_cache()
    lines = [f"=== {args.title}, {date.isoformat()} UTC ==="]
    for reg in args.fleet:
        info = resolve_hex(reg, cache)
        lines.extend(report_aircraft(reg, info, date, airports))
    save_cache(cache)

    print("\n".join(lines))


if __name__ == "__main__":
    main()
