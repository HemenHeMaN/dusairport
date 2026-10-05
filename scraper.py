from datetime import datetime, timedelta
import urllib.request
import urllib.parse
import json
import os
import time

API_KEY = os.environ.get("AIRLABS_API_KEY")
AIRPORT = "DUS"
CACHE_FILE = "cache.json"
CACHE_DURATION = 300  # 5 Minuten

MINUTES_PAST = 60
HOURS_FUTURE = 5

AIRPORT_NAMES = {
    "MUC": "München",
    "LHR": "London-Heathrow",
    "SMI": "Samos",
    "FCO": "Rom-Fiumicino",
    "IBZ": "Ibiza",
    "WAW": "Warschau",
    "CDG": "Paris-Charles-de-Gaulle",
    "CPH": "Kopenhagen",
    "CFU": "Korfu",
    "MAN": "Manchester",
    "RHO": "Rhodos",
    "BUD": "Budapest",
    "PMI": "Palma de Mallorca",
    "HAM": "Hamburg",
    "OTP": "Bukarest",
    "LIN": "Mailand-Linate",
    "ALC": "Alicante",
    "BHX": "Birmingham",
    "FNC": "Madeira",
    "FRA": "Frankfurt",
    "HER": "Iraklion",
    "MAD": "Madrid",
    "DLM": "Dalaman",
    "AMS": "Amsterdam",
    "FAO": "Faro",
    "AGP": "Malaga",
    "HRG": "Hurghada",
    "KGS": "Kos",
    "PRG": "Prag",
    "AGA": "Agadir",
    "BIO": "Bilbao",
    "LPA": "Gran Canaria",
    "BCN": "Barcelona",
    "TFS": "Teneriffa Süd",
    "LCA": "Larnaka",
    "FUE": "Fuerteventura"
}

CHARTER_AIRLINES = [
    "condor", "tuifly", "corendon", "freebird", "smartlynx", 
    "eurowings discover", "discover airlines", "sunexpress", "enter air",
    "Tailwind", "Sundair", "Marabu", "Mavi Gök"
]

CHARTER_CODES = [
    "DE", "X3", "XC", "FHY", "6Y", "4Y", "XQ", "ENT", "TWI", "SRD", "MBU"
]

def get_flight_type(airline_name, airline_iata):
    airline_lower = str(airline_name or "").lower()
    iata_upper = str(airline_iata or "").upper()
    if iata_upper in CHARTER_CODES:
        return "Charter"
    for keyword in CHARTER_AIRLINES:
        if keyword in airline_lower:
            return "Charter"
    return "Linie"

def get_exit_gate(flight_type):
    return "Ausgang 4 (Charter)" if flight_type == "Charter" else "Ausgang 1 (Linie)"

def parse_airlabs_time(time_string):
    if not time_string:
        return None
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(str(time_string), fmt)
        except ValueError:
            pass
    return None

def get_dynamic_city_name(flight_data):
    iata = flight_data.get("dep_iata")
    if iata and str(iata).upper() in AIRPORT_NAMES:
        return AIRPORT_NAMES[str(iata).upper()]
    city = flight_data.get("dep_city")
    if city and len(str(city).strip()) > 1:
        return str(city).strip()
    name = flight_data.get("dep_name")
    if name and len(str(name).strip()) > 1:
        return str(name).replace(" Airport", "").replace(" International", "").strip()
    return str(iata).strip() if iata else "Unbekannt"

def fetch_flights():
    if os.path.exists(CACHE_FILE):
        if time.time() - os.path.getmtime(CACHE_FILE) < CACHE_DURATION:
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # Wenn die Cache-Datei bereits unsere verarbeitete Struktur hat, direkt zurückgeben
                    if isinstance(data, dict) and "flights" in data:
                        return data, True
            except Exception:
                pass

    params = {"api_key": API_KEY, "arr_iata": AIRPORT, "limit": 80}
    api_url = "https://airlabs.co/api/v9/schedules?" + urllib.parse.urlencode(params)

    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "DUS-Flight-Scraper/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw_data = json.loads(resp.read().decode("utf-8"))
        return raw_data, False
    except Exception as e:
        print(f"Fehler beim Abrufen: {e}")
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f), True
        return {"response": []}, False

def main():
    now = datetime.now()
    time_min = now - timedelta(minutes=MINUTES_PAST)
    time_max = now + timedelta(hours=HOURS_FUTURE)

    raw_data, from_cache = fetch_flights()
    
    # Falls es rohe API-Daten sind, verarbeiten
    raw_flights = raw_data.get("response", raw_data) if isinstance(raw_data, dict) else raw_data
    if not isinstance(raw_flights, list):
        raw_flights = []

    valid_list = []

    for flight in raw_flights:
        flight_iata = flight.get("flight_iata") or flight.get("flight_number") or ""
        if not flight_iata:
            continue

        airline_name = flight.get("airline_name") or flight.get("airline_iata") or "Unbekannt"
        airline_iata = flight.get("airline_iata") or ""

        scheduled_time = flight.get("arr_scheduled")
        scheduled_dt = parse_airlabs_time(scheduled_time)
        if not scheduled_dt or not (time_min <= scheduled_dt <= time_max):
            continue

        if any(x in airline_name.lower() for x in ["flugschule", "training", "flight school"]):
            continue

        delay = flight.get("arr_delayed")
        try:
            delay = int(delay) if delay is not None else 0
        except Exception:
            delay = 0

        estimated_time = flight.get("arr_estimated") or flight.get("arr_time")
        estimated_dt = parse_airlabs_time(estimated_time)
        if not estimated_dt:
            estimated_dt = scheduled_dt + timedelta(minutes=delay)

        flight_type = get_flight_type(airline_name, airline_iata)

        valid_list.append({
            "dt": scheduled_dt.timestamp(),
            "time_scheduled": scheduled_dt.strftime("%H:%M"),
            "time_estimated": estimated_dt.strftime("%H:%M"),
            "flight_no": flight_iata,
            "airline": airline_name,
            "city": get_dynamic_city_name(flight),
            "type": flight_type,
            "gate": get_exit_gate(flight_type),
            "status": flight.get("status") or "scheduled",
            "delay": delay
        })

    valid_list = sorted(valid_list, key=lambda x: x["dt"])

    # Codeshare-Duplikate filtern
    unique_flights = []
    seen = set()
    for f in valid_list:
        key = (f["city"], f["time_scheduled"])
        if key in seen:
            continue
        seen.add(key)
        unique_flights.append(f)

    output_data = {
        "updated_at": now.strftime("%d.%M.%Y %H:%M"),
        "source": "Cache" if from_cache else "Live",
        "flights": unique_flights
    }

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"cache.json aktualisiert: {len(unique_flights)} Flüge gespeichert.")

if __name__ == "__main__":
    main()
