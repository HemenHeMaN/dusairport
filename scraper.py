python
from datetime import datetime, timedelta
import urllib.request
import urllib.parse
import json
import os
import time
import html

# ============================================================
# AIRLABS API
# ============================================================

API_KEY = os.environ.get("AIRLABS_API_KEY")

AIRPORT = "DUS"

CACHE_FILE = "cache.json"
CACHE_DURATION = 300  # 5 Minuten

# Zeitfenster
MINUTES_PAST = 30
HOURS_FUTURE = 5


# ============================================================
# CHARTER-ZUORDNUNG
# ============================================================
#
# AirLabs liefert nicht bei jedem Flug ein direktes
# "Linie/Charter"-Feld.
#
# Deshalb können hier einzelne Airlines/Flüge als Charter
# eingetragen werden.
#
# Wichtig:
# Eine Airline kann sowohl Linien- als auch Charterflüge
# durchführen. Deshalb ist die Liste bewusst anpassbar.
#

CHARTER_AIRLINES = [
    "condor",
    "tuifly",
    "corendon",
    "freebird",
    "smartlynx",
    "eurowings discover",
]

# Optional: einzelne Flugnummern können hier zusätzlich
# als Charter definiert werden.
#
# Beispiel:
# "XQ989",
# "XC1234",

CHARTER_FLIGHTS = [
]


# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def clean_text(value, fallback="Unbekannt"):
    """
    Bereinigt Text und verhindert HTML-Probleme.
    """
    if value is None:
        return fallback

    value = str(value).strip()

    if not value:
        return fallback

    return html.escape(value)


def get_flight_type(airline_name, flight_iata):
    """
    Bestimmt Linie oder Charter.

    Einzelne Flugnummern haben Vorrang.
    Danach wird die Airline geprüft.
    """

    airline_lower = str(airline_name or "").lower()
    flight_upper = str(flight_iata or "").upper().replace(" ", "")

    # Einzelner Flug als Charter
    for charter_flight in CHARTER_FLIGHTS:
        if flight_upper == charter_flight.upper().replace(" ", ""):
            return "Charter"

    # Airline als Charter
    for keyword in CHARTER_AIRLINES:
        if keyword in airline_lower:
            return "Charter"

    return "Linie"


def get_exit_gate(flight_type):
    """
    Ausgang entsprechend Linie / Charter.
    """

    if flight_type == "Charter":
        return "Ausgang 4 (Charter)"

    return "Ausgang 1 (Linie)"


def parse_airlabs_time(time_string):
    """
    AirLabs liefert normalerweise lokale Flughafenzeit.

    Erwartetes Format:
    2026-09-25 14:35

    Wir behandeln diese Zeit als lokale DUS-Zeit.
    """

    if not time_string:
        return None

    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(str(time_string), fmt)
        except ValueError:
            pass

    return None


# ============================================================
# AIRLABS ABFRAGE
# ============================================================

def fetch_live_flights():
    """
    Holt aktuelle Ankünfte von Düsseldorf über AirLabs.

    AirLabs Free kann eine begrenzte Anzahl Ergebnisse
    pro Anfrage liefern. Wir verwenden daher limit=50.

    Zusätzlich wird ein 5-Minuten-Cache verwendet.
    """

    # --------------------------------------------------------
    # CACHE PRÜFEN
    # --------------------------------------------------------

    if os.path.exists(CACHE_FILE):

        file_age = time.time() - os.path.getmtime(CACHE_FILE)

        if file_age < CACHE_DURATION:

            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)

                return cached_data, True

            except Exception:
                pass

    # --------------------------------------------------------
    # AIRLABS URL
    # --------------------------------------------------------

    params = {
        "api_key": API_KEY,
        "arr_iata": AIRPORT,
        "limit": 50
    }

    query_string = urllib.parse.urlencode(params)

    api_url = (
        "https://airlabs.co/api/v9/schedules?"
        + query_string
    )

    try:

        request = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": "DUS-Flight-Scraper/1.0"
            }
        )

        with urllib.request.urlopen(request, timeout=10) as response:

            raw_data = response.read().decode("utf-8")

        data = json.loads(raw_data)

        # ----------------------------------------------------
        # AIRLABS FEHLER
        # ----------------------------------------------------

        if "error" in data:

            print("AirLabs Fehler:")
            print(json.dumps(data["error"], indent=2, ensure_ascii=False))

            raise Exception("AirLabs API Fehler")

        flights = data.get("response", [])

        if not flights:

            print("AirLabs: Keine Flüge erhalten.")

            raise Exception("Keine Flüge")

        # ----------------------------------------------------
        # CACHE SPEICHERN
        # ----------------------------------------------------

        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                flights,
                f,
                ensure_ascii=False,
                indent=2
            )

        return flights, False

    except Exception as e:

        print("Fehler beim Abrufen von AirLabs:")
        print(e)

        # ----------------------------------------------------
        # FALLBACK CACHE
        # ----------------------------------------------------

        if os.path.exists(CACHE_FILE):

            try:

                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cached_data = json.load(f)

                print("Verwende alten Cache.")

                return cached_data, True

            except Exception:
                pass

        return [], False


# ============================================================
# HTML ERSTELLEN
# ============================================================

def update_html():

    now = datetime.now()

    time_min = now - timedelta(minutes=MINUTES_PAST)
    time_max = now + timedelta(hours=HOURS_FUTURE)

    raw_flights, from_cache = fetch_live_flights()

    valid_list = []

    # ========================================================
    # FLÜGE VERARBEITEN
    # ========================================================

    for flight in raw_flights:

        # ----------------------------------------------------
        # Flugnummer
        # ----------------------------------------------------

        flight_iata = (
            flight.get("flight_iata")
            or flight.get("flight_number")
            or ""
        )

        if not flight_iata:
            continue

        # ----------------------------------------------------
        # Airline
        # ----------------------------------------------------

        airline_name = (
            flight.get("airline_name")
            or flight.get("airline_iata")
            or "Unbekannt"
        )

        # ----------------------------------------------------
        # Ankunftszeit
        # ----------------------------------------------------

        arrival_time = (
            flight.get("arr_estimated")
            or flight.get("arr_time")
            or flight.get("arr_scheduled")
            or flight.get("arr_actual")
        )

        flight_dt = parse_airlabs_time(arrival_time)

        if not flight_dt:
            continue

        # ----------------------------------------------------
        # Zeitfenster
        # ----------------------------------------------------

        if not (time_min <= flight_dt <= time_max):
            continue

        # ----------------------------------------------------
        # Flugschule / Training ausschließen
        # ----------------------------------------------------

        airline_lower = str(airline_name).lower()

        if (
            "flugschule" in airline_lower
            or "training" in airline_lower
            or "flight school" in airline_lower
        ):
            continue

        # ----------------------------------------------------
        # Herkunft
        # ----------------------------------------------------

        departure_iata = (
            flight.get("dep_iata")
            or ""
        )

        departure_city = (
            flight.get("dep_city")
            or flight.get("dep_name")
            or departure_iata
            or "Unbekannt"
        )

        # ----------------------------------------------------
        # Terminal
        # ----------------------------------------------------

        terminal_raw = (
            flight.get("arr_terminal")
            or flight.get("terminal")
        )

        if terminal_raw:
            terminal = f"Terminal {terminal_raw}"
        else:
            terminal = "Terminal A/B"

        # ----------------------------------------------------
        # Flugstatus
        # ----------------------------------------------------

        status = (
            flight.get("status")
            or "scheduled"
        )

        # ----------------------------------------------------
        # Verspätung
        # ----------------------------------------------------

        delay = flight.get("arr_delayed")

        # AirLabs kann die Verspätung in Minuten liefern.
        try:
            if delay is not None:
                delay = int(delay)
        except Exception:
            delay = None

        # ----------------------------------------------------
        # Linie / Charter
        # ----------------------------------------------------

        flight_type = get_flight_type(
            airline_name,
            flight_iata
        )

        gate = get_exit_gate(flight_type)

        # ----------------------------------------------------
        # Eintrag
        # ----------------------------------------------------

        valid_list.append({

            "dt": flight_dt,

            "time_formatted":
                flight_dt.strftime("%H:%M"),

            "flight_no":
                flight_iata,

            "airline":
                airline_name,

            "city":
                departure_city,

            "dep_iata":
                departure_iata,

            "terminal":
                terminal,

            "type":
                flight_type,

            "gate":
                gate,

            "status":
                status,

            "delay":
                delay
        })

    # ========================================================
    # SORTIEREN
    # ========================================================

    valid_list = sorted(
        valid_list,
        key=lambda x: x["dt"]
    )

    # ========================================================
    # DOPPELTE FLÜGE ENTFERNEN
    # ========================================================

    unique_flights = []

    seen = set()

    for flight in valid_list:

        unique_key = (
            flight["flight_no"],
            flight["dt"].strftime("%Y-%m-%d %H:%M")
        )

        if unique_key in seen:
            continue

        seen.add(unique_key)

        unique_flights.append(flight)

    valid_list = unique_flights

    # ========================================================
    # CARDS
    # ========================================================

    cards_html = ""

    for f in valid_list:

        if f["type"] == "Linie":
            badge_class = "badge-linie"
        else:
            badge_class = "badge-charter"

        # ----------------------------------------------------
        # Verspätung
        # ----------------------------------------------------

        delay_html = ""

        if f["delay"] is not None and f["delay"] > 0:

            delay_html = (
                f'<span class="delay-badge">'
                f'+{f["delay"]} Min.'
                f'</span>'
            )

        # ----------------------------------------------------
        # Status
        # ----------------------------------------------------

        status_translation = {

            "scheduled": "Geplant",
            "en-route": "Unterwegs",
            "landed": "Gelandet",
            "cancelled": "Storniert",
            "incident": "Störung",
            "diverted": "Umgeleitet"
        }

        status_text = status_translation.get(
            str(f["status"]).lower(),
            f["status"]
        )

        cards_html += f"""
        <div
            class="flight-card"
            data-category="{f['type']}"
            onclick="toggleCard(this)"
        >

            <div class="card-header">

                <div class="time-col">
                    <span class="flight-time">
                        {f['time_formatted']}
                    </span>
                    {delay_html}
                </div>

                <div class="main-info">

                    <div class="city-name">
                        {clean_text(f['city'])}
                    </div>

                    <div class="gate-info">

                        <span class="{badge_class}">
                            {clean_text(f['gate'])}
                        </span>

                    </div>

                </div>

                <div class="toggle-icon">
                    ▼
                </div>

            </div>

            <div class="card-details">

                <hr class="detail-divider">

                <div class="detail-grid">

                    <div>
                        <strong>Flug-Nr.:</strong>
                        {clean_text(f['flight_no'])}
                    </div>

                    <div>
                        <strong>Airline:</strong>
                        {clean_text(f['airline'])}
                    </div>

                    <div>
                        <strong>Von:</strong>
                        {clean_text(f['dep_iata'])}
                    </div>

                    <div>
                        <strong>Terminal:</strong>
                        {clean_text(f['terminal'])}
                    </div>

                    <div>
                        <strong>Status:</strong>
                        {clean_text(status_text)}
                    </div>

                    <div>
                        <strong>Typ:</strong>
                        {clean_text(f['type'])}
                    </div>

                </div>

            </div>

        </div>
        """

    # ========================================================
    # KEINE FLÜGE
    # ========================================================

    if not valid_list:

        cards_html = """
        <div class="no-flights">
            Keine Flüge im aktuellen Zeitfenster gefunden.
        </div>
        """

    # ========================================================
    # CACHE STATUS
    # ========================================================

    if from_cache:
        cache_status_text = "⚡ Cache"
    else:
        cache_status_text = "🌐 Live"

    update_info_text = f"""
        Aktuelle Zeit:
        <b>{now.strftime('%d.%m.%Y %H:%M')} Uhr</b>
        {cache_status_text}
        <br>

        Zeitfenster:
        {time_min.strftime('%H:%M')} Uhr
        bis
        {time_max.strftime('%H:%M')} Uhr
        <br>

        {len(valid_list)} Ankünfte
    """

    # ========================================================
    # HTML
    # ========================================================

    full_html = f"""<!DOCTYPE html>

<html lang="de">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>DUS Ankünfte</title>

<style>

* {{
    box-sizing: border-box;
}}

body {{

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Roboto,
        Helvetica,
        Arial,
        sans-serif;

    margin: 0;

    padding: 12px;

    background: #f4f4f9;

    color: #333;
}}

h1 {{

    color: #003366;

    font-size: 1.3rem;

    margin:
        0 0 5px 0;
}}

.update-info {{

    font-size: 0.8rem;

    color: #666;

    margin-bottom: 12px;

    line-height: 1.5;
}}

.filter-container {{

    margin-bottom: 15px;

    display: flex;

    gap: 6px;
}}

.filter-btn {{

    flex: 1;

    padding: 8px;

    border: none;

    border-radius: 6px;

    cursor: pointer;

    font-weight: bold;

    font-size: 0.85rem;

    text-align: center;

    background: #e4e4ed;

    color: #333;

    transition: all 0.2s;
}}

.btn-all.active {{

    background: #003366;

    color: white;
}}

.btn-linie.active {{

    background: #0d47a1;

    color: white;
}}

.btn-charter.active {{

    background: #e65100;

    color: white;
}}

.flight-card {{

    background: #fff;

    border-radius: 8px;

    padding: 12px;

    margin-bottom: 10px;

    box-shadow:
        0 1px 3px rgba(0,0,0,0.1);

    cursor: pointer;

    transition:
        background 0.1s;
}}

.flight-card:active {{

    background: #fafafa;
}}

.card-header {{

    display: flex;

    align-items: center;

    justify-content:
        space-between;
}}

.time-col {{

    font-size: 1.2rem;

    font-weight: bold;

    color: #003366;

    min-width: 72px;
}}

.flight-time {{

    display: block;
}}

.main-info {{

    flex-grow: 1;

    padding: 0 10px;
}}

.city-name {{

    font-size: 0.95rem;

    font-weight: bold;

    color: #222;

    margin-bottom: 3px;
}}

.badge-linie {{

    background: #e3f2fd;

    color: #0d47a1;

    padding: 3px 6px;

    border-radius: 4px;

    font-size: 0.75rem;

    font-weight: bold;

    display: inline-block;
}}

.badge-charter {{

    background: #fff3e0;

    color: #e65100;

    padding: 3px 6px;

    border-radius: 4px;

    font-size: 0.75rem;

    font-weight: bold;

    display: inline-block;
}}

.delay-badge {{

    display: inline-block;

    margin-top: 3px;

    padding: 2px 5px;

    border-radius: 4px;

    background: #ffebee;

    color: #c62828;

    font-size: 0.7rem;

    font-weight: bold;
}}

.toggle-icon {{

    font-size: 0.8rem;

    color: #888;

    transition:
        transform 0.3s;
}}

.flight-card.open .toggle-icon {{

    transform:
        rotate(180deg);
}}

.card-details {{

    display: none;

    margin-top: 10px;

    font-size: 0.85rem;

    color: #444;
}}

.flight-card.open .card-details {{

    display: block;
}}

.detail-divider {{

    border: none;

    border-top:
        1px solid #eee;

    margin: 8px 0;
}}

.detail-grid {{

    display: grid;

    grid-template-columns:
        1fr 1fr;

    gap: 8px;
}}

.no-flights {{

    text-align: center;

    padding: 20px;

    color: #666;

    background: #fff;

    border-radius: 8px;
}}

</style>

</head>

<body>

<h1>
    DUS Ankünfte
</h1>

<div class="update-info">

    {update_info_text}

</div>

<div class="filter-container">

    <button
        class="filter-btn btn-all active"
        onclick="filterFlights('all', event)"
    >
        Alle
    </button>

    <button
        class="filter-btn btn-linie"
        onclick="filterFlights('Linie', event)"
    >
        Linie
    </button>

    <button
        class="filter-btn btn-charter"
        onclick="filterFlights('Charter', event)"
    >
        Charter
    </button>

</div>

<div id="flightList">

    {cards_html}

</div>

<script>

function toggleCard(cardElement) {{

    cardElement.classList.toggle("open");

}}


function filterFlights(category, event) {{

    const cards =
        document.querySelectorAll(".flight-card");

    const buttons =
        document.querySelectorAll(".filter-btn");

    buttons.forEach(function(btn) {{

        btn.classList.remove("active");

    }});

    if (
        event &&
        event.target
    ) {{

        event.target.classList.add("active");

    }}

    cards.forEach(function(card) {{

        const cardCategory =
            card.getAttribute("data-category");

        if (!cardCategory) {{

            return;

        }}

        if (
            category === "all" ||
            cardCategory === category
        ) {{

            card.style.display = "block";

        }} else {{

            card.style.display = "none";

        }}

    }});

}}

</script>

</body>

</html>
"""

    # ========================================================
    # INDEX.HTML SPEICHERN
    # ========================================================

    with open(
        "index.html",
        "w",
        encoding="utf-8"
    ) as file:

        file.write(full_html)

    print(
        f"HTML aktualisiert: "
        f"{len(valid_list)} Flüge"
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    update_html()
