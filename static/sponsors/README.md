# static/sponsors/

Sponsor-Logos, organisiert nach Jahr.

## Struktur

```
sponsors/
  2025/
    sponsor-name.png
    anderer-sponsor.png
  2026/
    neuer-sponsor.png
```

## Sponsor hinzufügen — Schritt für Schritt

**1. Logo-Datei ablegen**

```
static/sponsors/2026/beispiel-gmbh.png
```

**2. Eintrag in `static/data/sponsors.json` ergänzen**

```json
[
  {
    "name": "Beispiel GmbH",
    "logo_file": "2026/beispiel-gmbh.png",
    "url": "https://www.beispiel-gmbh.de",
    "tagline": "Offizieller Partner",
    "year": 2026
  }
]
```

Die App filtert automatisch nach dem aktuellen Kalenderjahr —
alte Sponsoren erscheinen nicht mehr, ohne dass die Datei bearbeitet werden muss.

**3. Kein Neustart nötig**

`sponsors.json` wird bei jedem Seitenaufruf neu eingelesen.
Änderungen sind sofort sichtbar.

---

## JSON-Felder

| Feld         | Pflicht | Beschreibung                                      |
|--------------|---------|---------------------------------------------------|
| `name`       | ✓       | Anzeigename / Alt-Text                            |
| `logo_file`  |         | Relativer Pfad ab `static/sponsors/`              |
| `url`        |         | Klick-Ziel (öffnet in neuem Tab)                  |
| `tagline`    |         | Kleiner Text unter dem Logo (z. B. "Hauptsponsor")|
| `year`       |         | Nur Sponsoren dieses Jahres werden angezeigt      |

Fehlt `logo_file`, wird automatisch ein Platzhalter-SVG angezeigt.
Fehlt das Bild auf dem Server, fällt es ebenfalls auf den Platzhalter zurück.

---

## Technische Anforderungen für Logos

- **Format:** PNG mit Transparenz oder SVG
- **Größe:** ~400 × 150 px, max. 1 MB
- **Hintergrund:** transparent — der Hintergrund der Sponsor-Leiste ist hell (`#f4f3ed`)
- Logos werden auf 48 px Höhe skaliert und mit leichtem Graustufen-Filter angezeigt

---

## Wo erscheinen Sponsoren?

- Startseite (`/`) — Sponsor-Leiste unter den Feature-Abschnitten
- Anmeldeseite (`/auth/anmelden`) — kleiner Strip unterhalb des Login-Formulars

Wenn `sponsors.json` leer ist oder keine Sponsoren für das aktuelle Jahr enthält,
wird der gesamte Bereich ausgeblendet — keine leeren Boxen.
