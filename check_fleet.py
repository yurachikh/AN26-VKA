#!/usr/bin/env python3
"""
Мониторинг суточной активности парка по ADS-B.
Группирует борты по типу: ATR-72 / Saab 340 / AN-26.

Логика:
- Тянет суточный трек с открытых зеркал ADS-B Exchange (adsb.lol → airplanes.live → adsb.fi).
- Разбивает на отрезки (legs) по флагу «start of new leg».
- Аэропорты вылета/посадки ищет ТОЛЬКО по точкам, помеченным как «на земле».
- Если первый leg за день начинается в воздухе — подтягивает файл за предыдущий
  день и склеивает («обрезок полуночи UTC»). Это исправляет рейсы, пересёкшие
  00:00 UTC, которые иначе показывались как короткие куски с неверным
  аэропортом вылета.
- Если ни взлёт, ни посадка не попали в покрытие ADS-B — рейс помечается [частично].
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

# ─── конфигурация парка ───────────────────────────────────────────────────────

FLEET_GROUPS = {
    "ATR-72": [
        "ES-NTC",                               # NyxAir
    ],
    "Saab 340": [
        "YL-RAG", "YL-RAL",                     # RAF-Avia
        "SP-KPR", "SP-KPK",                     # SprintAir
        "ES-LSA", "ES-LSG",                     # Airest
    ],
    "AN-26": [
        "UR-CQD", "UR-CQE", "UR-CQV", "UR-CQZ", # Vulkan Air
    ],
}

CACHE_FILE = Path(__file__).parent / "fleet.json"

TRACE_SOURCES = [
    "https://globe.adsb.lol/globe_history/{yyyy}/{mm}/{dd}/traces/{last2}/trace_full_{hex}.json",
    "https://globe.airplanes.live/globe_history/{yyyy}/{mm}/{dd}/traces/{last2}/trace_full_{hex}.json",
    "https://globe.adsb.fi/globe_history/{yyyy}/{mm}/{dd}/traces/{last2}/trace_full_{hex}.json",
]

GROUND_RADIUS_KM = 10  # допустимое расстояние от точки на земле до аэропорта в БД

# ─── HTTP ─────────────────────────────────────────────────────────────────────

def http_get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "fleet-monitor/3.0",
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

# ─── кэш hex-кодов ───────────────────────────────────────────────────────────

def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def resolve_hex(reg, cache):
    reg = reg.upper().strip()
    cached = cache.get(reg)
    if cached and cached.get("hex"):
        return cached
    try:
        data = http_get_json(f"https://api.adsbdb.com/v0/aircraft/{reg}", timeout=15)
        ac = (data or {}).get("response", {}).get("aircraft", {})
        info = {
            "hex":       (ac.get("mode_s") or "").lower() or None,
            "type":      ac.get("type"),
            "icao_type": ac.get("icao_type"),
            "owner":     ac.get("registered_owner"),
        }
        cache[reg] = info
        return info
    except Exception as e:
        print(f"[warn] {reg}: adsbdb не ответил ({e})", file=sys.stderr)
        cache.setdefault(reg, {})
        cache[reg]["error"] = str(e)
        return cache[reg]

# ─── треки и легs ─────────────────────────────────────────────────────────────

def fetch_trace(hex_id, date):
    hex_id = hex_id.lower()
    p = {
        "yyyy": date.strftime("%Y"), "mm": date.strftime("%m"),
        "dd":   date.strftime("%d"), "last2": hex_id[-2:], "hex": hex_id,
    }
    for tmpl in TRACE_SOURCES:
        try:
            return http_get_json(tmpl.format(**p), timeout=30)
        except Exception:
            continue
    return None


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = radians(lat2 - lat1); dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2 * R * asin(sqrt(a))


def nearest_airport(lat, lon, airports, max_km):
    best, best_d = None, float("inf")
    for ap in airports.values():
        try:
            d = haversine_km(lat, lon, ap["lat"], ap["lon"])
        except Exception:
            continue
        if d < best_d:
            best_d = d; best = ap
    if best and best_d <= max_km:
        return best, best_d
    return None, best_d


def parse_legs(trace):
    """Разбиваем точки трека на отрезки по флагу «start of new leg»."""
    points = trace.get("trace", [])
    t0 = trace.get("timestamp", 0)
    legs, cur = [], []
    for p in points:
        if len(p) < 7:
            continue
        secs, lat, lon, alt = p[0], p[1], p[2], p[3]
        flags = p[6] or 0
        pt = {
            "time":      datetime.fromtimestamp(t0 + secs, tz=timezone.utc),
            "lat":       lat,
            "lon":       lon,
            "alt":       alt,
            "on_ground": (alt == "ground"),
        }
        if (flags & 2) and cur:
            legs.append(cur); cur = []
        cur.append(pt)
    if cur:
        legs.append(cur)
    return [l for l in legs if len(l) >= 2]


def stitch_with_previous_day(hex_id, date, legs_today):
    """Если первый leg сегодня начинается в воздухе — рейс пересёк полночь UTC.
    Подтягиваем файл за вчера и склеиваем последний leg оттуда с первым сегодня."""
    if not legs_today or legs_today[0][0]["on_ground"]:
        return legs_today

    prev_trace = fetch_trace(hex_id, date - timedelta(days=1))
    if not prev_trace:
        return legs_today
    prev_legs = parse_legs(prev_trace)
    if not prev_legs:
        return legs_today

    last_prev = prev_legs[-1]
    if last_prev[-1]["on_ground"]:
        # Предыдущий leg уже завершился посадкой накануне — значит это разные рейсы.
        return legs_today

    stitched = last_prev + legs_today[0]
    return [stitched] + legs_today[1:]


def label_point(pt, airports):
    """Метка точки. Аэропорт — только если самолёт на земле и аэропорт в радиусе."""
    if pt["on_ground"]:
        ap, _ = nearest_airport(pt["lat"], pt["lon"], airports, max_km=GROUND_RADIUS_KM)
        if ap:
            iata = ap.get("iata") or ""
            code = f"{iata}/{ap['icao']}" if iata else ap["icao"]
            return f"{code} {ap['name']}"
        return f"(на земле: {pt['lat']:.2f},{pt['lon']:.2f})"
    return f"(в воздухе: {pt['lat']:.2f},{pt['lon']:.2f})"


def takeoff_landing(leg):
    """Первая ground-точка → взлёт, последняя ground-точка → посадка.
    Если ground-точек нет — берём края leg-а (рейс не виден целиком)."""
    takeoff = next((p for p in leg if p["on_ground"]), None)
    landing = next((p for p in reversed(leg) if p["on_ground"]), None)
    start = takeoff if takeoff else leg[0]
    end   = landing if landing else leg[-1]
    complete = bool(takeoff and landing and takeoff is not landing)
    return start, end, complete


def fmt_time(t, report_date):
    """Если событие из вчерашних суток — добавляем пометку (-1д)."""
    if t.date() < report_date:
        return t.strftime("(-1д) %H:%M")
    return t.strftime("%H:%M")

# ─── отчёт ────────────────────────────────────────────────────────────────────

def report_aircraft(reg, info, date, airports):
    lines = []
    hex_id = info.get("hex")
    typ   = info.get("type") or info.get("icao_type") or ""
    owner = info.get("owner") or ""

    head = reg
    if owner: head += f"  [{owner}]"
    if typ:   head += f"  {typ}"
    lines.append(head)

    if not hex_id:
        lines.append("   ⚠ hex не определён")
        return lines

    trace = fetch_trace(hex_id, date)
    if trace is None:
        lines.append("   — полёты не выполнял")
        return lines

    legs = parse_legs(trace)
    if not legs:
        lines.append("   — полёты не выполнял")
        return lines

    legs = stitch_with_previous_day(hex_id, date, legs)

    for leg in legs:
        start, end, complete = takeoff_landing(leg)
        dur = int((end["time"] - start["time"]).total_seconds() / 60)
        marker = "" if complete else "  [частично]"
        lines.append(
            f"   {fmt_time(start['time'], date)}→{fmt_time(end['time'], date)}  "
            f"{label_point(start, airports)} → {label_point(end, airports)}  "
            f"({dur}мин){marker}"
        )
    return lines


def build_report(fleet_groups, date, airports, cache):
    sep = "─" * 36
    lines = [f"✈ Fleet Monitor  {date.isoformat()} UTC", sep]
    for group_name, regs in fleet_groups.items():
        lines.append(f"\n▸ {group_name}")
        for reg in regs:
            info = resolve_hex(reg, cache)
            lines.extend(report_aircraft(reg, info, date, airports))
        lines.append(sep)
    return "\n".join(lines)

# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD UTC (по умолчанию — вчера)")
    args = p.parse_args()

    try:
        import airportsdata
    except ImportError:
        print("pip install airportsdata", file=sys.stderr); sys.exit(1)
    airports = airportsdata.load("ICAO")

    date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
        else (datetime.now(timezone.utc) - timedelta(days=1)).date()
    )

    cache = load_cache()
    report = build_report(FLEET_GROUPS, date, airports, cache)
    save_cache(cache)
    print(report)


if __name__ == "__main__":
    main()
