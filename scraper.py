#!/usr/bin/env python3
"""
DUS-Ankünfte-Scraper mit Firebase-Historie und historischen Prognosen.

Funktionen:
- dus.com Flight-API mit Pagination und Retry
- bestehende Zeit-, Codeshare-, Tages- und Airline-Filter
- Linie/Charter-Erkennung und Status-/Verspätungsaufbereitung
- lokale arrival.json, Firebase-Live-Daten und GitHub-Reserve
- arrival_history.json mit maximal 21 Tagen echter Flugdaten
- Prognosen für Ausgang, Gepäckband und Flugtyp
- echte DUS.com-Werte haben immer Vorrang
- Historie wird pro Lauf nur einmal aus Firebase gelesen

WICHTIG:
Die Historie enthält ausschließlich gelandete Flüge und echte API-Informationen.
Prognosewerte und reine Airline-Standardannahmen werden nicht als Tatsachen
in arrival_history.json gespeichert.
"""

import base64
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

API_URL = "https://www.dus.com/api/sitecore/flightapi/SearchFlightsWithOutParams"
REFERER = "https://www.dus.com/de-de/fliegen/ankunft"

CACHE_FILE = "arrival.json"
DATA_REPO_DIR = ".git_dus_data"
DATA_REPO_EVERY_MIN = 10

FIREBASE_URL = (
    "https://dus-airport-default-rtdb.europe-west1.firebasedatabase.app/arrival.json"
)
FIREBASE_HISTORY_URL = FIREBASE_URL.rsplit("/", 1)[0] + "/arrival_history.json"

TZ = ZoneInfo("Europe/Berlin")

WINDOW_START = (5, 0)
WINDOW_END = (0, 35)

MINUTES_PAST = 120
HOURS_FUTURE = 8
ONLY_TODAY = True

FETCH_BACK_HOURS = 4
FETCH_AHEAD_HOURS = HOURS_FUTURE + 1

PAGE_SIZE = 100
MAX_PAGES = 10
API_RETRIES = 3
API_TIMEOUT = 20

HISTORY_DAYS = 21
HISTORY_TIME_TOLERANCE_MINUTES = 60

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": REFERER,
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}

AIRPORT_NAME_OVERRIDES = {
    "HER": "Heraklion (Kreta)", "CHQ": "Chania (Kreta)",
    "SUF": "Lamezia Terme (Kalabrien)", "GWT": "Sylt (Westerland)",
    "BIA": "Bastia (Korsika)", "CTA": "Catania (Sizilien)",
    "OLB": "Olbia (Sardinien)", "CAG": "Cagliari (Sardinien)",
    "TFS": "Teneriffa Süd", "TFN": "Teneriffa Nord",
    "LPA": "Gran Canaria", "XRY": "Jerez de la Frontera",
    "PVK": "Preveza-Aktion", "COV": "Mersin-Çukurova",
    "ESB": "Ankara-Esenboğa", "SAW": "Istanbul-Sabiha Gökçen",
    "GZP": "Gazipaşa-Alanya", "HEL": "Helsinki",
}
COUNTRY_RENAMES = {}

CHARTER_AIRLINES = [
    "condor", "tuifly", "corendon", "freebird", "smartlynx",
    "eurowings discover", "discover airlines", "sunexpress", "sun express",
    "enter air", "tailwind", "sundair", "marabu", "mavi gök", "mavi gok",
]
CHARTER_CODES = {
    "DE", "X3", "XC", "XR", "FHY", "6Y", "4Y", "XQ",
    "ENT", "E4", "TWI", "SRD", "MBU",
}
SKIP_AIRLINE_KEYWORDS = ("flugschule", "training", "flight school")


# ---------------------------------------------------------------------------
# Allgemeine Hilfsfunktionen
# ---------------------------------------------------------------------------

def log(message):
    print(f"[{datetime.now(TZ):%d.%m.%Y %H:%M:%S}] {message}", flush=True)


def in_run_window(now_local):
    start = now_local.replace(
        hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0
    )
    end = now_local.replace(
        hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0
    )

    if end < start:
        if now_local >= start:
            end += timedelta(days=1)
        else:
            start -= timedelta(days=1)

    return start <= now_local <= end


def write_json_atomic(path, data):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def timestamp_to_local(timestamp):
    return datetime.fromtimestamp(float(timestamp), TZ)


def first_int(value):
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else None


def belts_text(belts):
    if isinstance(belts, str):
        belts = [belts]

    result = []
    for belt in belts or []:
        belt = str(belt).strip()
        if belt:
            result.append(str(int(belt)) if belt.isdigit() else belt)
    return "/".join(result)


def clean_text(value):
    return str(value or "").strip()


def terminal_letter(flight, airline):
    gates = flight.get("gate") or []
    if isinstance(gates, str):
        gates = [gates]
    if gates:
        match = re.match(r"[A-Za-z]", str(gates[0]).strip())
        if match:
            return match.group(0).upper()
    return clean_text(airline.get("terminalGateArrival")).upper()


def remark_text(flight):
    remark = flight.get("remark") or {}
    public_remark = remark.get("publicRemark") or {}
    return clean_text(public_remark.get("name") or remark.get("description"))


def history_entry_key(entry):
    """Eindeutiger Schlüssel: Flugdatum plus planmäßige Uhrzeit."""
    return (
        str(entry.get("date") or ""),
        str(entry.get("scheduled_time") or ""),
    )


# ---------------------------------------------------------------------------
# Flugtyp, Status und Flughafeninformationen
# ---------------------------------------------------------------------------

def classify(flight, airline_name, airline_iata):
    """Bestehende Reihenfolge der Linie-/Charter-Erkennung."""
    exit_number = first_int(flight.get("arrivalExit"))
    if exit_number is not None:
        return ("Linie" if exit_number <= 2 else "Charter"), "Ausgang"

    belt_number = first_int(flight.get("belt"))
    if belt_number is not None:
        return ("Linie" if belt_number <= 6 else "Charter"), "Gepäckband"

    flight_type = flight.get("flightType") or {}
    if flight_type.get("code") == 21:
        return "Charter", "Flugtyp"

    if str(airline_iata or "").upper() in CHARTER_CODES:
        return "Charter", "Airline"

    normalized_name = str(airline_name or "").lower()
    if any(keyword in normalized_name for keyword in CHARTER_AIRLINES):
        return "Charter", "Airline"

    return "Linie", "Airline"


def exit_text(flight, flight_type):
    exit_number = first_int(flight.get("arrivalExit"))
    if exit_number is not None:
        return f"Ausgang {exit_number} ({flight_type})"
    return "Ausgang 4 (Charter)" if flight_type == "Charter" else "Ausgang 1 (Linie)"


def map_status(flight):
    status = flight.get("status") or {}
    public_status = status.get("publicStatus") or {}
    text = clean_text(public_status.get("name") or status.get("description"))
    normalized = text.lower()

    if "gelandet" in normalized and "nicht" not in normalized:
        return "landed", text
    if "anflug" in normalized:
        return "active", text
    if any(word in normalized for word in ("gestrichen", "annull", "cancel")):
        return "cancelled", text
    if "umgeleitet" in normalized or "divert" in normalized:
        return "diverted", text
    if not text and flight.get("actualTime"):
        return "landed", text
    return "scheduled", text


def airport_info(flight):
    destination = flight.get("destination") or {}
    iata = clean_text(destination.get("iataCode")).upper()
    city_object = destination.get("city") or {}

    name = (
        AIRPORT_NAME_OVERRIDES.get(iata)
        or clean_text(destination.get("name"))
        or clean_text(city_object.get("name"))
        or iata
        or "Unbekannt"
    )
    country = clean_text((city_object.get("country") or {}).get("name"))
    return name, COUNTRY_RENAMES.get(country, country)


# ---------------------------------------------------------------------------
# dus.com API
# ---------------------------------------------------------------------------

def get_json(url, retries=API_RETRIES):
    last_error = None

    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))

            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise RuntimeError(f"Unerwartete API-Antwort: {str(payload)[:200]}")

            return payload["data"]

        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                raise
            last_error = error
        except Exception as error:
            last_error = error

        if attempt < retries - 1:
            wait_seconds = 5 * (attempt + 1)
            log(f"API-Fehler: {last_error}; neuer Versuch in {wait_seconds}s.")
            time.sleep(wait_seconds)

    raise last_error


def fetch_flights(now):
    start = (now - timedelta(hours=FETCH_BACK_HOURS)).replace(microsecond=0)
    end = (now + timedelta(hours=FETCH_AHEAD_HOURS)).replace(microsecond=0)

    all_flights = []
    offset = 0

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
        url = API_URL + "?" + urllib.parse.urlencode(params)

        try:
            data = get_json(url)
        except Exception as error:
            if page == 0:
                raise
            log(f"API-Seite {page + 1} fehlgeschlagen ({error}); nutze bisherige Seiten.")
            break

        batch = data.get("flights") or []
        all_flights.extend(batch)

        if not data.get("more") or not batch:
            break

        offset += len(batch)
        time.sleep(random.uniform(1.0, 2.5))

    return all_flights


# ---------------------------------------------------------------------------
# Live-Daten aufbereiten
# ---------------------------------------------------------------------------

def build_flights(raw_flights, now):
    time_min = now - timedelta(minutes=MINUTES_PAST)
    time_max = now + timedelta(hours=HOURS_FUTURE)

    flights = []
    unknown_status = set()

    for raw in raw_flights:
        flight_no = re.sub(r"\s+", "", str(raw.get("flightNumber") or ""))
        if not flight_no:
            continue

        if raw.get("codeshare"):
            continue

        airline = raw.get("airline") or {}
        airline_name = airline.get("name") or airline.get("iataCode") or "Unbekannt"
        airline_iata = airline.get("iataCode") or ""

        if any(keyword in airline_name.lower() for keyword in SKIP_AIRLINE_KEYWORDS):
            continue

        scheduled = parse_dt(raw.get("scheduledTime"))
        if not scheduled:
            continue

        actual = parse_dt(raw.get("actualTime"))
        estimated = parse_dt(raw.get("estimatedTime"))
        real_time = actual or estimated or scheduled

        if not (time_min <= real_time <= time_max):
            continue

        if ONLY_TODAY and real_time.date() > now.date():
            continue

        delay = max(0, int(round((real_time - scheduled).total_seconds() / 60)))
        status, status_text = map_status(raw)
        if status == "scheduled" and status_text:
            unknown_status.add(status_text)

        city, country = airport_info(raw)
        flight_type, type_source = classify(raw, airline_name, airline_iata)
        remark = remark_text(raw)
        status_detail = status_text + (", " + remark if remark and status_text else remark)

        actual_exit_number = first_int(raw.get("arrivalExit"))
        actual_baggage = belts_text(raw.get("belt"))

        # Ein Flugtyp ist nur dann historisch belastbar, wenn er nicht bloß
        # aus dem Airline-Namen bzw. Airline-Code abgeleitet wurde.
        type_is_api_based = type_source in ("Ausgang", "Gepäckband", "Flugtyp")

        flights.append({
            "dt": scheduled.timestamp(),
            "dt_real": real_time.timestamp(),
            "time_scheduled": scheduled.strftime("%H:%M"),
            "time_estimated": real_time.strftime("%H:%M"),
            "flight_no": flight_no,
            "airline": airline_name,
            "city": city,
            "country": country,
            "type": flight_type,
            "type_source": type_source,
            "terminal": terminal_letter(raw, airline),
            "baggage": actual_baggage or "----",
            "gate": exit_text(raw, flight_type),
            "status": status,
            "status_text": status_text,
            "status_detail": status_detail,
            "remark": remark,
            "delay": delay,
            "gate_actual": f"Ausgang {actual_exit_number}" if actual_exit_number is not None else None,
            "baggage_actual": actual_baggage or None,
            "type_actual": flight_type if type_is_api_based else None,
        })

    flights.sort(key=lambda item: (item["dt_real"], item["dt"]))

    unique = []
    seen = set()
    for flight in flights:
        key = (flight["flight_no"], flight["time_scheduled"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(flight)

    return unique, unknown_status


# ---------------------------------------------------------------------------
# Firebase Auth und REST-Zugriff
# ---------------------------------------------------------------------------

def create_firebase_credentials():
    service_account_text = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not service_account_text:
        log("FIREBASE_SERVICE_ACCOUNT fehlt; Firebase wird übersprungen.")
        return None

    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request

        credentials = service_account.Credentials.from_service_account_info(
            json.loads(service_account_text),
            scopes=[
                "https://www.googleapis.com/auth/firebase.database",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
        )
        credentials.refresh(Request())
        return credentials
    except Exception as error:
        log(f"Firebase-Anmeldung fehlgeschlagen: {error}")
        return None


def firebase_request(url, credentials, method="GET", data=None, timeout=15):
    request_url = url + "?access_token=" + urllib.parse.quote(credentials.token)
    body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(
        request_url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return response.status, (json.loads(raw) if raw else None)


def load_history(credentials):
    if credentials is None:
        return None

    try:
        _, data = firebase_request(
            FIREBASE_HISTORY_URL, credentials, method="GET", timeout=15
        )
        return data if isinstance(data, dict) else {}
    except Exception as error:
        log(f"Historie konnte nicht gelesen werden: {error}")
        return None


def save_history(history_data, credentials):
    try:
        status, _ = firebase_request(
            FIREBASE_HISTORY_URL,
            credentials,
            method="PUT",
            data=history_data,
            timeout=20,
        )
        log(f"arrival_history.json aktualisiert (HTTP {status}).")
        return True
    except Exception as error:
        log(f"Fehler beim Schreiben der Historie: {error}")
        return False


# ---------------------------------------------------------------------------
# Historische Daten: Aufbewahrung und Speicherung
# ---------------------------------------------------------------------------

def prune_history(history_data, now):
    """Entfernt Historieneinträge, die älter als 21 Tage sind."""
    cutoff_date = (now.date() - timedelta(days=HISTORY_DAYS - 1)).isoformat()
    changed = False

    for flight_no in list(history_data.keys()):
        record = history_data.get(flight_no)
        if not isinstance(record, dict):
            history_data.pop(flight_no, None)
            changed = True
            continue

        entries = record.get("history", [])
        if not isinstance(entries, list):
            entries = []

        kept = [
            entry for entry in entries
            if isinstance(entry, dict)
            and str(entry.get("date") or "") >= cutoff_date
        ]

        if len(kept) != len(entries):
            record["history"] = kept
            changed = True

        if not kept:
            history_data.pop(flight_no, None)
            changed = True

    return changed


def update_historical_database(flights, history_data, now):
    """
    Aktualisiert die bereits geladene Historie.
    Es werden nur gelandete Flüge mit mindestens einem echten Flughafenwert
    gespeichert. Identität = Flugnummer + Datum + Planzeit.
    """
    if history_data is None:
        return False

    changed = prune_history(history_data, now)

    for flight in flights:
        if str(flight.get("status", "")).lower() != "landed":
            continue

        actual_gate = flight.get("gate_actual")
        actual_baggage = flight.get("baggage_actual")
        actual_type = flight.get("type_actual")

        if not actual_gate and not actual_baggage and not actual_type:
            continue

        flight_no = flight.get("flight_no")
        if not flight_no:
            continue

        scheduled_dt = timestamp_to_local(flight["dt"])
        flight_date = scheduled_dt.strftime("%Y-%m-%d")
        weekday = scheduled_dt.strftime("%A")

        flight_record = history_data.setdefault(
            flight_no,
            {
                "airline": flight.get("airline"),
                "city": flight.get("city"),
                "history": [],
            },
        )
        if flight_record.get("airline") != flight.get("airline") and flight.get("airline"):
            flight_record["airline"] = flight.get("airline")
            changed = True
        if flight_record.get("city") != flight.get("city") and flight.get("city"):
            flight_record["city"] = flight.get("city")
            changed = True

        entries = flight_record.setdefault("history", [])
        if not isinstance(entries, list):
            entries = []
            flight_record["history"] = entries
            changed = True

        existing = next(
            (
                entry for entry in entries
                if history_entry_key(entry) == (flight_date, flight.get("time_scheduled"))
            ),
            None,
        )

        new_entry = {
            "date": flight_date,
            "weekday": weekday,
            "scheduled_time": flight.get("time_scheduled"),
            "gate": actual_gate,
            "baggage": actual_baggage,
            "type": actual_type,
            "city": flight.get("city"),
            "airline": flight.get("airline"),
        }

        if existing is None:
            entries.append(new_entry)
            changed = True
        else:
            # Nur echte, aktuell gelieferte Werte aktualisieren. Fehlende API-
            # Werte löschen keine bereits gespeicherten echten Angaben.
            for key, value in new_entry.items():
                if value in (None, ""):
                    continue
                if existing.get(key) != value:
                    existing[key] = value
                    changed = True

        entries.sort(key=lambda entry: history_entry_key(entry))

    return changed


# ---------------------------------------------------------------------------
# Historische Prognosen
# ---------------------------------------------------------------------------

def time_to_minutes(value):
    try:
        hours, minutes = str(value).split(":")[:2]
        return int(hours) * 60 + int(minutes)
    except (TypeError, ValueError):
        return None


def time_difference_minutes(first_time, second_time):
    first = time_to_minutes(first_time)
    second = time_to_minutes(second_time)
    if first is None or second is None:
        return None

    difference = abs(first - second)
    return min(difference, 1440 - difference)


def most_common_value(values):
    cleaned = [value for value in values if value not in (None, "", "----")]
    if not cleaned:
        return None
    return Counter(cleaned).most_common(1)[0][0]


def find_historical_matches(flight, history_data):
    flight_no = flight.get("flight_no")
    target_weekday = timestamp_to_local(flight["dt"]).strftime("%A")
    target_time = flight.get("time_scheduled")

    # 1. Exakte Flugnummer, gleicher Wochentag und ähnliche Planzeit.
    exact_matches = []
    exact_record = history_data.get(flight_no, {})
    for entry in exact_record.get("history", []):
        difference = time_difference_minutes(
            entry.get("scheduled_time"), target_time
        )
        if entry.get("weekday") == target_weekday and difference is not None:
            if difference <= HISTORY_TIME_TOLERANCE_MINUTES:
                exact_matches.append(entry)

    if exact_matches:
        return exact_matches, "Flugnummer"

    # 2. Fallback: Airline + Herkunft + Wochentag + ähnliche Planzeit.
    fallback_matches = []
    target_city = flight.get("city")
    target_airline = flight.get("airline")

    for record in history_data.values():
        if not isinstance(record, dict):
            continue

        for entry in record.get("history", []):
            entry_city = entry.get("city") or record.get("city")
            entry_airline = entry.get("airline") or record.get("airline")

            if entry_city != target_city or entry_airline != target_airline:
                continue

            difference = time_difference_minutes(
                entry.get("scheduled_time"), target_time
            )
            if entry.get("weekday") == target_weekday and difference is not None:
                if difference <= HISTORY_TIME_TOLERANCE_MINUTES:
                    fallback_matches.append(entry)

    return fallback_matches, "Airline/Herkunft"


def apply_historical_predictions(flights, history_data):
    """
    Ergänzt ausschließlich fehlende Felder.
    DUS.com-Echtwerte bleiben unangetastet; Prognosen werden gekennzeichnet.
    """
    if not isinstance(history_data, dict) or not history_data:
        return flights

    for flight in flights:
        needs_gate = not flight.get("gate_actual")
        needs_baggage = not flight.get("baggage_actual")
        can_predict_type = flight.get("type_source") == "Airline"

        if not needs_gate and not needs_baggage and not can_predict_type:
            continue

        matches, match_source = find_historical_matches(flight, history_data)
        if not matches:
            continue

        gates = [
            entry.get("gate") for entry in matches
            if entry.get("gate") and "?" not in str(entry.get("gate"))
        ]
        baggages = [
            entry.get("baggage") for entry in matches
            if entry.get("baggage") and "?" not in str(entry.get("baggage"))
        ]
        types = [
            entry.get("type") for entry in matches
            if entry.get("type") in ("Linie", "Charter")
        ]

        predicted_gate = most_common_value(gates) if needs_gate else None
        predicted_baggage = most_common_value(baggages) if needs_baggage else None
        predicted_type = most_common_value(types) if can_predict_type else None

        if predicted_type:
            flight["type"] = predicted_type
            flight["type_source"] = "Prognose (Historie)"
            flight["type_prediction_source"] = match_source

        if predicted_gate:
            flight_type = predicted_type or flight.get("type")
            flight["gate"] = f"{predicted_gate} ({flight_type}, Prognose)"
            flight["gate_source"] = "Prognose (Historie)"
            flight["prediction_match"] = match_source
        elif needs_gate and predicted_type:
            # Nur der Fallback-Ausgang wird an den prognostizierten Typ angepasst.
            flight["gate"] = (
                "Ausgang 4 (Charter, Prognose)"
                if predicted_type == "Charter"
                else "Ausgang 1 (Linie, Prognose)"
            )
            flight["gate_source"] = "Typ-Prognose (Historie)"

        if predicted_baggage:
            flight["baggage"] = f"{predicted_baggage} (Prognose)"
            flight["baggage_source"] = "Prognose (Historie)"

        if predicted_gate or predicted_baggage or predicted_type:
            flight["prediction_match"] = match_source
            flight["prediction_votes"] = len(matches)

    return flights


# ---------------------------------------------------------------------------
# Live-Daten nach Firebase schreiben
# ---------------------------------------------------------------------------

def push_to_firebase(credentials):
    if credentials is None:
        return

    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as file:
            live_data = json.load(file)

        status, _ = firebase_request(
            FIREBASE_URL,
            credentials,
            method="PUT",
            data=live_data,
            timeout=20,
        )
        log(f"Firebase Live-Daten aktualisiert (HTTP {status}).")
    except Exception as error:
        log(f"Fehler beim Schreiben der Live-Daten nach Firebase: {error}")


# ---------------------------------------------------------------------------
# GitHub-Reserve
# ---------------------------------------------------------------------------

def push_to_data_repo():
    token = os.environ.get("DUSFLUGDATENKEY")
    if not token:
        log("DUSFLUGDATENKEY fehlt; GitHub-Reserve wird übersprungen.")
        return

    username = "HemenHeMaN"
    repository_url = f"https://github.com/{username}/dusflugdaten.git"

    try:
        os.makedirs(DATA_REPO_DIR, exist_ok=True)

        if not os.path.isdir(os.path.join(DATA_REPO_DIR, ".git")):
            subprocess.run(["git", "-C", DATA_REPO_DIR, "init"], check=True)
            subprocess.run(
                ["git", "-C", DATA_REPO_DIR, "remote", "add", "origin", repository_url],
                check=True,
            )
        else:
            subprocess.run(
                ["git", "-C", DATA_REPO_DIR, "remote", "set-url", "origin", repository_url],
                check=True,
            )

        subprocess.run(
            ["git", "config", "--global", "user.name", "DUS Scraper Bot"],
            check=True,
        )
        subprocess.run(
            ["git", "config", "--global", "user.email", "bot@dus.local"],
            check=True,
        )

        shutil.copy2(CACHE_FILE, os.path.join(DATA_REPO_DIR, "arrival.json"))

        subprocess.run(
            ["git", "-C", DATA_REPO_DIR, "checkout", "-B", "main"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", DATA_REPO_DIR, "add", "arrival.json"],
            check=True,
        )

        status = subprocess.run(
            ["git", "-C", DATA_REPO_DIR, "diff", "--cached", "--quiet"]
        )
        if status.returncode == 0:
            log("GitHub-Reserve: keine Änderungen, kein Push nötig.")
            return

        subprocess.run(
            ["git", "-C", DATA_REPO_DIR, "commit", "-m", "Auto-update flight cache"],
            check=True,
        )

        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        subprocess.run(
            [
                "git", "-C", DATA_REPO_DIR,
                "-c", f"http.extraheader=AUTHORIZATION: basic {auth}",
                "push", "-f", "origin", "main",
            ],
            check=True,
        )
        log("Flugdaten erfolgreich in das GitHub-Daten-Repo gepusht.")

    except Exception as error:
        log(f"Fehler beim Pushen ins Daten-Repo: {error}")


# ---------------------------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------------------------

def main():
    now = datetime.now(TZ)

    if "--force" not in sys.argv and not in_run_window(now):
        log(
            f"{now:%H:%M} liegt außerhalb des Abruffensters "
            f"(05:00–00:35); kein Abruf."
        )
        return

    try:
        raw_flights = fetch_flights(now)
    except Exception as error:
        log(f"Fehler beim Abruf der dus.com-API: {error}")
        log(f"{CACHE_FILE} bleibt unverändert.")
        sys.exit(1)

    if not raw_flights:
        log(f"API lieferte keine Flüge; {CACHE_FILE} bleibt unverändert.")
        sys.exit(1)

    flights, unknown_status = build_flights(raw_flights, now)

    source_counts = {}
    for flight in flights:
        source = flight["type_source"]
        source_counts[source] = source_counts.get(source, 0) + 1

    log(
        f"Rohdaten: {len(raw_flights)} Einträge; "
        f"im Anzeige-Fenster: {len(flights)} Flüge; "
        f"Klassifizierung: {source_counts}"
    )

    if unknown_status:
        log(
            "Unbekannte Statustexte (als 'scheduled' behandelt): "
            + ", ".join(sorted(unknown_status))
        )

    credentials = create_firebase_credentials()

    # Historie nur EINMAL pro Lauf laden. Bei Lesefehler wird sie weder
    # überschrieben noch werden auf Basis einer leeren Historie Prognosen gebaut.
    history_data = load_history(credentials) if credentials else None
    history_read_ok = history_data is not None

    if history_read_ok:
        history_changed = update_historical_database(flights, history_data, now)

        # Prognosen verwenden auch neu eingetroffene echte Daten aus diesem Lauf.
        flights = apply_historical_predictions(flights, history_data)

        if history_changed:
            save_history(history_data, credentials)
        else:
            log("Historie: keine Änderungen; kein Firebase-Schreibzugriff.")
    else:
        log("Historie nicht verfügbar; Prognosen und Historien-Update übersprungen.")

    output = {
        "updated_at": now.strftime("%d.%m.%Y %H:%M"),
        "source": "Dus.com",
        "flights": flights,
    }

    try:
        write_json_atomic(CACHE_FILE, output)
        log(f"{CACHE_FILE} lokal aktualisiert: {len(flights)} Flüge.")
    except Exception as error:
        log(f"Fehler beim lokalen Schreiben von {CACHE_FILE}: {error}")
        sys.exit(1)

    push_to_firebase(credentials)

    if "--force" in sys.argv or now.minute % DATA_REPO_EVERY_MIN < 3:
        push_to_data_repo()
    else:
        log("GitHub-Reserve diesmal übersprungen (nur alle 10 Minuten).")


if __name__ == "__main__":
    main()
