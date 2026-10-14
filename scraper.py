#!/usr/bin/env python3
"""
DUS-Ankünfte Scraper (dus.com Flug-API)
Läuft zwischen 05:25 und 00:05 (Europe/Berlin), Takt über Cron/Scheduler.
Aufruf mit --force ignoriert das Zeitfenster (zum Testen).

Charter/Linie-Zuordnung (Reihenfolge):
  1. Ausgang   : Ausgang 1-2 = Linie, Ausgang 3-6 = Charter   (verlässlich)
  2. Gepäckband: Band 1-6 = Linie, Band 7+ = Charter            (verlässlich)
  3. Flugtyp   : dus.com-Flugtyp 21 (Pauschalreise-Charter)    (Fallback)
  4. Airline   : Airline-Liste                                (Fallback)
Ausgang und Band vergibt der Flughafen erst kurz vor der Landung, davor
greifen die Fallbacks. type_source zeigt, woher die Zuordnung stammt.
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- Konfiguration
API_URL = "https://www.dus.com/api/sitecore/flightapi/SearchFlightsWithOutParams"
REFERER = "https://www.dus.com/de-de/fliegen/ankunft"
CACHE_FILE = "cache.json"

TZ = ZoneInfo("Europe/Berlin")

# Abruf-Zeitfenster (lokale Zeit). Endet am Folgetag um 00:05 Uhr.
WINDOW_START = (5, 25)    # frühester Start (Soll: 05:30)
WINDOW_END = (0, 35)      # spätester Start (bis 00:35 Uhr, cronjob läuft bis 00:55 )

# Anzeige-Fenster (nach tatsächlicher/erwarteter Ankunft)
MINUTES_PAST = 120
HOURS_FUTURE = 5

# Abfrage-Fenster (nach Plan-Zeit), größer als das Anzeige-Fenster,
# damit stark verspätete oder zu früh gelandete Flüge nicht fehlen
FETCH_BACK_HOURS = 4
FETCH_AHEAD_HOURS = HOURS_FUTURE + 1

PAGE_SIZE = 100
MAX_PAGES = 10

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": REFERER,
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"),
}

# Flughafennamen kommen direkt von dus.com (Name + Land auf Deutsch).
AIRPORT_NAME_OVERRIDES = {
    "HER": "Heraklion (Kreta)", "CHQ": "Chania (Kreta)",
    "SUF": "Lamezia Terme (Kalabrien)", "GWT": "Sylt (Westerland)",
    "BIA": "Bastia (Korsika)", "CTA": "Catania (Sizilien)",
    "OLB": "Olbia (Sardinien)", "CAG": "Cagliari (Sardinien)",
    "TFS": "Teneriffa Süd", "TFN": "Teneriffa Nord", "LPA": "Gran Canaria",
    "XRY": "Jerez de la Frontera", "PVK": "Preveza-Aktion",
    "COV": "Mersin-Çukurova", "ESB": "Ankara-Esenboğa",
    "SAW": "Istanbul-Sabiha Gökçen", "GZP": "Gazipaşa-Alanya",
    "HEL": "Helsinki",
}
COUNTRY_RENAMES = {}

CHARTER_AIRLINES = [
    "condor", "tuifly", "corendon", "freebird", "smartlynx",
    "eurowings discover", "discover airlines", "sunexpress", "sun express",
    "enter air", "tailwind", "sundair", "marabu", "mavi gök", "mavi gok",
]
CHARTER_CODES = {"DE", "X3", "XC", "XR", "FHY", "6Y", "4Y", "XQ", "ENT", "E4",
                 "TWI", "SRD", "MBU"}

SKIP_AIRLINE_KEYWORDS = ("flugschule", "training", "flight school")


# ---------------------------------------------------------------- Hilfsfunktionen
def in_run_window(now_local):
    start = now_local.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    end = now_local.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    
    # Wenn das End-Fenster vor dem Start liegt, geht es über Mitternacht hinweg
    if end < start:
        if now_local >= start:
            end = end + timedelta(days=1)
        else:
            start = start - timedelta(days=1)
            
    return start <= now_local <= end


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def first_int(v):
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    m = re.search(r"\d+", str(v or ""))
    return int(m.group(0)) if m else None


def belts_text(belts):
    if isinstance(belts, str):
        belts = [belts]
    out = []
    for b in belts or []:
        b = str(b).strip()
        out.append(str(int(b)) if b.isdigit() else b)
    return "/".join(out)


def terminal_letter(fl, airline):
    gates = fl.get("gate") or []
    if isinstance(gates, str):
        gates = [gates]
    if gates:
        m = re.match(r"[A-Za-z]", str(gates[0]).strip())
        if m:
            return m.group(0).upper()
    return str(airline.get("terminalGateArrival") or "").strip().upper()


def remark_text(fl):
    r = fl.get("remark") or {}
    return str((r.get("publicRemark") or {}).get("name") or r.get("description") or "").strip()


def classify(fl, airline_name, airline_iata):
    ex = first_int(fl.get("arrivalExit"))
    if ex is not None:
        return ("Linie" if ex <= 2 else "Charter"), "Ausgang"

    belt = first_int(fl.get("belt"))
    if belt is not None:
        return ("Linie" if belt <= 6 else "Charter"), "Gepäckband"

    if (fl.get("flightType") or {}).get("code") == 21:
        return "Charter", "Flugtyp"

    if str(airline_iata or "").upper() in CHARTER_CODES:
        return "Charter", "Airline"
    name = str(airline_name or "").lower()
    if any(k in name for k in CHARTER_AIRLINES):
        return "Charter", "Airline"
    return "Linie", "Airline"


def exit_text(fl, flight_type):
    ex = first_int(fl.get("arrivalExit"))
    if ex is not None:
        return f"Ausgang {ex} ({flight_type})"
    return "Ausgang 4 (Charter)" if flight_type == "Charter" else "Ausgang 1 (Linie)"


def map_status(fl):
    st = fl.get("status") or {}
    text = str((st.get("publicStatus") or {}).get("name") or st.get("description") or "").strip()
    low = text.lower()
    if "gelandet" in low and "nicht" not in low:
        return "landed", text
    if "anflug" in low:
        return "active", text
    if any(k in low for k in ("gestrichen", "annull", "cancel")):
        return "cancelled", text
    if "umgeleitet" in low or "divert" in low:
        return "diverted", text
    if not text and fl.get("actualTime"):
        return "landed", text
    return "scheduled", text


def airport_info(fl):
    dest = fl.get("destination") or {}
    iata = str(dest.get("iataCode") or "").upper()
    city_obj = dest.get("city") or {}
    name = (AIRPORT_NAME_OVERRIDES.get(iata)
            or str(dest.get("name") or "").strip()
            or str(city_obj.get("name") or "").strip()
            or iata or "Unbekannt")
    country = str((city_obj.get("country") or {}).get("name") or "").strip()
    return name, COUNTRY_RENAMES.get(country, country)


# ---------------------------------------------------------------- Datenabruf
def get_json(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
                raise RuntimeError(f"Unerwartete Antwort: {str(data)[:200]}")
            return data["data"]
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                raise
            last = e
        except Exception as e:
            last = e
        if attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
    raise last


def fetch_flights(now):
    start = (now - timedelta(hours=FETCH_BACK_HOURS)).replace(microsecond=0)
    end = (now + timedelta(hours=FETCH_AHEAD_HOURS)).replace(microsecond=0)
    flights, offset = [], 0
    for page in range(MAX_PAGES):
        params = {
            "lang": "de",
            "arrival": "true",
            "offset": offset,
            "codeshare": "true",
            "count": PAGE_SIZE,
            "flightStartTime": start.isoformat(),
            "flightEndTime": end.isoformat(),
            "showDetails": "false",
        }
        try:
            data = get_json(API_URL + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            if page == 0:
                raise
            print(f"Seite {page + 1} fehlgeschlagen ({e}), verwende bisherige Daten.")
            break
        batch = data.get("flights") or []
        flights.extend(batch)
        if not data.get("more") or not batch:
            break
        offset += len(batch)
        time.sleep(random.uniform(1.0, 2.5))
    return flights


# ---------------------------------------------------------------- Hauptlogik
def build_flights(raw, now):
    time_min = now - timedelta(minutes=MINUTES_PAST)
    time_max = now + timedelta(hours=HOURS_FUTURE)

    flights, unknown_status = [], set()
    for fl in raw:
        flight_no = re.sub(r"\s+", "", str(fl.get("flightNumber") or ""))
        if not flight_no:
            continue

        if fl.get("codeshare"):
            continue

        airline = fl.get("airline") or {}
        airline_name = airline.get("name") or airline.get("iataCode") or "Unbekannt"
        airline_iata = airline.get("iataCode") or ""
        if any(k in airline_name.lower() for k in SKIP_AIRLINE_KEYWORDS):
            continue

        sched = parse_dt(fl.get("scheduledTime"))
        if not sched:
            continue
        real = parse_dt(fl.get("actualTime")) or parse_dt(fl.get("estimatedTime")) or sched

        if not (time_min <= real <= time_max):
            continue

        delay = max(0, int(round((real - sched).total_seconds() / 60)))
        status, status_text = map_status(fl)
        if status == "scheduled" and status_text:
            unknown_status.add(status_text)

        city, country = airport_info(fl)
        ftype, type_source = classify(fl, airline_name, airline_iata)
        remark = remark_text(fl)
        status_detail = status_text + (", " + remark if remark and status_text else remark)
        flights.append({
            "dt": sched.timestamp(),
            "time_scheduled": sched.strftime("%H:%M"),
            "time_estimated": real.strftime("%H:%M"),
            "flight_no": flight_no,
            "airline": airline_name,
            "city": city,
            "country": country,
            "type": ftype,
            "type_source": type_source,
            "terminal": terminal_letter(fl, airline),
            "baggage": belts_text(fl.get("belt")),
            "gate": exit_text(fl, ftype),
            "status": status,
            "status_text": status_text,
            "status_detail": status_detail,
            "remark": remark,
            "delay": delay,
        })

    flights.sort(key=lambda x: x["dt"])

    unique, seen = [], set()
    for f in flights:
        key = (f["flight_no"], f["time_scheduled"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)
    return unique, unknown_status


def main():
    now = datetime.now(TZ)

    if "--force" not in sys.argv and not in_run_window(now):
        print(f"{now:%H:%M} liegt außerhalb des erlaubten Zeitfensters (05:25–00:05), kein Abruf.")
        return

    try:
        raw = fetch_flights(now)
    except Exception as e:
        print(f"Fehler beim Abruf: {e}")
        print("cache.json bleibt unverändert.")
        sys.exit(1)
    if not raw:
        print("API lieferte keine Flüge, cache.json bleibt unverändert.")
        sys.exit(1)

    unique, unknown_status = build_flights(raw, now)

    by_source = {}
    for f in unique:
        by_source[f["type_source"]] = by_source.get(f["type_source"], 0) + 1
    print(f"Rohdaten: {len(raw)} Einträge, im Fenster: {len(unique)} {by_source}")
    if unknown_status:
        print("Unbekannte Statustexte (als 'scheduled' behandelt):", sorted(unknown_status))

    write_json_atomic(CACHE_FILE, {
        "updated_at": now.strftime("%d.%m.%Y %H:%M"),
        "source": "Dus.com",
        "flights": unique,
    })
    print(f"cache.json aktualisiert: {len(unique)} Flüge.")


if __name__ == "__main__":
    main()
