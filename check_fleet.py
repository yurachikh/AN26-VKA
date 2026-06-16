#!/usr/bin/env python3
"""
Мониторинг суточной активности парка по ADS-B.
Группирует борты по типу: ATR-72 / Saab 340 / AN-26.

Логика:
- Тянет суточный трек с открытых зеркал ADS-B Exchange (adsb.lol → airplanes.live → adsb.fi).
- Разбивает на отрезки (legs) по флагу «start of new leg» И по временным разрывам >1.5 часа
  (это нужно для случаев когда борт стоит на земле без покрытия ADS-B —
  readsb не успевает поставить флаг, и два рейса склеиваются в один).
- Аэропорт ищется в три прохода: узкий радиус (10км) для ground-точек,
  умеренный радиус (30км) для воздушных точек на краях leg-а, иначе показываем координаты.
- Если первый leg за день начинается в воздухе, а последний leg за вчера —
  тоже заканчивается в воздухе, и разрыв между ними <30 минут — склеиваем
  (рейс пересёк полночь UTC).
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

GROUND_RADIUS_KM = 10        # для точек, явно помеченных как ground
AIR_EDGE_RADIUS_KM = 30      # для воздушных точек на краях leg-а
LEG_GAP_SEC = 5400           # 1.5 часа без сигнала = новый рейс
STITCH_GAP_SEC = 1800        # 30 минут разрыва — допустимо для склейки полуночи
STITCH_GAP_KM = 150          # 150 км — допустимо для склейки полуночи
MIN_LEG_DURATION_SEC = 1200  # 20 минут — короткие обрывки сигнала отбрасываем

# ─── HTTP ─────────────────────────────────────────────────────────────────────

def http_get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "fleet-monitor/4.0",
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

# ─── треки ────────────────────────────────────────────────────────────────────

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
    """Разбиваем точки на отрезки.
    Новый leg = либо флаг readsb, либо разрыв во времени >LEG_GAP_SEC."""
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
        force_new = bool(flags & 2)
        if cur and not force_new:
            gap = (pt["time"] - cur[-1]["time"]).total_seconds()
            if gap > LEG_GAP_SEC:
                force_new = True
        if force_new and cur:
            legs.append(cur); cur = []
        cur.append(pt)
    if cur:
        legs.append(cur)
    return [l for l in legs if len(l) >= 2]


def stitch_with_previous_day(hex_id, date, legs_today):
    """Склеить рейс, пересёкший полночь UTC.
    Условия: первая точка сегодня в воздухе, последняя точка вчера в воздухе,
    разрыв <STITCH_GAP_SEC по времени И <STITCH_GAP_KM географически."""
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
        return legs_today

    gap_sec = (legs_today[0][0]["time"] - last_prev[-1]["time"]).total_seconds()
    if gap_sec > STITCH_GAP_SEC:
        return legs_today

    # Географический разрыв: если конец вчера и начало сегодня далеко друг от друга,
    # это не один и тот же рейс через полночь, а разные рейсы с потерей покрытия.
    gap_km = haversine_km(
        last_prev[-1]["lat"], last_prev[-1]["lon"],
        legs_today[0][0]["lat"], legs_today[0][0]["lon"],
    )
    if gap_km > STITCH_GAP_KM:
        return legs_today

    stitched = last_prev + legs_today[0]
    return [stitched] + legs_today[1:]


def label_airport(ap):
    iata = ap.get("iata") or ""
    code = f"{iata}/{ap['icao']}" if iata else ap["icao"]
    return f"{code} {ap['name']}"


def label_point(pt, airports):
    """Метка точки: аэропорт если найден, иначе координаты."""
    if pt["on_ground"]:
        ap, _ = nearest_airport(pt["lat"], pt["lon"], airports, max_km=GROUND_RADIUS_KM)
        if ap:
            return label_airport(ap)
        return f"(на земле: {pt['lat']:.2f},{pt['lon']:.2f})"
    # Точка в воздухе — но если она первая/последняя в leg-е, аэропорт должен быть рядом
    ap, _ = nearest_airport(pt["lat"], pt["lon"], airports, max_km=AIR_EDGE_RADIUS_KM)
    if ap:
        return label_airport(ap)
    return f"(в воздухе: {pt['lat']:.2f},{pt['lon']:.2f})"


def takeoff_landing(leg):
    """Просто первая и последняя точка leg-а.
    Промежуточные ground-точки игнорируем (бывают артефактом readsb при слабом сигнале)."""
    return leg[0], leg[-1]


def fmt_time(t, report_date):
    """Помечаем время отметкой (-1д), если событие из предыдущих суток."""
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

    # Отбрасываем короткие обрывки сигнала (<20 минут) — это не реальные рейсы
    real_legs = [
        leg for leg in legs
        if (leg[-1]["time"] - leg[0]["time"]).total_seconds() >= MIN_LEG_DURATION_SEC
    ]
    if not real_legs:
        lines.append("   — полёты не выполнял")
        return lines

    for leg in real_legs:
        start, end = takeoff_landing(leg)
        dur = int((end["time"] - start["time"]).total_seconds() / 60)
        lines.append(
            f"   {fmt_time(start['time'], date)}→{fmt_time(end['time'], date)}  "
            f"{label_point(start, airports)} → {label_point(end, airports)}  "
            f"({dur}мин)"
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
