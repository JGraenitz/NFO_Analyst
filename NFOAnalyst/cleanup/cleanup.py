"""Prüft und leert die Windows-Temp-/Cache-Ordner (Benutzer-Temp und
System-Temp). Anders als analyze.py/green.py/parse.py wird dafür nichts aus
data/input oder data/output GELESEN, sondern direkt live auf dem Dateisystem
geschaut, wie voll diese Ordner gerade sind, bzw. sie werden direkt geleert.
Ergebnis wird aber genau wie bei den anderen Modulen zusätzlich als JSON
nach data/output/cleanup/ GESCHRIEBEN (siehe pruefe_cache()/leere_cache()),
damit sich auch eine Cache-Prüfung oder -Leerung nachträglich nachvollziehen
lässt, ohne dass man sie live in der Konsole mitgelesen haben muss.

Bibliotheken:
- pathlib:  für die Ordnerpfade (siehe _temp_ordner()) und den Ausgabepfad.
- tempfile: liefert mit gettempdir() den tatsächlich benutzten Pfad zum
            Benutzer-Temp-Ordner. Zuverlässiger als os.environ["TEMP"] direkt
            zu lesen, weil Windows TEMP an mehreren Stellen setzen kann
            (Benutzer- und Systemvariable) und tempfile die zur Laufzeit
            wirklich verwendete Auflösung übernimmt.
- os:       für os.walk() beim rekursiven Durchsuchen der Ordner und
            os.environ für den Windows-Ordner (WINDIR).
- shutil:   zum Löschen von Unterordnern (shutil.rmtree). Einzelne Dateien
            werden mit Path.unlink() gelöscht.
- json:     zum Schreiben der Berichte (gleiches Muster wie in
            analyze.py/green.py/parse.py/defrag.py, siehe _save_json()).
- datetime: für den Zeitstempel im Bericht (wann geprüft/geleert wurde),
            wichtig gerade hier, weil der Bericht sonst nur ein
            Dateisystem-Zustand ohne erkennbaren Zeitbezug wäre.

Nutzung als Bibliothek:

    from nfoanalyst.cleanup.cleanup import pruefe_cache, leere_cache
    berichte = pruefe_cache()      # speichert zusätzlich cache_pruefung.json
    ergebnisse = leere_cache()     # speichert zusätzlich cache_leerung.json

Beide Funktionen speichern ihren Bericht standardmäßig unter
nfoanalyst/data/output/cleanup/, wie bei den anderen Modulen über einen
optionalen output_dir-Parameter überschreibbar.

Fehler und Lösungen beim Entwickeln:
Viele Dateien im Windows-Temp-Ordner (C:\\Windows\\Temp) und teils auch im
Benutzer-Temp-Ordner sind gerade in Benutzung (von laufenden Programmen
gesperrt) oder brauchen Admin-Rechte zum Löschen. Ein einzelner Fehlschlag
darf die ganze Aktion nicht abbrechen. Deshalb wird pro Datei/Unterordner
einzeln versucht zu löschen, und Fehler (meistens PermissionError, manchmal
allgemeiner OSError) werden nur gezählt statt weitergegeben. Am Ende steht
dann zum Beispiel "1234 Einträge gelöscht, 12 übersprungen (in Benutzung oder
keine Berechtigung)" statt eines Abbruchs nach der ersten gesperrten Datei.
Bei shutil.rmtree() auf einen Unterordner gilt das nur pro Unterordner als
Ganzes: bricht das Löschen mittendrin ab (zum Beispiel weil eine Datei tief
im Unterordner gesperrt ist), zählt der komplette Unterordner als ein
Fehler, auch wenn ein Teil davon schon gelöscht wurde. Für einen einfachen
Cache-Cleaner reicht das, ein Datei-für-Datei-Rückzug wäre hier unnötig
kompliziert.

Zweiter Fund: C:\\Windows\\Temp ohne Administrator-Rechte zu öffnen, verhält
sich in pruefe_cache() und leere_cache() unterschiedlich, wenn man nicht
aufpasst. os.walk() (in _ordnergroesse(), von pruefe_cache() genutzt) gibt
bei einem Zugriffsfehler auf den Startordner selbst standardmäßig einfach
gar nichts zurück, ohne eine Ausnahme zu werfen. Beim ersten Testen hat das
dazu geführt, dass 'cache' für C:\\Windows\\Temp fälschlich "0 Bytes, 0
Dateien" gemeldet hat, obwohl in Wirklichkeit gar kein Zugriff bestand.
pfad.iterdir() (in leere_cache()) wirft dagegen sehr wohl eine echte
PermissionError, und die war ursprünglich gar nicht abgefangen, sondern nur
die Löschversuche für die einzelnen Einträge darin. Das Programm ist beim
Testen deshalb genau an dieser Stelle mit einem Traceback abgestürzt. Jetzt
wird in beiden Funktionen zuerst versucht, den Ordner aufzulisten
(os.listdir()/pfad.iterdir()), und ein Fehlschlag dort wird als eigenes Feld
"zugreifbar": False zurückgegeben, statt als "leer" durchzugehen oder das
Programm abstürzen zu lassen.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

# Pfade werden relativ zu dieser Datei berechnet, nicht zum aktuellen
# Arbeitsverzeichnis (gleiches Prinzip wie in collect/collect.py, parse/parse.py etc.).
CLEANUP_DIR = Path(__file__).resolve().parent
NFOANALYST_DIR = CLEANUP_DIR.parent
# Cleanup braucht (wie defrag) kein vorheriges 'collect'/'parse', deshalb bekommt
# es einen eigenen Ordner unter data/output/ statt unter data/output/report/
# (das ist für die Berichte reserviert, die auf 'analyze' aufbauen).
DEFAULT_OUTPUT_DIR = NFOANALYST_DIR / "data" / "output" / "cleanup"


def _save_json(path: Path, data) -> None:
    """Gleiches Muster wie in analyze.py/green.py/parse.py/defrag.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  gespeichert: {path.parent.name}/{path.name}")


def _fuer_json(eintraege: list[dict]) -> list[dict]:
    """Wandelt "pfad" (ein Path-Objekt) in jedem Eintrag zu einem String um,
    bevor gespeichert wird: Path-Objekte sind nicht JSON-serialisierbar,
    json.dumps() würde sonst mit einer TypeError abbrechen. Die von
    pruefe_cache()/leere_cache() ZURÜCKGEGEBENE Liste bleibt davon
    unberührt (dort ist ein Path-Objekt weiterhin praktischer), nur die für
    die Datei bestimmte Kopie wird umgewandelt.
    """
    return [{**eintrag, "pfad": str(eintrag["pfad"])} for eintrag in eintraege]


def _temp_ordner() -> list[dict]:
    """Baut die Liste der zu prüfenden Ordner bei jedem Aufruf neu, statt sie
    als Modul-Konstante beim Import einmal festzulegen. So macht allein das
    Importieren dieses Moduls noch keinen Dateisystemzugriff, sondern erst
    der tatsächliche Aufruf von pruefe_cache()/leere_cache()."""
    windows_ordner = Path(os.environ.get("WINDIR", r"C:\Windows"))
    return [
        {"name": "Benutzer-Temp", "pfad": Path(tempfile.gettempdir())},
        {"name": "Windows-Temp (System, evtl. Admin nötig)", "pfad": windows_ordner / "Temp"},
    ]


def _ordnergroesse(pfad: Path) -> tuple[int, int, int]:
    """Läuft rekursiv durch pfad und summiert die Dateigrößen auf.
    Gibt (groesse_bytes, anzahl_dateien, anzahl_fehler) zurück. Dateien, auf
    die kein Zugriff möglich ist (zum Beispiel weil sie gerade in Benutzung
    sind), werden übersprungen und nur gezählt, sonst würde eine einzelne
    gesperrte Datei die ganze Größenermittlung mit einer OSError abbrechen.
    """
    groesse = 0
    anzahl_dateien = 0
    anzahl_fehler = 0
    for wurzel, _unterordner, dateien in os.walk(pfad):
        for name in dateien:
            try:
                groesse += (Path(wurzel) / name).stat().st_size
                anzahl_dateien += 1
            except OSError:
                anzahl_fehler += 1
    return groesse, anzahl_dateien, anzahl_fehler


def pruefe_cache(output_dir: Path | str | None = None) -> list[dict]:
    """Ermittelt Größe und Dateianzahl der Temp-/Cache-Ordner, ohne etwas zu
    löschen. Ein Eintrag pro Ordner aus _temp_ordner(). Speichert das
    Ergebnis zusätzlich als cache_pruefung.json unter output_dir (Standard:
    nfoanalyst/data/output/cleanup/), gleiches Muster wie bei den anderen
    Modulen (analyze.py/green.py/defrag.py)."""
    berichte = []
    for eintrag in _temp_ordner():
        pfad = eintrag["pfad"]
        if not pfad.exists():
            berichte.append({
                **eintrag, "existiert": False, "zugreifbar": False,
                "groesse_bytes": 0, "anzahl_dateien": 0, "anzahl_fehler": 0,
            })
            continue

        # Erst getrennt prüfen, ob sich der Ordner überhaupt auflisten lässt,
        # bevor _ordnergroesse() (also os.walk()) läuft: os.walk() gibt bei
        # einem Zugriffsfehler auf den STARTORDNER selbst (anders als bei
        # Unterordnern weiter unten) standardmäßig einfach gar nichts zurück,
        # ohne einen Fehler zu melden. Ohne diese Extra-Prüfung hätte
        # C:\Windows\Temp ohne Admin-Rechte fälschlich als "leer" (0 Bytes)
        # gegolten, obwohl in Wirklichkeit kein Zugriff möglich war. Genau das
        # ist beim Testen von leere_cache() auch aufgefallen: dort flog beim
        # gleichen Ordner eine echte PermissionError, hier wurde sie vorher
        # verschluckt.
        try:
            os.listdir(pfad)
        except OSError:
            berichte.append({
                **eintrag, "existiert": True, "zugreifbar": False,
                "groesse_bytes": 0, "anzahl_dateien": 0, "anzahl_fehler": 0,
            })
            continue

        groesse, anzahl_dateien, anzahl_fehler = _ordnergroesse(pfad)
        berichte.append({
            **eintrag, "existiert": True, "zugreifbar": True,
            "groesse_bytes": groesse,
            "anzahl_dateien": anzahl_dateien,
            "anzahl_fehler": anzahl_fehler,
        })

    zusammenfassung = {
        "geprueft_am": datetime.now().isoformat(timespec="seconds"),
        "anzahl_ordner": len(berichte),
        "gesamt_bytes": sum(b["groesse_bytes"] for b in berichte),
        "gesamt_dateien": sum(b["anzahl_dateien"] for b in berichte),
        "gesamt_fehler": sum(b["anzahl_fehler"] for b in berichte),
    }
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    _save_json(
        output_dir / "cache_pruefung.json",
        {"zusammenfassung": zusammenfassung, "ordner": _fuer_json(berichte)},
    )
    return berichte


def leere_cache(output_dir: Path | str | None = None) -> list[dict]:
    """Löscht den INHALT der Temp-/Cache-Ordner, nicht die Ordner selbst
    (Windows erwartet, dass zum Beispiel C:\\Windows\\Temp als Ordner
    weiter existiert). Pro Datei/Unterordner wird einzeln gelöscht, Fehler
    werden nur gezählt statt die ganze Aktion abzubrechen (siehe
    Fehler-und-Lösungen-Abschnitt oben im Modul-Docstring). Speichert das
    Ergebnis zusätzlich als cache_leerung.json unter output_dir (Standard:
    nfoanalyst/data/output/cleanup/), inklusive Zeitstempel, damit sich eine
    frühere Leerung auch im Nachhinein nachvollziehen lässt."""
    ergebnisse = []
    for eintrag in _temp_ordner():
        pfad = eintrag["pfad"]

        if not pfad.exists():
            ergebnisse.append({
                **eintrag, "existiert": False, "zugreifbar": False,
                "geloescht_bytes": 0, "geloescht_eintraege": 0, "anzahl_fehler": 0,
            })
            continue

        # Ohne diesen Versuch-zuerst-Aufzulisten-Schritt würde pfad.iterdir()
        # weiter unten in der for-Schleife mit einer nicht abgefangenen
        # PermissionError abstürzen, sobald man ohne Admin-Rechte versucht,
        # C:\Windows\Temp zu öffnen. Das ist beim Testen auch tatsächlich so
        # passiert (siehe Fehler-und-Lösungen-Abschnitt oben im Modul-Docstring).
        try:
            kinder = list(pfad.iterdir())
        except OSError:
            ergebnisse.append({
                **eintrag, "existiert": True, "zugreifbar": False,
                "geloescht_bytes": 0, "geloescht_eintraege": 0, "anzahl_fehler": 0,
            })
            continue

        geloescht_bytes = 0
        geloescht_eintraege = 0
        anzahl_fehler = 0
        for kind in kinder:
            try:
                if kind.is_dir() and not kind.is_symlink():
                    groesse, _anzahl, _fehler = _ordnergroesse(kind)
                    shutil.rmtree(kind)
                else:
                    groesse = kind.stat().st_size
                    kind.unlink()
                geloescht_bytes += groesse
                geloescht_eintraege += 1
            except OSError:
                # Häufigster Fall hier (anders als oben beim Auflisten): eine
                # einzelne Datei/ein einzelner Unterordner ist gerade in
                # Benutzung (unter Windows als "WinError 32" bekannt). Wird
                # nur gezählt, nicht weiter behandelt.
                anzahl_fehler += 1

        ergebnisse.append({
            **eintrag, "existiert": True, "zugreifbar": True,
            "geloescht_bytes": geloescht_bytes,
            "geloescht_eintraege": geloescht_eintraege,
            "anzahl_fehler": anzahl_fehler,
        })

    zusammenfassung = {
        "durchgefuehrt_am": datetime.now().isoformat(timespec="seconds"),
        "anzahl_ordner": len(ergebnisse),
        "geloescht_bytes_gesamt": sum(e["geloescht_bytes"] for e in ergebnisse),
        "geloescht_eintraege_gesamt": sum(e["geloescht_eintraege"] for e in ergebnisse),
        "anzahl_fehler_gesamt": sum(e["anzahl_fehler"] for e in ergebnisse),
    }
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    _save_json(
        output_dir / "cache_leerung.json",
        {"zusammenfassung": zusammenfassung, "ordner": _fuer_json(ergebnisse)},
    )
    return ergebnisse
