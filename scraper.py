#!/usr/bin/env python3
"""
DUS-Ankünfte Scraper (AirLabs /schedules)
Läuft alle 30 Minuten zwischen 05:30 und 23:30 (Europe/Berlin).
Aufruf mit --force ignoriert das Zeitfenster (zum Testen).
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- Konfiguration
API_KEY = os.environ.get("AIRLABS_API_KEY")
AIRPORT = "DUS"
RAW_CACHE_FILE = ".raw_cache.json"
CACHE_FILE = "cache.json"

TZ = ZoneInfo("Europe/Berlin")
UTC = timezone.utc

# Abruf-Zeitfenster (lokale Zeit). Etwas Toleranz für verspätete Cron-Starts.
WINDOW_START = (5, 25)    # frühester Start (Soll: 05:30)
WINDOW_END = (23, 40)     # spätester Start (Soll: 23:30)
CACHE_DURATION = 25 * 60  # Sekunden; kleiner als das 30-Minuten-Intervall

MINUTES_PAST = 60
HOURS_FUTURE = 5

AIRPORT_NAMES = {
    "MUC": "München", "LHR": "London-Heathrow", "SMI": "Samos", "FCO": "Rom-Fiumicino",
    "IBZ": "Ibiza", "WAW": "Warschau", "CDG": "Paris-Charles-de-Gaulle", "CPH": "Kopenhagen",
    "CFU": "Korfu", "MAN": "Manchester", "RHO": "Rhodos", "BUD": "Budapest",
    "PMI": "Palma de Mallorca", "HAM": "Hamburg", "OTP": "Bukarest", "LIN": "Mailand-Linate",
    "ALC": "Alicante", "BHX": "Birmingham", "FNC": "Madeira", "FRA": "Frankfurt",
    "HER": "Iraklion", "MAD": "Madrid", "DLM": "Dalaman", "AMS": "Amsterdam",
    "FAO": "Faro", "AGP": "Malaga", "HRG": "Hurghada", "KGS": "Kos",
    "PRG": "Prag", "AGA": "Agadir", "BIO": "Bilbao", "LPA": "Gran Canaria",
    "BCN": "Barcelona", "TFS": "Teneriffa Süd", "LCA": "Larnaka", "FUE": "Fuerteventura",
    "IST": "Istanbul", "SAW": "Istanbul-Sabiha Gökçen", "AYT": "Antalya", "VIE": "Wien",
    "ZRH": "Zürich", "SPU": "Split", "DBV": "Dubrovnik", "ATH": "Athen",
    "LIS": "Lissabon", "OPO": "Porto", "ARN": "Stockholm", "OSL": "Oslo",
    "HEL": "Helsinki", "DUB": "Dublin", "LGW": "London-Gatwick", "STN": "London-Stansted",
    "BJV": "Bodrum", "ADB": "Izmir", "TIA": "Tirana", "PRN": "Pristina",
    "SKP": "Skopje", "BEG": "Belgrad", "SOF": "Sofia", "HRG": "Hurghada",
    "RAK": "Marrakesch", "TUN": "Tunis", "DJE": "Djerba", "SSH": "Sharm el-Sheikh",
}

# Alles klein schreiben (Vergleich erfolgt mit .lower())
CHARTER_AIRLINES = [
    "condor", "tuifly", "corendon", "freebird", "smartlynx",
    "eurowings discover", "discover airlines", "sunexpress", "enter air",
    "tailwind", "sundair", "marabu", "mavi gök", "mavi gok",
]

# IATA- und ICAO-Codes gemischt, weil AirLabs je nach Feld beides liefert
CHARTER_CODES = {"DE", "X3", "XC", "FHY", "6Y", "4Y", "XQ", "ENT", "TWI", "SRD", "MBU"}

SKIP_AIRLINE_KEYWORDS = ("flugschule", "training", "flight school")


# ---------------------------------------------------------------- Hilfsfunktionen
def in_run_window(now_local):
    start = now_local.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    end = now_local.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    return start <= now_local <= end


def get_flight_type(airline_name, airline_iata):
    if str(airline_iata or "").upper() in CHARTER_CODES:
        return "Charter"
    name = str(airline_name or "").lower()
    if any(k in name for k in CHARTER_AIRLINES):
        return "Charter"
    return "Linie"


def get_exit_gate(flight_type):
    return "Ausgang 4 (Charter)" if flight_type == "Charter" else "Ausgang 1 (Linie)"


def _parse_str(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(s), fmt)
        except ValueError:
            continue
    return None


def get_dt(flight, ts_key, utc_key, local_key):
    """Liefert eine zeitzonenbewusste Zeit in Europe/Berlin oder None.
    Reihenfolge: Unix-Timestamp -> UTC-String -> lokaler String."""
    ts = flight.get(ts_key)
    if ts:
        try:
            return datetime.fromtimestamp(int(ts), UTC).astimezone(TZ)
        except (ValueError, TypeError, OSError):
            pass
    s = flight.get(utc_key)
    if s:
        dt = _parse_str(s)
        if dt:
            return dt.replace(tzinfo=UTC).astimezone(TZ)
    s = flight.get(local_key)
    if s:
        dt = _parse_str(s)
        if dt:
            return dt.replace(tzinfo=TZ)
    return None


def get_city_name(flight):
    iata = str(flight.get("dep_iata") or "").upper()
    if iata in AIRPORT_NAMES:
        return AIRPORT_NAMES[iata]
    city = flight.get("dep_city")
    if city and len(str(city).strip()) > 1:
        return str(city).strip()
    name = flight.get("dep_name")
    if name and len(str(name).strip()) > 1:
        return str(name).replace(" International", "").replace(" Airport", "").strip()
    return iata or "Unbekannt"


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def valid_response(data):
    return isinstance(data, dict) and "error" not in data and isinstance(data.get("response"), list)


# ---------------------------------------------------------------- Datenabruf
def fetch_flights():
    """Gibt (raw_data, quelle) zurück; quelle: 'Live' | 'Cache' | 'Cache (veraltet)' | None."""
    if os.path.exists(RAW_CACHE_FILE) and time.time() - os.path.getmtime(RAW_CACHE_FILE) < CACHE_DURATION:
        try:
            data = load_json(RAW_CACHE_FILE)
            if valid_response(data):
                return data, "Cache"
        except Exception:
            pass

    if not API_KEY:
        print("Fehler: Umgebungsvariable AIRLABS_API_KEY ist nicht gesetzt.")
    else:
        params = {"api_key": API_KEY, "arr_iata": AIRPORT, "limit": 100}
        url = "https://airlabs.co/api/v9/schedules?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DUS-Flight-Scraper/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if valid_response(data):
                write_json_atomic(RAW_CACHE_FILE, data)
                return data, "Live"
            print(f"API-Fehlerantwort: {str(data)[:200]}")
        except Exception as e:
            print(f"Fehler beim API-Abruf: {e}")

    # Fallback: alte Rohdaten, falls vorhanden
    if os.path.exists(RAW_CACHE_FILE):
        try:
            data = load_json(RAW_CACHE_FILE)
            if valid_response(data):
                return data, "Cache (veraltet)"
        except Exception:
            pass
    return None, None


# ---------------------------------------------------------------- Hauptlogik
def main():
    now = datetime.now(TZ)

    if "--force" not in sys.argv and not in_run_window(now):
        print(f"{now:%H:%M} liegt außerhalb 05:30–23:30, kein Abruf.")
        return

    raw_data, source = fetch_flights()
    if raw_data is None:
        print("Keine Daten verfügbar, cache.json bleibt unverändert.")
        sys.exit(1)

    time_min = now - timedelta(minutes=MINUTES_PAST)
    time_max = now + timedelta(hours=HOURS_FUTURE)

    flights = []
    for fl in raw_data["response"]:
        flight_no = fl.get("flight_iata") or fl.get("flight_number") or ""
        if not flight_no:
            continue

        # Codeshare-Partner überspringen (Betreiberflug bleibt erhalten)
        if fl.get("cs_flight_iata"):
            continue

        airline_name = fl.get("airline_name") or fl.get("airline_iata") or "Unbekannt"
        airline_iata = fl.get("airline_iata") or ""
        if any(k in airline_name.lower() for k in SKIP_AIRLINE_KEYWORDS):
            continue

        sched = get_dt(fl, "arr_time_ts", "arr_time_utc", "arr_time")
        if not sched or not (time_min <= sched <= time_max):
            continue

        try:
            delay = int(fl.get("arr_delayed") or 0)
        except (ValueError, TypeError):
            delay = 0

        est = get_dt(fl, "arr_estimated_ts", "arr_estimated_utc", "arr_estimated")
        if not est:
            est = sched + timedelta(minutes=delay)

        ftype = get_flight_type(airline_name, airline_iata)
        flights.append({
            "dt": sched.timestamp(),
            "time_scheduled": sched.strftime("%H:%M"),
            "time_estimated": est.strftime("%H:%M"),
            "flight_no": flight_no,
            "airline": airline_name,
            "city": get_city_name(fl),
            "type": ftype,
            "gate": get_exit_gate(ftype),
            "status": fl.get("status") or "scheduled",
            "delay": delay,
        })

    flights.sort(key=lambda x: x["dt"])

    # Restliche Duplikate: gleiche Flugnummer + gleiche Zeit
    unique, seen = [], set()
    for f in flights:
        key = (f["flight_no"], f["time_scheduled"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    write_json_atomic(CACHE_FILE, {
        "updated_at": now.strftime("%d.%m.%Y %H:%M"),
        "source": source,
        "flights": unique,
    })
    print(f"cache.json aktualisiert ({source}): {len(unique)} Flüge.")


if __name__ == "__main__":
    main()
