"""Stumpfe Kritikalitäts-Einschätzung des Rechners auf Basis der von parse.py
erzeugten JSON-Daten: wie alt, signiert und aktuell sind die installierten
Treiber, und wie voll sind die Laufwerke. Ergänzt das Ganze um die Green-IT-
Sicht aus green/green.py (CO2-/Energieeinschätzung von Autostart und
Energieschema), damit ein Nutzer auf einen Blick sieht, was an dem Rechner
kritisch ist.

Nutzung als Bibliothek (einzeiliger Aufruf aus einem anderen Programm):

    from nfoanalyst.analyze.analyze import erstelle_bericht
    bericht = erstelle_bericht()

erstelle_bericht() speichert mehrere kleinere Berichte (treiber.json,
laufwerke.json, gesamtbericht.json) unter nfoanalyst/data/output/report/ und
gibt zusätzlich alles als dict zurück, zum Beispiel für eine spätere CLI-Anzeige.

Wichtig: Das ist bewusst eine grobe, regelbasierte Einschätzung (feste
Schwellenwerte für "warnung"/"kritisch"), keine tiefgehende Sicherheits-
oder Zuverlässigkeitsanalyse.

Fehler und Lösungen beim Entwickeln:
Anfangs war der Plan, driverquery /si (Signaturstatus) und pnputil /enum-drivers
(Alter und Version) über den INF-Namen zu einer einzigen Treiberliste
zusammenzuführen. Beim Testen kamen dabei aber nur für ungefähr ein Drittel
der Einträge überhaupt Treffer zustande, weil beide Befehle nicht dieselbe
Menge an Treibern auflisten (der Treiber-Store von pnputil enthält zum
Beispiel auch reine Erweiterungspakete, während driverquery /si dafür fest
eingebaute Windows-Treiber wie disk.inf oder usb.inf mitzählt, die im Store
gar nicht auftauchen). Ein erzwungener Join hätte also zwei Drittel der Daten
stillschweigend weggelassen. Die Lösung war, beide Listen getrennt und
vollständig zu behalten, statt sie unvollständig zu verknüpfen (siehe
analysiere_treiber()).

Zweiter Fund beim Testen: Ein echter Intel-Treiber (raptorlakepch-ssystem.inf)
gibt in seiner INF-Datei als DriverVer tatsächlich "07/18/1968" an. Kein
Parsing-Fehler auf unserer Seite, sondern eine kuriose (falsche) Angabe des
Herstellers selbst, die dann als 58 Jahre alter Treiber gemeldet wird. Wurde
bewusst nicht herausgefiltert, weil es eine ehrliche Wiedergabe der Quelldaten
ist und selbst schon ein interessanter Befund sein kann.

Dritter Punkt: Die Treiberliste sollte später auch die deutlich vollständigere
msinfo32-Kategorie "Systemtreiber" (432 Einträge statt nur 116 im
pnputil-Treiber-Store) mit abdecken, ohne Duplikate zu erzeugen. Ein exakter
Abgleich ist aus demselben Grund wie oben nicht möglich (unterschiedliche
Namensschemata), deshalb ist auch dieser Abgleich in _ergaenze_msinfo_treiber()
nur ein Best-Effort-Namensvergleich mit rund 15 Prozent Trefferquote. Die
msinfo32-Einträge, die dabei übrig bleiben, werden ohne Datum in dieselbe
Liste eingemischt, statt sie zu verwerfen.
"""

from __future__ import annotations

# json:      zum Einlesen der parse.py-Ausgaben und zum Schreiben der eigenen Berichte.
# re:        für die zwei Stellen, an denen Text nach einem festen Muster ausgewertet wird
#            (Treiberversion "MM/DD/YYYY X.X.X.X" und die Byte-Zahl in Größenangaben).
# sys:       nur für den sys.path-Eingriff weiter unten, damit green/green.py importierbar ist.
# datetime:  date.today() und date(...) fürs Treiberalter (heute minus Datum aus der INF-Datei).
# pathlib:   für alle Ein-/Ausgabepfade.
import json
import re
import sys
from datetime import date
from pathlib import Path

ANALYZE_DIR = Path(__file__).resolve().parent
NFOANALYST_DIR = ANALYZE_DIR.parent
DEFAULT_INPUT_DIR = NFOANALYST_DIR / "data" / "output"
DEFAULT_OUTPUT_DIR = NFOANALYST_DIR / "data" / "output" / "report"

# green/green.py liegt als Geschwister-Ordner neben analyze/, nicht als
# Unterordner. Je nachdem, wie analyze.py geladen wird (direkt als Skript oder
# als nfoanalyst.analyze.analyze-Modul), ist nfoanalyst/ nicht automatisch auf
# sys.path. Das wird hier vorne sichergestellt, statt es beim Import in
# erstelle_bericht() dem Zufall zu überlassen.
if str(NFOANALYST_DIR) not in sys.path:
    sys.path.insert(0, str(NFOANALYST_DIR))

# Ab wann ein Treiber als "warnung" bzw. "kritisch" gilt. Reine Daumenregel
# (verbreitete IT-Hygiene-Empfehlung liegt zwischen 3 und 5 Jahren), über die
# Parameter von erstelle_bericht()/analysiere_treiber() anpassbar. Funktioniert
# nach demselben Muster wie STANDARD_KRITISCH_PROZENT/STANDARD_WARNUNG_PROZENT
# weiter unten bei den Laufwerken.
STANDARD_WARNUNG_JAHRE = 3.0
STANDARD_KRITISCH_JAHRE = 5.0

# Unterhalb dieser freien Kapazität (in Prozent) gilt ein Laufwerk als
# "kritisch", darunter als "warnung". Ebenfalls eine Daumenregel.
STANDARD_KRITISCH_PROZENT = 10.0
STANDARD_WARNUNG_PROZENT = 20.0


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------

def _load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  gespeichert: {path.parent.name}/{path.name}")


# --------------------------------------------------------------------------
# Treiber: Alter, Version, Signatur
# --------------------------------------------------------------------------

# pnputil gibt Treiberversionen im Format "MM/DD/YYYY X.X.X.X" aus (US-Format,
# so wie es in der INF-Datei als DriverVer steht), unabhängig von der Systemsprache.
_TREIBERVERSION_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(.+)$")


def _parse_treiberversion(value: str | None) -> tuple[date | None, str | None]:
    # Reihenfolge beim Auslesen ist (monat, tag, jahr), nicht (tag, monat, jahr).
    # _TREIBERVERSION_RE.match() liefert die Gruppen in der Reihenfolge, in der
    # das US-Format "MM/DD/YYYY" sie hat. Leicht zu verwechseln mit dem
    # deutschen TT.MM.JJJJ, das an anderen Stellen im Projekt vorkommt.
    if not value:
        return None, None
    match = _TREIBERVERSION_RE.match(value.strip())
    if not match:
        return None, value.strip()
    monat, tag, jahr, version = match.groups()
    try:
        return date(int(jahr), int(monat), int(tag)), version
    except ValueError:
        return None, version


def _bewerte_treiber_alter(alter_jahre: float | None, warnung_jahre: float, kritisch_jahre: float) -> str:
    """Ordnet ein Treiberalter in eine von vier Stufen ein, nach demselben
    Muster wie analysiere_laufwerke() das mit dem freien Speicherplatz macht:
    "unbekannt" ohne Datum, sonst "kritisch" ab kritisch_jahre, "warnung" ab
    warnung_jahre, sonst "ok"."""
    if alter_jahre is None:
        return "unbekannt"
    if alter_jahre >= kritisch_jahre:
        return "kritisch"
    if alter_jahre >= warnung_jahre:
        return "warnung"
    return "ok"


def analysiere_treiberpakete(
    treiberpakete: list[dict], warnung_jahre: float, kritisch_jahre: float
) -> list[dict]:
    """Wertet den Treiber-Store (pnputil_drivers.txt, in pnputils/treiber.json)
    aus: Datum, Version und daraus abgeleitetes Alter je Treiberpaket.

    Das Ergebnis-dict hat bewusst auch Felder, die pnputil gar nicht liefert
    (beschreibung, datei, gestartet, startmodus, laufzeitstatus), und zwar mit
    dem Wert None. So haben pnputil-Einträge und die in _ergaenze_msinfo_treiber()
    dazugemischten msinfo32-Einträge am Ende dieselben Schlüssel, egal woher
    sie kommen.
    """
    heute = date.today()
    ergebnisse = []
    for eintrag in treiberpakete:
        datum, version = _parse_treiberversion(eintrag.get("treiberversion"))
        alter_tage = (heute - datum).days if datum else None
        alter_jahre = round(alter_tage / 365.25, 1) if alter_tage is not None else None
        ergebnisse.append({
            "quelle": "pnputil",
            "originalname": eintrag.get("originalname"),
            "veroeffentlichter_name": eintrag.get("veröffentlichter_name"),
            "anbietername": eintrag.get("anbietername"),
            "klassenname": eintrag.get("klassenname"),
            "beschreibung": None,
            "datei": None,
            "version": version,
            "datum": datum.isoformat() if datum else None,
            "alter_tage": alter_tage,
            "alter_jahre": alter_jahre,
            "status": _bewerte_treiber_alter(alter_jahre, warnung_jahre, kritisch_jahre),
            "signaturgeber": eintrag.get("name_des_signaturgebers"),
            "gestartet": None,
            "startmodus": None,
            "laufzeitstatus": None,
        })
    _sortiere_treiber(ergebnisse)
    return ergebnisse


def _sortiere_treiber(treiber: list[dict]) -> None:
    """Sortiert eine Treiberliste an Ort und Stelle, älteste zuerst, damit das
    Kritischste beim Anzeigen oben steht. Einträge ohne Datum (konnten wir
    nicht parsen, oder es gibt gar keins wie bei msinfo32) kommen ans Ende.
    Der Sortierschlüssel ist ein Tupel (kein_datum_vorhanden, negatives_alter):
    True sortiert nach False, und das Minus vor alter_tage dreht "größer
    zuerst" in Pythons standardmäßig aufsteigendes sort() um, ohne
    reverse=True zu brauchen.
    """
    treiber.sort(key=lambda e: (e["alter_tage"] is None, -(e["alter_tage"] or 0)))


def _ergaenze_msinfo_treiber(msinfo_systemtreiber: list[dict], pnputil_treiberpakete: list[dict]) -> list[dict]:
    """Baut aus der msinfo32-Kategorie "Systemtreiber" (parse.py, in
    msinfo/systemtreiber.json) zusätzliche Treiber-Einträge für die Liste, und
    zwar nur für die, die dort noch nicht über pnputil erfasst sind. msinfo32
    kennt kein Datum, diese Einträge bleiben also ohne Alter und mit Status
    "unbekannt" (siehe _bewerte_treiber_alter()), liefern dafür aber
    Beschreibung, Dateipfad und aktuellen Laufzeitstatus.

    Für den Abgleich gibt es leider keinen gemeinsamen, zuverlässigen
    Schlüssel: pnputil identifiziert Treiber über den INF-Namen, zum Beispiel
    "appleusb.inf", msinfo32 dagegen über den Modulnamen, zum Beispiel
    "1394ohci". Beide stimmen nur überein, wenn ein Hersteller die INF-Datei
    genauso benannt hat wie den Treiber selbst. In den echten Daten hier ist
    das nur bei ungefähr 15 Prozent der pnputil-Einträge der Fall. Der
    Namensvergleich unten ist deshalb bewusst nur ein Best-Effort-Abgleich und
    keine vollständige Deduplizierung: ein paar echte Duplikate rutschen
    wahrscheinlich noch durch. Das ist aber immer noch besser als ein
    erzwungener Abgleich, der falsche Treffer produziert (siehe auch den
    Fehler-und-Lösungen-Abschnitt im Modul-Docstring zu genau diesem Problem).
    """
    bekannte_namen = set()
    for eintrag in pnputil_treiberpakete:
        for feld in ("originalname", "veröffentlichter_name"):
            wert = eintrag.get(feld)
            if wert:
                bekannte_namen.add(wert.lower().removesuffix(".inf"))

    ergaenzung = []
    for eintrag in msinfo_systemtreiber:
        name = (eintrag.get("name") or "").lower()
        if name and name in bekannte_namen:
            continue
        ergaenzung.append({
            "quelle": "msinfo32",
            "originalname": eintrag.get("name"),
            "veroeffentlichter_name": None,
            "anbietername": None,
            "klassenname": eintrag.get("typ"),
            "beschreibung": eintrag.get("beschreibung"),
            "datei": eintrag.get("datei"),
            "version": None,
            "datum": None,
            "alter_tage": None,
            "alter_jahre": None,
            "status": "unbekannt",
            "signaturgeber": None,
            "gestartet": eintrag.get("gestartet"),
            "startmodus": eintrag.get("startmodus"),
            "laufzeitstatus": eintrag.get("status"),
        })
    return ergaenzung


def analysiere_signaturen(signaturen: list[dict]) -> list[dict]:
    """Wertet driverquery /si aus (driverquery_signed.csv, in
    driverquery/signaturen.json): ist ein Gerätetreiber digital signiert oder nicht."""
    ergebnisse = []
    for eintrag in signaturen:
        ergebnisse.append({
            "geraetename": eintrag.get("gerätename"),
            "infname": eintrag.get("infname"),
            "hersteller": eintrag.get("hersteller"),
            "signiert": eintrag.get("issigned"),
        })
    # Unsignierte zuerst: "signiert" ist False, True oder None (falls der Wert
    # in der Quelle fehlt). "is not False" ergibt für False also False (sortiert
    # nach vorne) und für alles andere True (sortiert nach hinten).
    ergebnisse.sort(key=lambda e: e["signiert"] is not False)
    return ergebnisse


def analysiere_treiber(
    treiberpakete: list[dict],
    signaturen: list[dict],
    msinfo_systemtreiber: list[dict] | None = None,
    warnung_jahre: float = STANDARD_WARNUNG_JAHRE,
    kritisch_jahre: float = STANDARD_KRITISCH_JAHRE,
) -> dict:
    """Baut aus allen Quellen einen Treiber-Bericht mit Zusammenfassung.

    Alter und Version kommen aus dem Treiber-Store (pnputil), signiert oder
    unsigniert aus driverquery /si. Beide Windows-Befehle listen nicht exakt
    dieselbe Menge an Treibern auf (siehe Fehler-und-Lösungen-Abschnitt oben),
    deshalb werden sie hier bewusst als zwei getrennte, aber vollständige
    Teillisten geführt statt sie gewaltsam und dadurch unvollständig zu
    verknüpfen.

    Wird zusätzlich msinfo_systemtreiber übergeben (die msinfo32-Kategorie
    "Systemtreiber", deutlich vollständiger als der pnputil-Treiber-Store),
    werden dessen Einträge in dieselbe treiberpakete-Liste eingemischt, sofern
    sie dort nicht schon per Namensabgleich vorhanden sind (siehe
    _ergaenze_msinfo_treiber()). Diese Einträge haben kein Datum und landen
    deshalb immer in Status "unbekannt", nie in kritische_treiber oder
    warnung_treiber, auch wenn der Treiber selbst uralt sein könnte.
    """
    treiber_bewertet = analysiere_treiberpakete(treiberpakete, warnung_jahre, kritisch_jahre)

    if msinfo_systemtreiber:
        treiber_bewertet += _ergaenze_msinfo_treiber(msinfo_systemtreiber, treiberpakete)
        _sortiere_treiber(treiber_bewertet)

    signaturen_bewertet = analysiere_signaturen(signaturen)

    kritische = [t for t in treiber_bewertet if t["status"] == "kritisch"]
    warnungen = [t for t in treiber_bewertet if t["status"] == "warnung"]
    unsignierte = [s for s in signaturen_bewertet if s["signiert"] is False]
    ohne_datum = sum(1 for t in treiber_bewertet if t["datum"] is None)

    zusammenfassung = {
        "anzahl_treiberpakete": len(treiber_bewertet),
        "anzahl_ohne_datum": ohne_datum,
        "anzahl_kritisch": len(kritische),
        "anzahl_warnung": len(warnungen),
        "warnung_ab_jahren": warnung_jahre,
        "kritisch_ab_jahren": kritisch_jahre,
        "anzahl_signaturen_geprueft": len(signaturen_bewertet),
        "anzahl_unsigniert": len(unsignierte),
    }

    return {
        "zusammenfassung": zusammenfassung,
        "kritische_treiber": kritische,
        "warnung_treiber": warnungen,
        "unsignierte_geraete": unsignierte,
        "treiberpakete": treiber_bewertet,
        "signaturen": signaturen_bewertet,
    }


# --------------------------------------------------------------------------
# Laufwerke: Speicherplatz
# --------------------------------------------------------------------------

# msinfo32 schreibt Größenangaben als "930,89 GB (999.530.360.832 Bytes)", uns
# interessiert nur die exakte Byte-Zahl in der Klammer.
_BYTES_RE = re.compile(r"\(([\d.]+)\s*Bytes\)")


def _extrahiere_bytes(text: str | None) -> int | None:
    if not text:
        return None
    match = _BYTES_RE.search(text)
    if not match:
        return None
    return int(match.group(1).replace(".", ""))


def analysiere_laufwerke(
    laufwerke: dict | list[dict],
    kritisch_prozent: float = STANDARD_KRITISCH_PROZENT,
    warnung_prozent: float = STANDARD_WARNUNG_PROZENT,
) -> dict:
    """Berechnet je Laufwerk den freien Speicherplatz in Prozent und markiert
    es als kritisch, warnung oder ok."""
    # _parse_element_wert_category() aus parse.py liefert bei nur einem Laufwerk
    # ein einzelnes dict statt einer Liste, das wird hier auf eine Liste vereinheitlicht.
    eintraege = laufwerke if isinstance(laufwerke, list) else [laufwerke]

    ergebnisse = []
    for eintrag in eintraege:
        groesse_bytes = _extrahiere_bytes(eintrag.get("größe"))
        frei_bytes = _extrahiere_bytes(eintrag.get("freier_speicherplatz"))
        # "if groesse_bytes" statt "is not None": eine Größe von 0 Bytes ergäbe
        # sonst eine Division durch null. Kommt in der Praxis wohl nie vor, aber
        # so bricht es wenigstens nicht mit einer ZeroDivisionError ab.
        if groesse_bytes:
            frei_prozent = round(frei_bytes / groesse_bytes * 100, 1) if frei_bytes is not None else None
        else:
            frei_prozent = None

        if frei_prozent is None:
            status = "unbekannt"
        elif frei_prozent < kritisch_prozent:
            status = "kritisch"
        elif frei_prozent < warnung_prozent:
            status = "warnung"
        else:
            status = "ok"

        ergebnisse.append({
            "laufwerk": eintrag.get("laufwerk"),
            "beschreibung": eintrag.get("beschreibung"),
            "dateisystem": eintrag.get("dateissystem"),
            "groesse_bytes": groesse_bytes,
            "frei_bytes": frei_bytes,
            "frei_prozent": frei_prozent,
            "status": status,
        })

    ergebnisse.sort(key=lambda e: (e["frei_prozent"] is None, e["frei_prozent"] or 0))

    return {
        "zusammenfassung": {
            "anzahl_laufwerke": len(ergebnisse),
            "anzahl_kritisch": sum(1 for e in ergebnisse if e["status"] == "kritisch"),
            "anzahl_warnung": sum(1 for e in ergebnisse if e["status"] == "warnung"),
            "kritisch_unter_prozent": kritisch_prozent,
            "warnung_unter_prozent": warnung_prozent,
        },
        "laufwerke": ergebnisse,
    }


# --------------------------------------------------------------------------
# Gesamtbericht
# --------------------------------------------------------------------------

def erstelle_bericht(
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
    warnung_jahre: float = STANDARD_WARNUNG_JAHRE,
    kritisch_jahre: float = STANDARD_KRITISCH_JAHRE,
    kritisch_prozent: float = STANDARD_KRITISCH_PROZENT,
    warnung_prozent: float = STANDARD_WARNUNG_PROZENT,
) -> dict:
    """Lädt die von parse.py erzeugten JSON-Dateien, führt Treiber- und
    Laufwerksanalyse sowie die Green-Einschätzung aus green/green.py aus und
    speichert alles als mehrere kleinere Berichte plus einen Gesamtbericht
    unter data/output/report/.
    """
    input_dir = Path(input_dir) if input_dir else DEFAULT_INPUT_DIR
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR

    print("[analyze] prüfe Treiber (Alter, Version, Signatur)...")
    treiberpakete = _load_json(input_dir / "pnputils" / "treiber.json")
    signaturen = _load_json(input_dir / "driverquery" / "signaturen.json")
    msinfo_systemtreiber = _load_json(input_dir / "msinfo" / "systemtreiber.json")
    treiber_bericht = analysiere_treiber(
        treiberpakete, signaturen, msinfo_systemtreiber, warnung_jahre, kritisch_jahre
    )

    print("[analyze] prüfe Laufwerke (freier Speicherplatz)...")
    speicher = _load_json(input_dir / "msinfo" / "speicher.json")
    laufwerke_bericht = analysiere_laufwerke(
        speicher.get("laufwerke", []), kritisch_prozent, warnung_prozent
    )

    print("[analyze] hole Green-/CO2-Einschätzung...")
    # Lazy-Import, damit `import analyze` nicht zwingend `green` mitlädt, falls
    # jemand nur die Treiber-/Laufwerksfunktionen direkt nutzen will.
    from green.green import berechne as green_berechne
    green_bericht = green_berechne(input_dir=input_dir, output_dir=output_dir)

    # kritische_punkte ist eine ganz flache Liste von Sätzen (keine verschachtelten
    # Objekte), extra für die CLI-Anzeige gedacht. Wer die vollen Daten braucht,
    # schaut in treiber.json/laufwerke.json/green.json statt hier.
    kritische_punkte = []
    for t in treiber_bericht["kritische_treiber"]:
        kritische_punkte.append(
            f"Treiber '{t['originalname']}' ist {t['alter_jahre']} Jahre alt (kritisch veraltet)."
        )
    for s in treiber_bericht["unsignierte_geraete"]:
        kritische_punkte.append(f"Gerät '{s['geraetename']}' ist nicht signiert.")
    for l in laufwerke_bericht["laufwerke"]:
        if l["status"] == "kritisch":
            kritische_punkte.append(
                f"Laufwerk {l['laufwerk']} hat nur noch {l['frei_prozent']}% frei."
            )
    if green_bericht["gesamteinstufung"] == "hoch":
        kritische_punkte.append(
            "Energieeinstellungen und Autostart verursachen laut Green-Einschätzung "
            "einen hohen zusätzlichen Energieverbrauch."
        )

    gesamtbericht = {
        "zusammenfassung": {
            "treiber": treiber_bericht["zusammenfassung"],
            "laufwerke": laufwerke_bericht["zusammenfassung"],
            "green_gesamteinstufung": green_bericht["gesamteinstufung"],
        },
        "kritische_punkte": kritische_punkte,
    }

    _save_json(output_dir / "treiber.json", treiber_bericht)
    _save_json(output_dir / "laufwerke.json", laufwerke_bericht)
    _save_json(output_dir / "gesamtbericht.json", gesamtbericht)
    print("[analyze] fertig.")

    return {
        "treiber": treiber_bericht,
        "laufwerke": laufwerke_bericht,
        "green": green_bericht,
        "gesamtbericht": gesamtbericht,
    }


def main() -> int:
    """Dünner CLI-Wrapper um erstelle_bericht() für den direkten Aufruf
    `python analyze.py`."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--warnung-jahre", type=float, default=STANDARD_WARNUNG_JAHRE)
    parser.add_argument("--kritisch-jahre", type=float, default=STANDARD_KRITISCH_JAHRE)
    parser.add_argument("--kritisch-prozent", type=float, default=STANDARD_KRITISCH_PROZENT)
    parser.add_argument("--warnung-prozent", type=float, default=STANDARD_WARNUNG_PROZENT)
    args = parser.parse_args()

    erstelle_bericht(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        warnung_jahre=args.warnung_jahre,
        kritisch_jahre=args.kritisch_jahre,
        kritisch_prozent=args.kritisch_prozent,
        warnung_prozent=args.warnung_prozent,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
