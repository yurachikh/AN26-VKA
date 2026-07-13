#!/usr/bin/env python3
"""
Мониторинг суточной активности парка по ADS-B.
Группирует борты по типу: ATR-72 / Saab 340 / AN-26.

Ключевая логика — **по высоте**:
- Тянет суточный трек с открытых зеркал ADS-B Exchange.
- Точки на низкой высоте (<3000 ft или ground) считаются у земли — это реальный
  взлёт/посадка.
- Полёты разделяются ТОЛЬКО на реальных посадках: разрыв >30 мин + одна
  из точек у земли. Потери сигнала в крейсе (борт пропал-появился на той же
  крейсерской высоте) разрыв НЕ создают — это один полёт.
- Аэропорт ищется ТОЛЬКО возле точек у земли. Если трек поймал борт уже
  на крейсе и не видел взлёта/посадки — честно пишем «(в воздухе)».
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

# ─── параметры алгоритма ──────────────────────────────────────────────────────

LOW_ALT_FT             = 3000      # высота ниже которой = «у земли» (взлёт/посадка/заруливание)
LANDING_GAP_SEC        = 1800      # 30 минут — минимальная стоянка для разрыва на новый рейс
MAX_SIGNAL_LOSS_SEC    = 10800     # 3 часа — максимальная допустимая потеря сигнала в крейсе
STITCH_GAP_SEC         = 1800      # 30 минут — допустимо для склейки полуночи
AIRPORT_RADIUS_KM      = 20        # радиус поиска аэропорта возле точки у земли
AIR_LABEL_RADIUS_KM    = 100       # радиус поиска аэропорта-ориентира для точек "в воздухе"
MAX_AIRCRAFT_KMH       = 600       # максимальная скорость борта (ATR/Saab/AN-26) для проверки
MIN_FLIGHT_DURATION_S  = 600       # 10 минут — короче считаем артефактом

# Источник «позывной → плановый маршрут» — зеркало VirtualRadarServer от adsb.lol,
# обновляется ежечасно, без авторизации. Используется как подсказка когда трек
# обрывается до посадки или начался уже на крейсе.
ROUTES_API_URL    = "https://vrs-standing-data.adsb.lol/routes/{prefix}/{callsign}.json"
ROUTES_CACHE_FILE = Path(__file__).parent / "routes_cache.json"

# ─── HTTP ─────────────────────────────────────────────────────────────────────

def http_get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": "fleet-monitor/5.0",
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

# ─── географические и высотные хелперы ────────────────────────────────────────

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


def altitude_ft(pt):
    """Высота в футах. 0 для ground, None если неизвестно."""
    alt = pt["alt"]
    if alt == "ground":
        return 0
    if alt is None:
        return None
    try:
        return float(alt)
    except (TypeError, ValueError):
        return None


def is_low(pt):
    """Самолёт у земли (взлёт/посадка/заруливание)."""
    a = altitude_ft(pt)
    return a is not None and a < LOW_ALT_FT

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


def extract_callsign(trace_point):
    """Достаём позывной из точки трека. В readsb-формате он лежит в одном из
    словарных элементов (обычно index 7 или 8) под ключом 'flight', допол-
    ненный пробелами до 8 символов."""
    for el in trace_point:
        if isinstance(el, dict):
            flight = el.get("flight")
            if flight and isinstance(flight, str):
                flight = flight.strip()
                if flight:
                    return flight
    return None


def parse_trace_points(trace):
    """Список словарей-точек из trace JSON."""
    if not trace:
        return []
    points = []
    t0 = trace.get("timestamp", 0)
    for p in trace.get("trace", []):
        if len(p) < 7:
            continue
        secs, lat, lon, alt = p[0], p[1], p[2], p[3]
        points.append({
            "time":     datetime.fromtimestamp(t0 + secs, tz=timezone.utc),
            "lat":      lat,
            "lon":      lon,
            "alt":      alt,
            "callsign": extract_callsign(p),
        })
    return points


def detect_flights(points):
    """Разбивает точки на полёты.
    
    Новый полёт начинается ТОЛЬКО при:
    - разрыве > MAX_SIGNAL_LOSS_SEC (слишком долго даже для крейсе), ИЛИ
    - разрыве > LANDING_GAP_SEC и хотя бы одна точка у разрыва на низкой
      высоте (это реальная посадка/взлёт), ИЛИ
    - разрыве > LANDING_GAP_SEC и дистанция нереальна для скорости борта
      (значит сигнал терялся не в крейсе, а во время реального полёта-стоянки).
    
    Потеря сигнала в крейсе при крейсерской скорости — это один и тот же полёт.
    """
    if not points:
        return []
    flights = [[points[0]]]
    for pt in points[1:]:
        prev = flights[-1][-1]
        gap_sec = (pt["time"] - prev["time"]).total_seconds()
        new = False
        if gap_sec > MAX_SIGNAL_LOSS_SEC:
            new = True
        elif gap_sec > LANDING_GAP_SEC:
            if is_low(prev) or is_low(pt):
                new = True
            else:
                # Проверка скорости: не превышает ли смещение возможное?
                dist_km = haversine_km(prev["lat"], prev["lon"], pt["lat"], pt["lon"])
                max_possible = gap_sec / 3600 * MAX_AIRCRAFT_KMH * 1.2  # +20% запас
                if dist_km > max_possible:
                    new = True
        if new:
            flights.append([pt])
        else:
            flights[-1].append(pt)
    return [f for f in flights if len(f) >= 2 and
            (f[-1]["time"] - f[0]["time"]).total_seconds() >= MIN_FLIGHT_DURATION_S]


def find_takeoff_landing(flight):
    """Взлёт = первая низкая точка В НАЧАЛЕ полёта (первые 20 мин).
    Посадка = последняя низкая точка В КОНЦЕ полёта (последние 20 мин).
    Это отсеивает случаи когда трек поймал борт уже в крейсе и единственная
    низкая точка — это только посадка (или только взлёт)."""
    duration = (flight[-1]["time"] - flight[0]["time"]).total_seconds()
    window = min(1200, duration * 0.4)  # 20 минут или 40% длительности

    takeoff = next((p for p in flight if is_low(p)), None)
    landing = next((p for p in reversed(flight) if is_low(p)), None)

    takeoff_known = False
    if takeoff is not None:
        offset = (takeoff["time"] - flight[0]["time"]).total_seconds()
        takeoff_known = offset <= window

    landing_known = False
    if landing is not None:
        offset = (flight[-1]["time"] - landing["time"]).total_seconds()
        landing_known = offset <= window

    return (
        takeoff if takeoff_known else flight[0],
        landing if landing_known else flight[-1],
        takeoff_known,
        landing_known,
    )


def stitch_with_previous_day(hex_id, date, flights_today):
    """Склейка через полночь UTC.
    Условия: первая точка сегодня НЕ у земли (борт в полёте на полночь),
    последняя точка вчера тоже НЕ у земли,
    разрыв <30мин по времени и расстояние реалистично."""
    if not flights_today or is_low(flights_today[0][0]):
        return flights_today

    prev_trace = fetch_trace(hex_id, date - timedelta(days=1))
    if not prev_trace:
        return flights_today
    prev_points = parse_trace_points(prev_trace)
    prev_flights = detect_flights(prev_points)
    if not prev_flights:
        return flights_today

    last_prev = prev_flights[-1]
    if is_low(last_prev[-1]):
        return flights_today  # вчера уже сел, разные рейсы

    first = flights_today[0]
    gap_sec = (first[0]["time"] - last_prev[-1]["time"]).total_seconds()
    if gap_sec > STITCH_GAP_SEC:
        return flights_today

    dist_km = haversine_km(
        last_prev[-1]["lat"], last_prev[-1]["lon"],
        first[0]["lat"], first[0]["lon"],
    )
    max_possible = gap_sec / 3600 * MAX_AIRCRAFT_KMH * 1.2
    if dist_km > max_possible:
        return flights_today

    stitched = last_prev + first
    return [stitched] + flights_today[1:]


def label_airport(ap):
    iata = ap.get("iata") or ""
    icao = ap.get("icao") or ""
    return f"{iata}/{icao}" if iata else icao


def label_endpoint(pt, known, airports):
    """Если точка у земли (known=True) — точный аэропорт.
    Если борт был в воздухе (трек поймал в крейсе) — ищем ближайший аэропорт
    как ориентир в расширенном радиусе и помечаем «~» (не точная посадка)."""
    if known:
        ap, _ = nearest_airport(pt["lat"], pt["lon"], airports, max_km=AIRPORT_RADIUS_KM)
        if ap:
            return label_airport(ap)
        return f"(на земле: {pt['lat']:.2f},{pt['lon']:.2f})"
    ap, d = nearest_airport(pt["lat"], pt["lon"], airports, max_km=AIR_LABEL_RADIUS_KM)
    if ap:
        return f"~{label_airport(ap)} [{d:.0f}км]"
    return f"(в воздухе: {pt['lat']:.2f},{pt['lon']:.2f})"


def fmt_time(t, report_date):
    if t.date() < report_date:
        return t.strftime("(-1д) %H:%M")
    return t.strftime("%H:%M")


# ─── позывной → плановый маршрут ─────────────────────────────────────────────

def dominant_callsign(flight):
    """Самый частый позывной за этот полёт. Если все None — возвращаем None."""
    from collections import Counter
    callsigns = [p["callsign"] for p in flight if p.get("callsign")]
    if not callsigns:
        return None
    return Counter(callsigns).most_common(1)[0][0]


def load_routes_cache():
    if ROUTES_CACHE_FILE.exists():
        try:
            return json.loads(ROUTES_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_routes_cache(cache):
    ROUTES_CACHE_FILE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def lookup_route(callsign, cache):
    """Запрашиваем VRS-зеркало adsb.lol по позывному.
    Возвращаем {origin: {...}, destination: {...}} или None если позывного нет.
    Кэшируется (включая отрицательные ответы) в routes_cache.json."""
    if not callsign or len(callsign) < 2:
        return None
    if callsign in cache:
        return cache[callsign]  # может быть None — это тоже валидный кэшированный ответ

    prefix = callsign[:2].upper()
    url = ROUTES_API_URL.format(prefix=prefix, callsign=callsign.upper())
    try:
        data = http_get_json(url, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            cache[callsign] = None  # позывного нет в базе — кэшируем «нет»
            return None
        print(f"[warn] vrs-standing-data для {callsign}: HTTP {e.code}", file=sys.stderr)
        cache[callsign] = None
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[warn] vrs-standing-data для {callsign}: {e}", file=sys.stderr)
        return None  # не кэшируем сетевые ошибки

    airports_list = data.get("_airports") if isinstance(data, dict) else None
    if not airports_list or len(airports_list) < 2:
        cache[callsign] = None
        return None

    result = {
        "origin":      airports_list[0],   # первая запись = вылет
        "destination": airports_list[-1],  # последняя = посадка (даже если есть транзит)
    }
    cache[callsign] = result
    return result


def label_route_airport(ap_dict):
    """Метка аэропорта из ответа vrs-standing-data."""
    iata = ap_dict.get("iata") or ""
    icao = ap_dict.get("icao") or ""
    return f"{iata}/{icao}" if iata else icao


# ─── отчёт ────────────────────────────────────────────────────────────────────

def report_aircraft(reg, info, date, airports, routes_cache):
    """Возвращает (lines, route_pairs) где route_pairs — список (dep_label, arr_label)
    для экспорта в Google Sheets."""
    lines = []
    route_pairs = []
    hex_id = info.get("hex")
    typ   = info.get("type") or info.get("icao_type") or ""
    owner = info.get("owner") or ""
    head = reg
    if owner: head += f"  [{owner}]"
    if typ:   head += f"  {typ}"
    lines.append(head)

    if not hex_id:
        lines.append("   ⚠ hex не определён")
        return lines, route_pairs

    trace = fetch_trace(hex_id, date)
    if trace is None:
        lines.append("   — полёты не выполнял")
        return lines, route_pairs

    points = parse_trace_points(trace)
    flights = detect_flights(points)
    if not flights:
        lines.append("   — полёты не выполнял")
        return lines, route_pairs

    flights = stitch_with_previous_day(hex_id, date, flights)

    for flight in flights:
        takeoff, landing, tk_known, ld_known = find_takeoff_landing(flight)
        dur = int((landing["time"] - takeoff["time"]).total_seconds() / 60)

        tk_label = label_endpoint(takeoff, tk_known, airports)
        ld_label = label_endpoint(landing, ld_known, airports)

        callsign = dominant_callsign(flight)
        route = lookup_route(callsign, routes_cache) if callsign else None

        if route:
            if not tk_known:
                tk_label = f"⟨по плану⟩ {label_route_airport(route['origin'])}"
            if not ld_known:
                ld_label = f"⟨по плану⟩ {label_route_airport(route['destination'])}"

        route_pairs.append((tk_label, ld_label))

        suffix = f"  ‹{callsign}›" if callsign else ""
        lines.append(
            f"   {fmt_time(takeoff['time'], date)}→{fmt_time(landing['time'], date)}  "
            f"{tk_label} → {ld_label}  ({dur}мин){suffix}"
        )
    return lines, route_pairs


def build_report(fleet_groups, date, airports, cache, routes_cache):
    """Возвращает (report_text, aircraft_routes) для Telegram и Google Sheets."""
    sep = "─" * 36
    lines = [f"✈ Fleet Monitor  {date.isoformat()} UTC", sep]
    aircraft_routes = {}  # {reg: [(dep, arr), ...]}
    for group_name, regs in fleet_groups.items():
        lines.append(f"\n▸ {group_name}")
        for reg in regs:
            info = resolve_hex(reg, cache)
            ac_lines, ac_routes = report_aircraft(reg, info, date, airports, routes_cache)
            lines.extend(ac_lines)
            aircraft_routes[reg] = ac_routes
        lines.append(sep)
    return "\n".join(lines), aircraft_routes

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
    routes_cache = load_routes_cache()
    report, aircraft_routes = build_report(FLEET_GROUPS, date, airports, cache, routes_cache)
    save_cache(cache)
    save_routes_cache(routes_cache)
    print(report)

    # Экспорт в Google Sheets (если настроен)
    try:
        from sheets_export import update_sheet
        columns = [reg for regs in FLEET_GROUPS.values() for reg in regs]
        update_sheet(date, aircraft_routes, columns)
    except ImportError:
        pass  # gspread не установлен — пропускаем молча
    except Exception as e:
        print(f"[sheets] Ошибка: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
