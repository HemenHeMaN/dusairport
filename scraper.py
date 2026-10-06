#!/usr/bin/env python3
"""
DUS-Ankünfte Scraper (AirLabs /schedules)
Läuft alle 30 Minuten zwischen 05:30 und 23:30 (Europe/Berlin).
Aufruf mit --force ignoriert das Zeitfenster (zum Testen).
"""
import json
import os
import re
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

PAGE_SIZE = 100
MAX_PAGES = 5

# IATA: (Name, Land)
AIRPORTS = {
    # Deutschland
    "MUC": ("München", "Deutschland"), "HAM": ("Hamburg", "Deutschland"),
    "FRA": ("Frankfurt", "Deutschland"), "BER": ("Berlin", "Deutschland"),
    # Spanien
    "IBZ": ("Ibiza", "Spanien"), "PMI": ("Palma de Mallorca", "Spanien"),
    "ALC": ("Alicante", "Spanien"), "MAD": ("Madrid", "Spanien"),
    "AGP": ("Malaga", "Spanien"), "BIO": ("Bilbao", "Spanien"),
    "LPA": ("Gran Canaria", "Spanien"), "BCN": ("Barcelona", "Spanien"),
    "TFS": ("Teneriffa Süd", "Spanien"), "FUE": ("Fuerteventura", "Spanien"),
    "XRY": ("Jerez de la Frontera", "Spanien"),
    # Griechenland
    "SMI": ("Samos", "Griechenland"), "CFU": ("Korfu", "Griechenland"),
    "RHO": ("Rhodos", "Griechenland"), "HER": ("Heraklion (Kreta)", "Griechenland"),
    "KGS": ("Kos", "Griechenland"), "KOS": ("Kos", "Griechenland"),
    "ATH": ("Athen", "Griechenland"), "SKG": ("Thessaloniki", "Griechenland"),
    "PVK": ("Preveza-Aktion", "Griechenland"), "KLX": ("Kalamata", "Griechenland"),
    "CHQ": ("Chania (Kreta)", "Griechenland"), "GPA": ("Patras-Araxos", "Griechenland"),
    # Italien
    "FCO": ("Rom-Fiumicino", "Italien"), "LIN": ("Mailand-Linate", "Italien"),
    "BRI": ("Bari", "Italien"), "MXP": ("Mailand-Malpensa", "Italien"),
    "NAP": ("Neapel", "Italien"), "BLQ": ("Bologna", "Italien"),
    "SUF": ("Lamezia Terme (Kalabrien)", "Italien"),
    # Großbritannien / Irland
    "LHR": ("London-Heathrow", "Großbritannien"), "LGW": ("London-Gatwick", "Großbritannien"),
    "STN": ("London-Stansted", "Großbritannien"), "MAN": ("Manchester", "Großbritannien"),
    "BHX": ("Birmingham", "Großbritannien"), "DUB": ("Dublin", "Irland"),
    # Türkei
    "DLM": ("Dalaman", "Türkei"), "IST": ("Istanbul", "Türkei"),
    "SAW": ("Istanbul-Sabiha Gökçen", "Türkei"), "AYT": ("Antalya", "Türkei"),
    "BJV": ("Bodrum", "Türkei"), "ADB": ("Izmir", "Türkei"),
    "ESB": ("Ankara-Esenboğa", "Türkei"), "DIY": ("Diyarbakır", "Türkei"),
    # Portugal
    "FNC": ("Madeira", "Portugal"), "FAO": ("Faro", "Portugal"),
    "LIS": ("Lissabon", "Portugal"), "OPO": ("Porto", "Portugal"),
    # Österreich / Schweiz
    "VIE": ("Wien", "Österreich"), "GRZ": ("Graz", "Österreich"),
    "ZRH": ("Zürich", "Schweiz"),
    # Nord- und Osteuropa
    "CPH": ("Kopenhagen", "Dänemark"), "ARN": ("Stockholm", "Schweden"), "GOT": ("Göteborg", "Schweden"),
    "OSL": ("Oslo", "Norwegen"), "HEL": ("Helsinki", "Finnland"),
    "WAW": ("Warschau", "Polen"), "PRG": ("Prag", "Tschechien"),
    "BUD": ("Budapest", "Ungarn"), "OTP": ("Bukarest", "Rumänien"),
    "SOF": ("Sofia", "Bulgarien"), "RIX": ("Riga", "Lettland"),
    # Westeuropa
    "CDG": ("Paris-Charles-de-Gaulle", "Frankreich"), "AMS": ("Amsterdam", "Niederlande"),
    # Balkan
    "SPU": ("Split", "Kroatien"), "DBV": ("Dubrovnik", "Kroatien"),
    "TIA": ("Tirana", "Albanien"), "PRN": ("Pristina", "Kosovo"),
    "SKP": ("Skopje", "Nordmazedonien"), "BEG": ("Belgrad", "Serbien"),
    # Zypern
    "LCA": ("Larnaka", "Zypern"),
    # Afrika / Naher Osten
    "HRG": ("Hurghada", "Ägypten"), "SSH": ("Sharm el-Sheikh", "Ägypten"), "RMF": ("Marsa Alam", "Ägypten"),
    "AGA": ("Agadir", "Marokko"), "RAK": ("Marrakesch", "Marokko"),
    "TUN": ("Tunis", "Tunesien"), "DJE": ("Djerba", "Tunesien"),
    "DXB": ("Dubai", "Vereinigte Arabische Emirate"),
    "TLV": ("Tel Aviv", "Israel"), "BEN": ("Bengasi", "Libyen"),
    # --- ergänzt ---
    "ACE": ("Lanzarote", "Spanien"),
    "SVQ": ("Sevilla", "Spanien"),
    "TFN": ("Teneriffa Nord", "Spanien"),
    "VLC": ("Valencia", "Spanien"),
    "MAH": ("Menorca", "Spanien"),
    "SPC": ("La Palma", "Spanien"),
    "LEI": ("Almería", "Spanien"),
    "GWT": ("Sylt (Westerland)", "Deutschland"),
    "BZO": ("Bozen", "Italien"),
    "SZG": ("Salzburg", "Österreich"),
    "SGZ": ("Salzburg", "Österreich"),
    "GVA": ("Genf", "Schweiz"),
    "ASR": ("Kayseri", "Türkei"),
    "ADA": ("Adana", "Türkei"),
    "GZT": ("Gaziantep", "Türkei"),
    "GZP": ("Gazipaşa-Alanya", "Türkei"),
    "TZX": ("Trabzon", "Türkei"),
    "EZS": ("Elazığ", "Türkei"),
    "ERZ": ("Erzurum", "Türkei"),
    "ERC": ("Erzincan", "Türkei"),
    "OGU": ("Ordu-Giresun", "Türkei"),
    "SZF": ("Samsun", "Türkei"),
    "ONQ": ("Zonguldak", "Türkei"),
    "KZR": ("Kütahya", "Türkei"),
    "EDO": ("Edremit-Balıkesir", "Türkei"),
    "KNY": ("Konya", "Türkei"),
    "MLX": ("Malatya", "Türkei"),
    "COV": ("Mersin-Çukurova", "Türkei"),
    "EDI": ("Edinburgh", "Großbritannien"),
    "NCL": ("Newcastle", "Großbritannien"),
    "NCE": ("Nizza", "Frankreich"),
    "LYS": ("Lyon", "Frankreich"),
    "MRS": ("Marseille", "Frankreich"),
    "BIA": ("Bastia (Korsika)", "Frankreich"),
    "BGY": ("Mailand-Bergamo", "Italien"),
    "VCE": ("Venedig", "Italien"),
    "FLR": ("Florenz", "Italien"),
    "CTA": ("Catania (Sizilien)", "Italien"),
    "OLB": ("Olbia (Sardinien)", "Italien"),
    "CAG": ("Cagliari (Sardinien)", "Italien"),
    "MLA": ("Malta", "Malta"),
    "KRN": ("Kiruna", "Schweden"),
    "KTT": ("Kittilä", "Finnland"),
    "IVL": ("Ivalo", "Finnland"),
    "RVN": ("Rovaniemi", "Finnland"),
    "KRK": ("Krakau", "Polen"),
    "KIV": ("Chișinău", "Moldau"),
    "VNO": ("Vilnius", "Litauen"),
    "JMK": ("Mykonos", "Griechenland"),
    "ZTH": ("Zakynthos", "Griechenland"),
    "EFL": ("Kefalonia", "Griechenland"),
    "JTR": ("Santorin", "Griechenland"),
    "CAI": ("Kairo", "Ägypten"),
    "LXR": ("Luxor", "Ägypten"),
    "MIR": ("Monastir", "Tunesien"),
    "NDR": ("Nador", "Marokko"),
    "OUD": ("Oujda", "Marokko"),
    "AUH": ("Abu Dhabi", "Vereinigte Arabische Emirate"),
    "DOH": ("Doha", "Katar"),
    "BEY": ("Beirut", "Libanon"),
    "EBL": ("Erbil", "Irak"),
    "ISU": ("Sulaimaniyya", "Irak"),
    "BGW": ("Bagdad", "Irak"),
    "AMM": ("Amman", "Jordanien"),
    "SID": ("Sal", "Kap Verde"),
    "BVC": ("Boa Vista", "Kap Verde"),
    "DKR": ("Dakar", "Senegal"),
    "JFK": ("New York-JFK", "USA"),
    "ATL": ("Atlanta", "USA"),
}

# Fallback, falls ein Flughafen nicht in der Tabelle steht (ISO-Ländercode von AirLabs)
COUNTRY_NAMES = {
    "DE": "Deutschland", "ES": "Spanien", "GR": "Griechenland", "IT": "Italien",
    "GB": "Großbritannien", "IE": "Irland", "TR": "Türkei", "PT": "Portugal",
    "AT": "Österreich", "CH": "Schweiz", "FR": "Frankreich", "NL": "Niederlande",
    "BE": "Belgien", "PL": "Polen", "CZ": "Tschechien", "HU": "Ungarn",
    "RO": "Rumänien", "BG": "Bulgarien", "HR": "Kroatien", "RS": "Serbien",
    "AL": "Albanien", "XK": "Kosovo", "MK": "Nordmazedonien", "CY": "Zypern",
    "EG": "Ägypten", "MA": "Marokko", "TN": "Tunesien", "AE": "Vereinigte Arabische Emirate",
    "DK": "Dänemark", "SE": "Schweden", "NO": "Norwegen", "FI": "Finnland",
    "LV": "Lettland", "LB": "Libanon", "IQ": "Irak", "QA": "Katar", "CV": "Kap Verde", "SN": "Senegal", "MT": "Malta", "MD": "Moldau", "LT": "Litauen", "LY": "Libyen", "US": "USA", "CA": "Kanada", "IL": "Israel", "JO": "Jordanien",
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


def get_flight_type(flight, airline_name, airline_iata):
    """Gibt (Typ, Quelle) zurück.
    Priorität: 1. Terminal (A = Linie, B/C = Charter)
               2. Gepäckband (1-4 = Linie, 5-8 = Charter)
               3. Airline-Heuristik (Fallback, wenn AirLabs noch nichts zugewiesen hat)"""
    terminal = str(flight.get("arr_terminal") or "").strip().upper()
    m = re.search(r"\b([ABC])\b", terminal)
    if m:
        return ("Linie" if m.group(1) == "A" else "Charter"), "Terminal"

    baggage = re.search(r"\d+", str(flight.get("arr_baggage") or ""))
    if baggage:
        belt = int(baggage.group(0))
        if 1 <= belt <= 4:
            return "Linie", "Gepäckband"
        if 5 <= belt <= 8:
            return "Charter", "Gepäckband"

    if str(airline_iata or "").upper() in CHARTER_CODES:
        return "Charter", "Airline"
    name = str(airline_name or "").lower()
    if any(k in name for k in CHARTER_AIRLINES):
        return "Charter", "Airline"
    return "Linie", "Airline"


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


def get_airport_info(flight):
    """Gibt (Stadtname, Land) zurück."""
    iata = str(flight.get("dep_iata") or "").upper()
    if iata in AIRPORTS:
        return AIRPORTS[iata]

    city = flight.get("dep_city")
    name = flight.get("dep_name")
    if city and len(str(city).strip()) > 1:
        city_name = str(city).strip()
    elif name and len(str(name).strip()) > 1:
        city_name = str(name).replace(" International", "").replace(" Airport", "").strip()
    else:
        city_name = iata or "Unbekannt"

    code = str(flight.get("dep_country") or flight.get("dep_country_code") or "").upper()
    country = COUNTRY_NAMES.get(code, "")
    return city_name, country


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
def fetch_all_pages():
    """Lädt mehrere Seiten, bis eine Seite nicht mehr voll ist."""
    all_flights = []
    first_of_prev_page = None
    for page in range(MAX_PAGES):
        params = {
            "api_key": API_KEY,
            "arr_iata": AIRPORT,
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
        }
        url = "https://airlabs.co/api/v9/schedules?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "DUS-Flight-Scraper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if not valid_response(data):
                raise RuntimeError(f"API-Fehlerantwort: {str(data)[:200]}")
        except Exception as e:
            if page == 0:
                raise
            print(f"Seite {page + 1} fehlgeschlagen ({e}), verwende bisherige Daten.")
            break

        batch = data["response"]
        if not batch:
            break
        # Schutz: API ignoriert offset und liefert immer dieselbe Seite
        marker = (batch[0].get("flight_iata"), batch[0].get("arr_time"))
        if page > 0 and marker == first_of_prev_page:
            print("API ignoriert offset, Paginierung abgebrochen.")
            break
        first_of_prev_page = marker if page == 0 else first_of_prev_page

        all_flights.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return {"response": all_flights}


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
        try:
            data = fetch_all_pages()
            if data["response"]:
                write_json_atomic(RAW_CACHE_FILE, data)
                return data, "Live"
            print("API lieferte keine Flüge.")
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
        if not sched:
            continue

        try:
            delay = int(fl.get("arr_delayed") or 0)
        except (ValueError, TypeError):
            delay = 0

        est = get_dt(fl, "arr_estimated_ts", "arr_estimated_utc", "arr_estimated")
        if not est:
            est = sched + timedelta(minutes=delay)

        # Fenster nach erwarteter Ankunft, nicht nach Plan-Zeit
        if not (time_min <= est <= time_max):
            continue

        city, country = get_airport_info(fl)
        ftype, type_source = get_flight_type(fl, airline_name, airline_iata)
        flights.append({
            "dt": sched.timestamp(),
            "time_scheduled": sched.strftime("%H:%M"),
            "time_estimated": est.strftime("%H:%M"),
            "flight_no": flight_no,
            "airline": airline_name,
            "city": city,
            "country": country,
            "type": ftype,
            "type_source": type_source,
            "terminal": fl.get("arr_terminal") or "",
            "baggage": fl.get("arr_baggage") or "",
            "gate": get_exit_gate(ftype),
            "status": fl.get("status") or "scheduled",
            "delay": delay,
        })

    flights.sort(key=lambda x: x["dt"])
    print(f"Rohdaten: {len(raw_data['response'])} Einträge, im Fenster: {len(flights)}")

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
