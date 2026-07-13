#!/usr/bin/env python3
"""
Записывает суточную сводку в Google Sheets.

Формат таблицы:
  Дата | ES-NTC | YL-RAG | YL-RAL | ... | UR-CQD | UR-CQE | ...
  01.07.2026 | ZAZ-TNG-ZAZ | | | ... | FKB-HAM-SOF-LCA | ...

Каждая ячейка содержит цепочку IATA-кодов аэропортов через дефис:
все точки за день для этого борта.

Требует:
  pip install gspread google-auth
  Переменная окружения GOOGLE_CREDENTIALS_JSON — содержимое JSON-ключа
  сервисного аккаунта Google Cloud.
  Переменная окружения GOOGLE_SHEET_ID — ID таблицы из URL.
"""

import json
import os
import sys


def get_sheet():
    """Подключаемся к Google Sheets через сервисный аккаунт."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("pip install gspread google-auth", file=sys.stderr)
        sys.exit(1)

    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")

    if not creds_json or not sheet_id:
        print("[sheets] GOOGLE_CREDENTIALS_JSON или GOOGLE_SHEET_ID не заданы, пропускаю.",
              file=sys.stderr)
        return None

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id).sheet1


def extract_route_chain(flights_for_aircraft, airports_db):
    """Из списка рейсов борта за день строим цепочку аэропортов.
    
    Пример: борт летел HHN→TNG, потом TNG→OST → цепочка "HHN-TNG-OST".
    Дублирующийся транзитный аэропорт (TNG конец первого = TNG начало второго)
    схлопывается.
    """
    if not flights_for_aircraft:
        return ""

    chain = []
    for dep, arr in flights_for_aircraft:
        if not chain or chain[-1] != dep:
            chain.append(dep)
        chain.append(arr)
    return "-".join(chain)


def iata_or_icao(code_label):
    """Из метки типа 'HHN/EDFH' или '~EDRM [12км]' или '⟨по плану⟩ TNG/GMTT'
    достаём короткий код (предпочитая IATA)."""
    # Убираем маркеры
    s = code_label.replace("⟨по плану⟩", "").replace("~", "").strip()
    # Убираем [12км] и подобное
    if "[" in s:
        s = s[:s.index("[")].strip()
    # Убираем координаты
    if s.startswith("("):
        return "?"
    # IATA/ICAO — берём IATA (до /)
    if "/" in s:
        parts = s.split("/")
        return parts[0].strip() if parts[0].strip() else parts[1].strip()
    return s.strip() or "?"


def update_sheet(date, aircraft_routes, columns):
    """Добавляет строку в Google Sheet.
    
    Args:
        date: datetime.date
        aircraft_routes: dict {reg: [(dep_label, arr_label), ...]}
        columns: list of registration strings in order
    """
    sheet = get_sheet()
    if sheet is None:
        return

    # Проверяем/создаём заголовок
    try:
        header = sheet.row_values(1)
    except Exception:
        header = []

    expected_header = ["Дата"] + columns
    if header != expected_header:
        if not header or header == [""]:
            sheet.update("A1", [expected_header])
            print("[sheets] Создан заголовок таблицы.")
        else:
            # Заголовок уже есть но отличается — не трогаем, используем как есть
            pass

    # Собираем строку данных
    date_str = date.strftime("%d.%m.%Y")

    # Проверяем: может строка за эту дату уже есть?
    try:
        dates_col = sheet.col_values(1)
        if date_str in dates_col:
            row_idx = dates_col.index(date_str) + 1
            # Обновляем существующую строку
            row_data = [date_str]
            for reg in columns:
                routes = aircraft_routes.get(reg, [])
                chain = extract_route_chain(
                    [(iata_or_icao(d), iata_or_icao(a)) for d, a in routes],
                    None,
                )
                row_data.append(chain)
            sheet.update(f"A{row_idx}", [row_data])
            print(f"[sheets] Обновлена строка за {date_str}")
            return
    except Exception:
        pass

    # Добавляем новую строку
    row_data = [date_str]
    for reg in columns:
        routes = aircraft_routes.get(reg, [])
        chain = extract_route_chain(
            [(iata_or_icao(d), iata_or_icao(a)) for d, a in routes],
            None,
        )
        row_data.append(chain)
    sheet.append_row(row_data, value_input_option="USER_ENTERED")
    print(f"[sheets] Добавлена строка за {date_str}")
