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
"""

import base64
import json
import os
import random
import re
import shutil
import ssl
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
FIREBASE_USAGE_URL = FIREBASE_URL.rsplit("/", 1)[0] + "/usage_stats.json"

MONITORING_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
MONITORING_SCOPE = "https://www.googleapis.com/auth/monitoring.read"
USAGE_DAYS = 60
USAGE_EVERY_MIN = 10

TZ = ZoneInfo("Europe/Berlin")

WINDOW_START = (5, 0)
WINDOW_END = (3, 35)

MINUTES_PAST = 120
HOURS_FUTURE = 23   # Mindest-Vorlauf; zusätzlich wird immer bis zum nächsten 0 Uhr geladen
ONLY_TODAY = False   # False: Flüge nach Mitternacht schon vorab laden (die Seite zeigt ab 0 Uhr den neuen Tag)

FETCH_BACK_HOURS = 4
FETCH_AHEAD_HOURS = HOURS_FUTURE + 1   # deckt "bis 0 Uhr" (max. 24 Std.) plus Reserve ab

PAGE_SIZE = 100
MAX_PAGES = 10
API_RETRIES = 3
API_TIMEOUT = 20

# SSL-Failsafe bei defektem dus.com-Zertifikat (z.B. "self-signed certificate in
# certificate chain"): Schlägt die Zertifikatsprüfung beim dus.com-Abruf fehl, wird
# automatisch OHNE Prüfung wiederholt, damit die Daten weiter aktuell bleiben.
#   DUS_SSL_FALLBACK=0    -> Failsafe aus (bei Zertifikatsfehler Abbruch wie früher)
#   DUS_SSL_UNVERIFIED=1  -> Prüfung immer aus (nicht mehr nötig, nur zum Erzwingen)
# Gilt NUR für den dus.com-Abruf, nicht für Firebase oder GitHub.
SSL_FALLBACK = os.environ.get("DUS_SSL_FALLBACK", "1").strip() != "0"
SSL_UNVERIFIED = os.environ.get("DUS_SSL_UNVERIFIED", "").strip() == "1"

HISTORY_DAYS = 180
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
    return (
        str(entry.get("date") or ""),
        str(entry.get("scheduled_time") or ""),
    )


# ---------------------------------------------------------------------------
# Flugtyp, Status und Flughafeninformationen
# ---------------------------------------------------------------------------

def classify(flight, airline_name, airline_iata):
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

_ssl_warning_shown = False
_ssl_fallback_active = False


def ssl_unverified_used():
    """True, wenn in diesem Lauf die Zertifikatsprüfung für dus.com aus war."""
    return SSL_UNVERIFIED or _ssl_fallback_active


def api_ssl_context():
    """None = normale, sichere Prüfung; sonst ein Kontext ohne Zertifikatsprüfung."""
    global _ssl_warning_shown
    if not ssl_unverified_used():
        return None
    if not _ssl_warning_shown:
        reason = ("DUS_SSL_UNVERIFIED=1" if SSL_UNVERIFIED
                  else "Zertifikat von dus.com ungültig - Failsafe aktiv")
        log(f"WARNUNG: {reason} - dus.com wird OHNE Zertifikatsprüfung "
            "abgerufen. Wird automatisch wieder geprüft, sobald das Zertifikat "
            "in Ordnung ist.")
        _ssl_warning_shown = True
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def is_cert_error(error):
    reason = getattr(error, "reason", error)
    return (isinstance(reason, ssl.SSLCertVerificationError)
            or "CERTIFICATE_VERIFY_FAILED" in str(error))


def open_api(request, timeout):
    """Öffnet die dus.com-API. Nur bei einem Zertifikatsfehler (und aktivem
    Failsafe) wird einmal ohne Prüfung wiederholt; alle anderen Fehler bleiben."""
    global _ssl_fallback_active
    try:
        return urllib.request.urlopen(request, timeout=timeout, context=api_ssl_context())
    except Exception as error:
        if SSL_FALLBACK and not ssl_unverified_used() and is_cert_error(error):
            _ssl_fallback_active = True
            return urllib.request.urlopen(request, timeout=timeout, context=api_ssl_context())
        raise


def get_json(url, retries=API_RETRIES):
    last_error = None

    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with open_api(request, API_TIMEOUT) as response:
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
    # Bis zum nächsten 0 Uhr, mindestens aber HOURS_FUTURE Stunden voraus. So sieht man
    # tagsüber den ganzen Rest des Tages, und abends liegt der Folgetag schon bereit.
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = max(next_midnight, now + timedelta(hours=HOURS_FUTURE))

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

        status, status_text = map_status(raw)
        is_dead = status in ("cancelled", "diverted")

        actual = parse_dt(raw.get("actualTime"))
        estimated = parse_dt(raw.get("estimatedTime"))
        # Annullierte/umgeleitete Flüge: geplante Zeit statt Schätzzeit (z. B. "Folgetag")
        real_time = scheduled if is_dead else (actual or estimated or scheduled)

        if not (time_min <= real_time <= time_max):
            continue

        if ONLY_TODAY and not is_dead and real_time.date() > now.date():
            continue

        delay = max(0, int(round((real_time - scheduled).total_seconds() / 60)))
        if status == "scheduled" and status_text:
            unknown_status.add(status_text)

        city, country = airport_info(raw)
        flight_type, type_source = classify(raw, airline_name, airline_iata)
        remark = remark_text(raw)
        status_detail = status_text + (", " + remark if remark and status_text else remark)

        actual_exit_number = first_int(raw.get("arrivalExit"))
        actual_baggage = belts_text(raw.get("belt"))

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
            for key, value in new_entry.items():
                if value in (None, ""):
                    continue
                if existing.get(key) != value:
                    existing[key] = value
                    changed = True

        entries.sort(key=lambda entry: history_entry_key(entry))

    return changed


# ---------------------------------------------------------------------------
# Historische Prognosen (Mehrheits-Abgleich)
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


def entry_type(entry):
    """Typ eines Historien-Eintrags; fehlt er, wird er aus Ausgang/Band abgeleitet."""
    if entry.get("type") in ("Linie", "Charter"):
        return entry["type"]
    gate_number = first_int(entry.get("gate"))
    if gate_number is not None:
        return "Linie" if gate_number <= 2 else "Charter"
    belt_number = first_int(entry.get("baggage"))
    if belt_number is not None:
        return "Linie" if belt_number <= 6 else "Charter"
    return None


def plurality_value(entries, key, allow_tie=True):
    """Häufigster Wert. Bei Gleichstand gewinnt der jüngste Eintrag (allow_tie)
    oder es gibt keine Prognose (allow_tie=False)."""
    counts = {}
    latest = {}
    for entry in entries:
        value = entry.get(key)
        if value in (None, "", "----") or "?" in str(value):
            continue
        counts[value] = counts.get(value, 0) + 1
        stamp = history_entry_key(entry)
        if value not in latest or stamp > latest[value]:
            latest[value] = stamp

    if not counts:
        return None

    best = max(counts.values())
    leaders = [value for value, count in counts.items() if count == best]
    if len(leaders) > 1 and not allow_tie:
        return None
    return max(leaders, key=lambda value: latest[value])


def find_historical_matches(flight, history_data):
    flight_no = flight.get("flight_no")
    target_weekday = timestamp_to_local(flight["dt"]).strftime("%A")
    target_time = flight.get("time_scheduled")

    # 1. Exakte Flugnummer und ähnliche Planzeit (Wochentag-Filter entfernt, damit die echte Mehrheit greift!)
    exact_matches = []
    exact_record = history_data.get(flight_no, {})
    for entry in exact_record.get("history", []):
        difference = time_difference_minutes(
            entry.get("scheduled_time"), target_time
        )
        if difference is not None and difference <= HISTORY_TIME_TOLERANCE_MINUTES:
            exact_matches.append(entry)

    if exact_matches:
        return exact_matches, "Flugnummer"

    # 2. Fallback: Airline + Herkunft + ähnliche Planzeit
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
            if difference is not None and difference <= HISTORY_TIME_TOLERANCE_MINUTES:
                fallback_matches.append(entry)

    return fallback_matches, "Airline/Herkunft"


def apply_historical_predictions(flights, history_data):
    if not isinstance(history_data, dict) or not history_data:
        return flights

    for flight in flights:
        needs_gate = not flight.get("gate_actual")
        needs_baggage = not flight.get("baggage_actual")
        can_predict_type = flight.get("type_source") in ("Airline", "Flugtyp")

        if not needs_gate and not needs_baggage and not can_predict_type:
            continue

        matches, match_source = find_historical_matches(flight, history_data)
        if not matches:
            continue

        # 1. Typ bestimmen (Linie/Charter) - sicherer Typ aus der API hat Vorrang
        predicted_type = None
        if can_predict_type:
            types = [entry_type(entry) for entry in matches]
            predicted_type = plurality_value(
                [{"type": t} for t in types if t], "type", allow_tie=False
            )

        effective_type = predicted_type or flight.get("type")

        # 2. Ausgang und Gepäckband NUR aus Einträgen mit demselben Typ.
        #    So gehört ein Band immer zum selben Typ wie der Ausgang
        #    (Linie: Ausgang 1-2 / Band 1-6, Charter: Ausgang 3+ / Band 7+).
        typed_matches = [
            entry for entry in matches if entry_type(entry) == effective_type
        ]

        predicted_gate = (
            plurality_value(typed_matches, "gate") if needs_gate else None
        )
        predicted_baggage = (
            plurality_value(typed_matches, "baggage") if needs_baggage else None
        )

        if predicted_type:
            flight["type"] = predicted_type
            flight["type_source"] = "Prognose (Historie)"
            flight["type_prediction_source"] = match_source

        if predicted_gate:
            flight["gate"] = f"{predicted_gate} ({effective_type}, Prognose)"
            flight["gate_source"] = "Prognose (Historie)"
        elif needs_gate and predicted_type:
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
            flight["prediction_votes"] = len(typed_matches or matches)

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
# Firebase-Nutzung (Werte der Konsole) über Cloud Monitoring
# ---------------------------------------------------------------------------

def parse_rfc3339(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def monitoring_query(token, project, metric, start, end, period, aligner, reducer):
    """Liefert [(Ende des Intervalls, Wert)] für eine Firebase-Metrik."""
    points = []
    page_token = None

    while True:
        params = {
            "filter": f'metric.type = "firebasedatabase.googleapis.com/{metric}"',
            "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "interval.endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "aggregation.alignmentPeriod": f"{int(period)}s",
            "aggregation.perSeriesAligner": aligner,
            "aggregation.crossSeriesReducer": reducer,
            "view": "FULL",
        }
        if page_token:
            params["pageToken"] = page_token

        url = MONITORING_URL.format(project=project) + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        for series in payload.get("timeSeries", []):
            for point in series.get("points", []):
                value = point.get("value", {})
                number = value.get("int64Value", value.get("doubleValue"))
                if number is None:
                    continue
                points.append((parse_rfc3339(point["interval"]["endTime"]), float(number)))

        page_token = payload.get("nextPageToken")
        if not page_token:
            break

    return points


def collect_usage_stats():
    """Downloads pro Tag (UTC), Speicher und Verbindungen aus Cloud Monitoring.
    Braucht für das Service-Konto die Rolle "Monitoring-Betrachter"."""
    service_account_text = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not service_account_text:
        return None

    from datetime import timezone
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    info = json.loads(service_account_text)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=[MONITORING_SCOPE]
    )
    credentials.refresh(Request())
    token, project = credentials.token, info["project_id"]

    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    stats = {"updated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": {}}

    # Vollständige Tage (Intervallende = 00:00 UTC, daher passen die Tagesgrenzen)
    try:
        start = today_start - timedelta(days=USAGE_DAYS)
        for end, value in monitoring_query(
            token, project, "network/sent_bytes_count",
            start, today_start, 86400, "ALIGN_SUM", "REDUCE_SUM",
        ):
            day = (end - timedelta(seconds=1)).strftime("%Y-%m-%d")
            stats["days"][day] = stats["days"].get(day, 0) + int(value)
    except Exception as error:
        log(f"Nutzung (Downloads pro Tag) fehlgeschlagen: {error}")
        return None

    # Heute bisher
    elapsed = int((now_utc - today_start).total_seconds())
    if elapsed >= 60:
        try:
            points = monitoring_query(
                token, project, "network/sent_bytes_count",
                today_start, now_utc, elapsed, "ALIGN_SUM", "REDUCE_SUM",
            )
            stats["days"][today_start.strftime("%Y-%m-%d")] = int(sum(v for _, v in points))
        except Exception as error:
            log(f"Nutzung (Downloads heute) fehlgeschlagen: {error}")

        try:
            points = monitoring_query(
                token, project, "network/active_connections",
                today_start, now_utc, elapsed, "ALIGN_MAX", "REDUCE_MAX",
            )
            if points:
                stats["connections_peak_today"] = int(max(v for _, v in points))
        except Exception as error:
            log(f"Nutzung (Verbindungen) fehlgeschlagen: {error}")

    # Speicher (aktuell): jüngster Messpunkt der letzten 26 Stunden
    try:
        points = monitoring_query(
            token, project, "storage/total_bytes",
            now_utc - timedelta(hours=26), now_utc, 3600, "ALIGN_MAX", "REDUCE_SUM",
        )
        if points:
            stats["storage_bytes"] = int(max(points, key=lambda item: item[0])[1])
    except Exception as error:
        log(f"Nutzung (Speicher) fehlgeschlagen: {error}")

    return stats


def update_usage_stats(credentials):
    if credentials is None:
        return

    try:
        stats = collect_usage_stats()
        if not stats:
            return
        status, _ = firebase_request(
            FIREBASE_USAGE_URL, credentials, method="PUT", data=stats, timeout=20
        )
        month = stats["updated_at"][:7]
        month_bytes = sum(v for k, v in stats["days"].items() if k.startswith(month))
        log(f"usage_stats aktualisiert (HTTP {status}); Downloads {month}: {month_bytes / 1e9:.2f} GB.")
    except Exception as error:
        log(f"Fehler bei usage_stats: {error}")


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

    history_data = load_history(credentials) if credentials else None
    history_read_ok = history_data is not None

    if history_read_ok:
        history_changed = update_historical_database(flights, history_data, now)
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
    if ssl_unverified_used():
        output["ssl_unverified"] = True

    try:
        write_json_atomic(CACHE_FILE, output)
        log(f"{CACHE_FILE} lokal aktualisiert: {len(flights)} Flüge.")
    except Exception as error:
        log(f"Fehler beim lokalen Schreiben von {CACHE_FILE}: {error}")
        sys.exit(1)

    push_to_firebase(credentials)

    if "--force" in sys.argv or now.minute % USAGE_EVERY_MIN < 3:
        update_usage_stats(credentials)

    if "--force" in sys.argv or now.minute % DATA_REPO_EVERY_MIN < 3:
        push_to_data_repo()
    else:
        log("GitHub-Reserve diesmal übersprungen (nur alle 10 Minuten).")


if __name__ == "__main__":
    main()
