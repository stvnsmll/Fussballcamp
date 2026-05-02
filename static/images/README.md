# static/images/

Ablageort für das Camp-Logo und sonstige Grafiken.

## Logo-Datei ablegen

Legen Sie Ihre Logo-Datei in diesem Ordner ab und setzen Sie dann
die passende Umgebungsvariable in der `.env`-Datei.

### Empfohlene Dateinamen

| Datei                | Verwendung                                       |
|----------------------|--------------------------------------------------|
| `logo.png`           | Standard-Logo (für helle Hintergründe)           |
| `logo-white.png`     | Helles Logo für dunkle Hintergründe (Navbar)     |
| `logo.svg`           | Vektorversion empfohlen für beste Qualität       |

### .env-Einstellungen

```env
# Nur eine oder beide Zeilen setzen — der Rest wird automatisch angepasst.

# Standard-Logo (für helle Hintergründe, z. B. Startseite):
CAMP_LOGO_PATH=images/logo.png

# Logo für dunkle Hintergründe (Navbar, Login-Seite):
# Wenn nicht gesetzt, wird CAMP_LOGO_PATH mit CSS-Filter invertiert.
CAMP_LOGO_DARK_PATH=images/logo-white.png
```

### Technische Anforderungen

- **Format:** PNG (mit Transparenz) oder SVG empfohlen
- **Höhe:** mindestens 120 px (wird in der Navbar auf 38–64 px skaliert)
- **Hintergrund:** transparent — die Seite hat eigene Hintergrundfarben
- **Keine Whitespace-Ränder** im Bild — das Padding übernimmt das Template

### Solange kein Logo vorhanden ist

Die App zeigt automatisch ein Platzhalter-Logo (`placeholder/logo-placeholder.svg`).
Es erscheint überall, wo das echte Logo angezeigt werden würde.
Das Platzhalter-Bild wird nie für Endnutzer sichtbar sein,
sobald `CAMP_LOGO_PATH` gesetzt ist.

---

## Weitere Grafiken

Beliebige Bilder können hier abgelegt und in Templates eingebunden werden:

```html
<img src="{{ url_for('static', filename='images/mein-bild.jpg') }}" alt="…">
```

## Git

Die Dateien in diesem Ordner (außer `placeholder/`) sind in `.gitignore`
eingetragen — Logos können urheberrechtlich geschützt sein.
Der Ordner selbst bleibt über `.gitkeep` erhalten.
