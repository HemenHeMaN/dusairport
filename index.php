<?php
// cache.json einlesen
$cacheFile = 'cache.json';
$flights = [];

if (file_exists($cacheFile)) {
    $jsonData = file_get_contents($cacheFile);
    $flights = json_decode($jsonData, true);
}

// Flughafen-Mapping für sprechende Namen
$airportNames = [
    'MUC' => 'München',
    'LHR' => 'London-Heathrow',
    'SMI' => 'Samos',
    'FCO' => 'Rom-Fiumicino',
    'IBZ' => 'Ibiza',
    'WAW' => 'Warschau',
    'CDG' => 'Paris-Charles-de-Gaulle',
    'CPH' => 'Kopenhagen',
    'CFU' => 'Korfu',
    'MAN' => 'Manchester',
    'RHO' => 'Rhodos',
    'BUD' => 'Budapest',
    'PMI' => 'Palma de Mallorca',
    'HAM' => 'Hamburg',
    'OTP' => 'Bukarest',
    'LIN' => 'Mailand-Linate',
    'ALC' => 'Alicante',
    'BHX' => 'Birmingham',
    'FNC' => 'Madeira',
    'FRA' => 'Frankfurt',
    'HER' => 'Iraklion',
    'MAD' => 'Madrid',
    'DLM' => 'Dalaman',
    'AMS' => 'Amsterdam',
    'FAO' => 'Faro',
    'AGP' => 'Malaga',
    'HRG' => 'Hurghada',
    'KGS' => 'Kos',
    'PRG' => 'Prag',
    'AGA' => 'Agadir',
    'BIO' => 'Bilbao',
    'LPA' => 'Gran Canaria',
    'BCN' => 'Barcelona',
    'TFS' => 'Teneriffa Süd',
    'LCA' => 'Larnaka',
    'FUE' => 'Fuerteventura'
];

// Hilfsfunktion für den Namen
function getAirportName($code, $mapping) {
    return isset($mapping[$code]) ? $mapping[$code] : $code;
}
?>
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DUS Ankünfte</title>
<style>
* { box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    margin: 0; padding: 12px; background: #f4f4f9; color: #333;
}
h1 { color: #003366; font-size: 1.3rem; margin: 0 0 5px 0; }
.update-info { font-size: 0.8rem; color: #666; margin-bottom: 12px; line-height: 1.5; }
.filter-container { margin-bottom: 15px; display: flex; gap: 6px; }
.filter-btn {
    flex: 1; padding: 8px; border: none; border-radius: 6px; cursor: pointer;
    font-weight: bold; font-size: 0.85rem; text-align: center; background: #e4e4ed; color: #333; transition: all 0.2s;
}
.btn-all.active { background: #003366; color: white; }
.btn-linie.active { background: #0d47a1; color: white; }
.btn-charter.active { background: #e65100; color: white; }
.flight-card {
    background: #fff; border-radius: 8px; padding: 12px; margin-bottom: 10px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.card-header { display: flex; align-items: center; justify-content: space-between; }
.time-col { font-size: 1.2rem; font-weight: bold; color: #003366; min-width: 72px; }
.flight-time { display: block; }
.main-info { flex-grow: 1; padding: 0 10px; }
.city-name { font-size: 0.95rem; font-weight: bold; color: #222; margin-bottom: 3px; }
.badge-linie {
    background: #e3f2fd; color: #0d47a1; padding: 3px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; display: inline-block;
}
.badge-charter {
    background: #fff3e0; color: #e65100; padding: 3px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; display: inline-block;
}
.delay-badge {
    display: inline-block; margin-top: 3px; padding: 2px 5px; border-radius: 4px; background: #ffebee; color: #c62828; font-size: 0.7rem; font-weight: bold;
}
.card-details {
    margin-top: 10px; font-size: 0.85rem; color: #444; display: block;
}
.detail-divider { border: none; border-top: 1px solid #eee; margin: 8px 0; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.no-flights { text-align: center; padding: 20px; color: #666; background: #fff; border-radius: 8px; }
</style>
</head>
<body>
<h1>DUS Ankünfte</h1>
<div class="update-info">
        Aktuelle Zeit: <b><?php echo date('d.m.Y H:i'); ?> Uhr</b> 🌐 Live<br>
        <?php echo count($flights); ?> Ankünfte aus Cache geladen
    </div>
<div class="filter-container">
    <button class="filter-btn btn-all active" onclick="filterFlights('all', event)">Alle</button>
    <button class="filter-btn btn-linie" onclick="filterFlights('Linie', event)">Linie</button>
    <button class="filter-btn btn-charter" onclick="filterFlights('Charter', event)">Charter</button>
</div>
<div id="flightList">
    <?php if (empty($flights)): ?>
        <div class="no-flights">Keine Flüge im Cache gefunden.</div>
    <?php else: ?>
        <?php foreach ($flights as $flight): 
            // Werte auslesen (passe die Schlüssel je nach Struktur deiner cache.json an, falls sie abweichen)
            $category = isset($flight['type']) ? $flight['type'] : (isset($flight['category']) ? $flight['category'] : 'Linie');
            $time = isset($flight['time']) ? $flight['time'] : '';
            $rawCity = isset($flight['city']) ? $flight['city'] : (isset($flight['from']) ? $flight['from'] : '');
            $cityName = getAirportName($rawCity, $airportNames);
            $flightNo = isset($flight['flight_no']) ? $flight['flight_no'] : (isset($flight['flug_nr']) ? $flight['flug_nr'] : '');
            $airline = isset($flight['airline']) ? $flight['airline'] : '';
            $status = isset($flight['status']) ? $flight['status'] : 'Gelandet';
            $delay = isset($flight['delay']) ? $flight['delay'] : '';
            $gate = isset($flight['gate']) ? $flight['gate'] : '';
            $badgeClass = ($category === 'Charter') ? 'badge-charter' : 'badge-linie';
        ?>
        <div class="flight-card" data-category="<?php echo htmlspecialchars($category); ?>">
            <div class="card-header">
                <div class="time-col">
                    <span class="flight-time"><?php echo htmlspecialchars($time); ?></span>
                    <?php if (!empty($delay)): ?>
                        <span class="delay-badge"><?php echo htmlspecialchars($delay); ?></span>
                    <?php endif; ?>
                </div>
                <div class="main-info">
                    <div class="city-name"><?php echo htmlspecialchars($cityName); ?></div>
                    <div class="gate-info">
                        <span class="<?php echo $badgeClass; ?>"><?php echo htmlspecialchars($gate); ?> (<?php echo htmlspecialchars($category); ?>)</span>
                    </div>
                </div>
            </div>
            <div class="card-details">
                <hr class="detail-divider">
                <div class="detail-grid">
                    <div><strong>Flug-Nr.:</strong> <?php echo htmlspecialchars($flightNo); ?></div>
                    <div><strong>Airline:</strong> <?php echo htmlspecialchars($airline); ?></div>
                    <div><strong>Von:</strong> <?php echo htmlspecialchars($cityName); ?></div>
                    <div><strong>Typ:</strong> <?php echo htmlspecialchars($category); ?></div>
                    <div><strong>Status:</strong> <?php echo htmlspecialchars($status); ?></div>
                </div>
            </div>
        </div>
        <?php endforeach; ?>
    <?php endif; ?>
</div>
<script>
function filterFlights(category, event) {
    const cards = document.querySelectorAll(".flight-card");
    const buttons = document.querySelectorAll(".filter-btn");
    buttons.forEach(function(btn) { btn.classList.remove("active"); });
    if (event && event.target) { event.target.classList.add("active"); }
    cards.forEach(function(card) {
        const cardCategory = card.getAttribute("data-category");
        if (!cardCategory) { return; }
        if (category === "all" || cardCategory === category) {
            card.style.display = "block";
        } else {
            card.style.display = "none";
        }
    });
}
</script>
</body>
</html>