"""Parst die von collect/collect.py gesammelten Rohdaten (driverquery, pnputil,
msinfo32, powercfg) aus nfoanalyst/data/input/ in sauberes, iterierbares JSON
und speichert es nach nfoanalyst/data/output/<quelle>/.

Nutzung als Bibliothek (einzeiliger Aufruf aus einem anderen Programm):

    from nfoanalyst.parse.parse import parse
    ergebnisse = parse()

parse() ruft alle vier Einzel-Parser nacheinander auf und gibt ein dict mit
den kompletten Ergebnissen zurück (zusätzlich zum Schreiben der JSON-Dateien).
Für nur eine Quelle reicht auch der jeweilige Einzel-Parser:

    from nfoanalyst.parse.parse import parse_driverquery
    daten = parse_driverquery()

Das Skript ist außerdem direkt per `python parse.py` als CLI-Tool nutzbar,
siehe main()/argparse ganz unten.

Fehler und Lösungen beim Entwickeln:
Beim ersten Testlauf kamen bei driverquery_verbose.csv nur leere dicts heraus,
obwohl die CSV-Datei voll mit Zeilen war. Der Grund: driverquery streut
zwischen jede Datenzeile noch eine komplett leere Zeile, nicht nur nach der
Kopfzeile. Der csv.reader gibt so eine leere Zeile als leere Liste zurück,
und die wurde vorher nicht herausgefiltert, dadurch gab es für jede echte
Zeile noch eine leere dazwischen. Behoben in _read_csv_rows() mit einem
einfachen `if row`.

Zweiter Fund: In der msinfo32-Kategorie "Windows-Fehlerberichterstattung"
sahen die "Details"-Texte aus wie ein einziger langer Textblock ohne
Zeilenumbrüche. Es stellte sich heraus, dass msinfo32 dort Zeilenumbrüche
nicht auflöst, sondern
als rohen XML-Text "&#x000d;&#x000a;" stehen lässt. Das wird jetzt in
_coerce_value() in echte Zeilenumbrüche zurückverwandelt.
"""

from __future__ import annotations

# csv:                   zum Einlesen der driverquery-CSV-Dateien, kümmert sich um Anführungszeichen etc.
# json:                  zum Schreiben der sauberen Ausgabedateien.
# re:                    für die paar Stellen, an denen Text nach einem festen Muster ausgewertet wird,
#                        zum Beispiel "GUID (Name)" bei powercfg oder deutsch formatierte Zahlen wie "1.234".
# xml.etree.ElementTree: zum Einlesen der msinfo32-.nfo-Datei. Die sieht nach einer alten Textdatei aus,
#                        ist aber tatsächlich UTF-16-kodiertes XML (siehe parse_msinfo32).
# pathlib:               plattformunabhängiges Pfad-Handling statt String-Verkettung.
import csv
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# Pfade werden relativ zu dieser Datei berechnet, damit parse() auch dann die richtigen
# Verzeichnisse trifft, wenn es aus einem anderen Projekt mit anderem Arbeitsverzeichnis
# importiert wird (gleiches Prinzip wie in collect/collect.py).
PARSE_DIR = Path(__file__).resolve().parent
NFOANALYST_DIR = PARSE_DIR.parent
DEFAULT_INPUT_DIR = NFOANALYST_DIR / "data" / "input"
DEFAULT_OUTPUT_DIR = NFOANALYST_DIR / "data" / "output"


# --------------------------------------------------------------------------
# Allgemeine Helfer, die von mehreren Parsern gebraucht werden
# --------------------------------------------------------------------------

# Erkennt deutsch gruppierte Ganzzahlen wie "4.096" oder "3.850.240" (Tausenderpunkt).
# Absichtlich streng (jede Gruppe nach dem Punkt muss genau 3 Ziffern haben), damit
# Datumsangaben ("19.05.2015"), Versionsnummern ("10.0.19045") oder GUIDs nicht
# aus Versehen als Zahl erkannt werden.
_GERMAN_INT_RE = re.compile(r"^\d{1,3}(\.\d{3})*$")


def _normalize_key(raw: str) -> str:
    """Macht aus einer rohen Spalten-/Feldbezeichnung einen schlanken snake_case-Key,
    zum Beispiel "Ausgelagerter Pool (Bytes)" wird zu "ausgelagerter_pool_bytes" oder
    "Klassen-GUID" wird zu "klassen_guid". Umlaute bleiben erhalten, das liest sich für
    uns später einfach angenehmer, als sie krampfhaft in ae/oe/ue umzuwandeln.
    """
    key = raw.strip().rstrip(":").strip()
    key = key.replace("(", "_").replace(")", "")
    key = re.sub(r"[\s/_-]+", "_", key)
    return key.strip("_").lower()


def _coerce_value(value: str):
    """Wandelt einen rohen String in den passenden JSON-Typ um, wo das eindeutig ist.
    Leere Strings werden zu None, "TRUE"/"FALSE"/"Ja"/"Nein" werden zu bool, deutsch
    gruppierte Ganzzahlen werden zu int. Alles andere bleibt ein String. Das macht die
    Ausgabe direkt benutzbar, zum Beispiel bytes_wert > 1000 statt erst noch
    str.replace(".", "") aufzurufen.
    """
    # msinfo32 lässt in manchen Feldern (vor allem Windows-Fehlerberichterstattung)
    # eingebettete Zeilenumbrüche als rohe XML-Escapes stehen, statt sie beim Export
    # aufzulösen. Das macht sonst jede "Details"-Zeile zu einem einzigen unlesbaren
    # Textblock (siehe Fehler-und-Lösungen-Abschnitt im Modul-Docstring).
    value = value.replace("&#x000d;&#x000a;", "\n").replace("&#x000d;", "\n").replace("&#x000a;", "\n") # ersetzt Windows-Zeilenumbrüche mit Linux-Zeilenumbrüchen
    value = value.strip()  
    if not value:
        return None
    if value in ("TRUE", "Ja"):
        return True
    if value in ("FALSE", "Nein"):
        return False
    if _GERMAN_INT_RE.fullmatch(value):   
        return int(value.replace(".", "")) 
    return value


def _dedupe_keys(keys: list[str]) -> list[str]:
    """Hängt an doppelte Spaltennamen _2, _3, ... an, zum Beispiel weil driverquery
    und msinfo32 beide zweimal eine Spalte "Status" haben (einmal Laufzeitstatus,
    einmal Gesundheitsstatus)."""
    counts: dict[str, int] = {}
    result = []
    for key in keys:
        if key not in counts:
            counts[key] = 1
            result.append(key)
        else:
            counts[key] += 1
            result.append(f"{key}_{counts[key]}")
    return result


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  gespeichert: {path.parent.name}/{path.name}")


# --------------------------------------------------------------------------
# driverquery
# --------------------------------------------------------------------------

def _read_csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = _dedupe_keys([_normalize_key(h) for h in next(reader)])
        # driverquery streut zwischen jede Datenzeile noch eine Leerzeile, die der
        # csv-Reader als leere Liste zurückgibt. Die wird hier einfach übersprungen.
        return [
            {key: _coerce_value(value) for key, value in zip(header, row)}
            for row in reader
            if row
        ]


def parse_driverquery(
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> dict:
    """Parst driverquery_verbose.csv (die eigentliche Treiberliste) und
    driverquery_signed.csv (Signatur-/Herstellerinfos je Gerät) und speichert
    sie als treiber.json bzw. signaturen.json.
    """
    input_dir = Path(input_dir) if input_dir else DEFAULT_INPUT_DIR
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR / "driverquery"

    print("[driverquery] parse Treiberliste...")
    treiber = _read_csv_rows(input_dir / "driverquery_verbose.csv")
    signaturen = _read_csv_rows(input_dir / "driverquery_signed.csv")

    _save_json(output_dir / "treiber.json", treiber)
    _save_json(output_dir / "signaturen.json", signaturen)

    return {"treiber": treiber, "signaturen": signaturen}


# --------------------------------------------------------------------------
# pnputil
# --------------------------------------------------------------------------

def _parse_pnputil_blocks(path: Path) -> list[dict]:
    """pnputil schreibt pro Gerät/Treiber einen Block aus "Key:   Wert"-Zeilen,
    getrennt durch Leerzeilen (siehe pnputil_devices.txt/pnputil_drivers.txt).
    Als Marker dafür, wo ein neuer Block anfängt, nehmen wir einfach den
    allerersten Key, der in der Datei auftaucht (bei Geräten "Instanz-ID",
    bei Treibern "Veröffentlichter Name"). Der kommt garantiert bei jedem
    Block als erstes und sonst nirgends.
    """
    lines = path.read_text(encoding="utf-8").splitlines()

    felder = []
    for line in lines[1:]:  # erste Zeile ist nur die Überschrift "Microsoft-PnP-Hilfsprogramm"
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        felder.append((_normalize_key(key), value.strip()))

    if not felder:
        return []

    start_key = felder[0][0]
    blocks: list[dict] = []
    aktueller_block: dict = {}
    for key, value in felder:
        # "and aktueller_block" ist wichtig: ohne die Bedingung würde schon
        # beim allerersten Feld (das ja selbst start_key ist) ein leerer Block
        # angehängt werden, bevor überhaupt was drinsteht.
        if key == start_key and aktueller_block:
            blocks.append(aktueller_block)
            aktueller_block = {}
        aktueller_block[key] = _coerce_value(value)
    if aktueller_block:
        blocks.append(aktueller_block)
    return blocks


def parse_pnputil(
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> dict:
    """Parst pnputil_devices.txt (angeschlossene Geräte) und pnputil_drivers.txt
    (kompletter Treiber-Store) und speichert sie als geraete.json/treiber.json.
    """
    input_dir = Path(input_dir) if input_dir else DEFAULT_INPUT_DIR
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR / "pnputils"

    print("[pnputil] parse Geräte- und Treiberinformationen...")
    geraete = _parse_pnputil_blocks(input_dir / "pnputil_devices.txt")
    treiber = _parse_pnputil_blocks(input_dir / "pnputil_drivers.txt")

    _save_json(output_dir / "geraete.json", geraete)
    _save_json(output_dir / "treiber.json", treiber)

    return {"geraete": geraete, "treiber": treiber}


# --------------------------------------------------------------------------
# powercfg
# --------------------------------------------------------------------------

_GUID_NAME_RE = re.compile(r"([0-9a-fA-F-]+)\s*\(([^)]*)\)")


def _split_guid_name(value: str) -> tuple[str, str]:
    """"381b4222-...-ff5bb260df2e  (Ausbalanciert)" wird zu (guid, name)."""
    match = _GUID_NAME_RE.match(value.strip())
    if match:
        return match.group(1), match.group(2).strip()
    return value.strip(), ""


def _parse_powercfg_list(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    schemen = []
    for line in text.splitlines():
        if "GUID des Energieschemas" not in line:
            continue
        _, _, rest = line.partition(":")
        guid, name = _split_guid_name(rest)
        schemen.append({"guid": guid, "name": name, "aktiv": rest.strip().endswith("*")})
    return schemen


def _parse_powercfg_sleepstates(path: Path) -> dict:
    verfuegbar: list[str] = []
    nicht_verfuegbar: list[dict] = []
    abschnitt = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if "nicht verf" in raw_line:
            abschnitt = "nicht_verfuegbar"
            continue
        if "verf" in raw_line and "Standbymodusfunktionen" in raw_line:
            abschnitt = "verfuegbar"
            continue
        if raw_line.startswith("\t") and nicht_verfuegbar:
            nicht_verfuegbar[-1]["grund"] = stripped
            continue
        if abschnitt == "verfuegbar":
            verfuegbar.append(stripped)
        elif abschnitt == "nicht_verfuegbar":
            nicht_verfuegbar.append({"name": stripped, "grund": None})

    return {"verfügbar": verfuegbar, "nicht_verfügbar": nicht_verfuegbar}


# Zeilen-Bezeichnungen aus `powercfg /query`, die einfach als Eigenschaft auf dem
# zuletzt geöffneten Eintrag (Untergruppe oder Einstellung) landen.
_POWERCFG_EINFACHE_FELDER = {
    "GUID-Alias": "alias",
    "Minimum der möglichen Einstellung": "minimum",
    "Maximum der möglichen Einstellung": "maximum",
    "Schrittweise Erhöhung der möglichen Einstellungen": "schrittweite",
    "Einheiten der möglichen Einstellungen": "einheiten",
    "Index der aktuellen Wechselstromeinstellung": "aktueller_wert_wechselstrom",
    "Index der aktuellen Gleichstromeinstellung": "aktueller_wert_gleichstrom",
}


def _parse_powercfg_query(path: Path) -> dict:
    """`powercfg /query` gibt eine verschachtelte Baumstruktur aus (Schema, dann
    Untergruppen, dann Einstellungen, dann Details), aber nur über Einrückung,
    ohne irgendwelche IDs, die man leicht per Regex herausgreifen könnte. Deshalb
    hier ein simpler Zustandsautomat: wir merken uns, welche Untergruppe bzw.
    Einstellung gerade "offen" ist, und hängen nachfolgende Zeilen dort dran,
    bis die nächste Untergruppe oder Einstellung anfängt.
    """
    schema: dict = {}
    aktuelle_untergruppe: dict | None = None
    aktuelle_einstellung: dict | None = None
    # "ziel" zeigt immer auf das dict, in das gerade eingefügt wird, je nachdem,
    # welche Ebene (Schema, Untergruppe oder Einstellung) zuletzt begonnen hat.
    # Vergleichbar mit einem Cursor, der sich beim Auf- und Absteigen in der
    # Baumstruktur mitbewegt, ohne dass die Verschachtelung selbst nachgebaut
    # werden muss.
    ziel: dict = {}

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        label, _, value = line.partition(":")
        label = label.strip()
        value = value.strip()

        if label == "GUID des Energieschemas":
            guid, name = _split_guid_name(value)
            schema = {"guid": guid, "name": name, "untergruppen": []}
            ziel = schema

        elif label == "GUID der Untergruppe":
            guid, name = _split_guid_name(value)
            aktuelle_untergruppe = {"guid": guid, "name": name, "einstellungen": []}
            schema.setdefault("untergruppen", []).append(aktuelle_untergruppe)
            ziel = aktuelle_untergruppe

        # "and aktuelle_untergruppe is not None" schützt nur zur Sicherheit vor
        # fehlerhaften oder unerwarteten Eingabedateien (eine Einstellung ohne
        # vorherige Untergruppe sollte laut powercfg-Format eigentlich nie vorkommen).
        elif label == "GUID der Energieeinstellung" and aktuelle_untergruppe is not None:
            guid, name = _split_guid_name(value)
            aktuelle_einstellung = {"guid": guid, "name": name, "moegliche_werte": []}
            aktuelle_untergruppe["einstellungen"].append(aktuelle_einstellung)
            ziel = aktuelle_einstellung

        elif label == "Index der möglichen Einstellung" and aktuelle_einstellung is not None:
            aktuelle_einstellung["moegliche_werte"].append({"index": value, "name": None})

        elif label == "Anzeigename der möglichen Einstellung" and aktuelle_einstellung is not None:
            if aktuelle_einstellung["moegliche_werte"]:
                aktuelle_einstellung["moegliche_werte"][-1]["name"] = value

        elif label in _POWERCFG_EINFACHE_FELDER:
            ziel[_POWERCFG_EINFACHE_FELDER[label]] = _coerce_value(value)

    return schema


def parse_powercfg(
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> dict:
    """Parst die drei powercfg-Dateien: Liste der Energieschemen, die komplette
    Einstellungs-Baumstruktur des aktiven Schemas und die Standby-Fähigkeiten.
    """
    input_dir = Path(input_dir) if input_dir else DEFAULT_INPUT_DIR
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR / "powercfg"

    print("[powercfg] parse Energieeinstellungen...")
    schemen = _parse_powercfg_list(input_dir / "powercfg_list.txt")
    einstellungen = _parse_powercfg_query(input_dir / "powercfg_query.txt")
    standby = _parse_powercfg_sleepstates(input_dir / "powercfg_sleepstates.txt")

    _save_json(output_dir / "schemen.json", schemen)
    _save_json(output_dir / "einstellungen.json", einstellungen)
    _save_json(output_dir / "standby.json", standby)

    return {"schemen": schemen, "einstellungen": einstellungen, "standby": standby}


# --------------------------------------------------------------------------
# msinfo32
# --------------------------------------------------------------------------

# Die Speicher-Unterkategorien, die msinfo32 im Element/Wert-Format ausgibt.
# Können je nach Rechner mehrere Einträge enthalten, zum Beispiel mehrere Laufwerke.
_MSINFO_SPEICHER_KATEGORIEN = ("Laufwerke", "Datenträger", "SCSI", "IDE")

# Kategorien mit echten Tabellenspalten (jede Zeile hat dieselben, klar benannten
# Spalten), die für uns für die Treiber-/Autostart-/Fehleranalyse wichtig sind.
# "Aktive Aufgaben", "Geladene Module", "Dienste" und "Umgebungsvariablen" sind
# eher Beiwerk, kosten hier aber nichts extra.
_MSINFO_TABELLEN_KATEGORIEN = {
    "problemgeraete": "Problemgeräte",
    "systemtreiber": "Systemtreiber",
    "autostartprogramme": "Autostartprogramme",
    "windows_fehlerberichterstattung": "Windows-Fehlerberichterstattung",
    "aktive_aufgaben": "Aktive Aufgaben",
    "geladene_module": "Geladene Module",
    "dienste": "Dienste",
    "umgebungsvariablen": "Umgebungsvariablen",
}


def _find_category(root: ET.Element, name: str) -> ET.Element | None:
    # root.iter("Category") statt root.findall("Category"): iter() sucht in der
    # KOMPLETTEN Baumstruktur, egal wie tief verschachtelt (Systemtreiber liegt
    # z.B. drei Ebenen unter der Wurzel: Systemübersicht > Softwareumgebung >
    # Systemtreiber). findall() ohne ".//" würde nur direkte Kinder finden.
    for category in root.iter("Category"):
        if category.attrib.get("name") == name:
            return category
    return None


def _dedupe_and_build(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    counts: dict[str, int] = {}
    for raw_key, value in pairs:
        key = _normalize_key(raw_key)
        if key in result:
            counts[key] = counts.get(key, 1) + 1
            key = f"{key}_{counts[key]}"
        result[key] = value
    return result


def _parse_data_rows(category: ET.Element | None) -> list[dict]:
    """Liest alle <Data>-Zeilen einer Kategorie mit "echten" Spaltennamen ein
    (also nicht im generischen Element/Wert-Format), als Liste von dicts.
    """
    if category is None:
        return []
    rows = []
    for data in category.findall("Data"):
        pairs = [(child.tag, _coerce_value((child.text or "").strip())) for child in data]
        rows.append(_dedupe_and_build(pairs))
    return rows


def _parse_element_wert_category(category: ET.Element | None) -> dict | list[dict]:
    """Liest eine Kategorie im generischen <Element>/<Wert>-Format ein, zum
    Beispiel Systemübersicht oder Speicher > Laufwerke.

    Kommt jeder Element-Name nur einmal vor (wie bei Systemübersicht), ist das
    Ergebnis ein einzelnes flaches dict. Wiederholt sich der allererste
    Element-Name (zum Beispiel "Laufwerk" bei mehreren Festplatten), markiert
    das den Beginn eines neuen Eintrags, und es kommt eine Liste von dicts
    zurück.
    """
    if category is None:
        return {}

    paare = []
    for data in category.findall("Data"):
        element = data.findtext("Element")
        wert = data.findtext("Wert")
        if element is not None and element.strip():
            paare.append((element.strip(), _coerce_value((wert or "").strip())))

    if not paare:
        return {}

    # Zählt, wie oft der allererste Element-Name insgesamt vorkommt: kommt er
    # nur einmal vor (wie bei Systemübersicht, wo jeder Element-Name eindeutig
    # ist), gibt es nichts zu splitten und es kommt ein einzelnes flaches dict
    # heraus. Kommt er mehrfach vor (wie "Laufwerk" bei mehreren Festplatten),
    # markiert jedes Wiederauftauchen den Start eines neuen Eintrags.
    start_key = paare[0][0]
    if sum(1 for key, _ in paare if key == start_key) <= 1:
        return _dedupe_and_build(paare)

    eintraege: list[dict] = []
    aktuelle_paare: list[tuple[str, object]] = []
    for key, value in paare:
        if key == start_key and aktuelle_paare:
            eintraege.append(_dedupe_and_build(aktuelle_paare))
            aktuelle_paare = []
        aktuelle_paare.append((key, value))
    if aktuelle_paare:
        eintraege.append(_dedupe_and_build(aktuelle_paare))
    return eintraege


def parse_msinfo32(
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> dict:
    """Parst die msinfo32-.nfo-Datei (tatsächlich UTF-16-XML, kein Fließtext) und
    teilt sie in die für uns wichtigen Abteile auf: Systemübersicht, Speicher
    (Laufwerke/Datenträger/SCSI/IDE), Problemgeräte, Systemtreiber,
    Autostartprogramme und Windows-Fehlerberichterstattung, plus als Beigabe
    Aktive Aufgaben/Geladene Module/Dienste/Umgebungsvariablen. Jedes Abteil
    wird als eigene JSON-Datei in output_dir gespeichert.
    """
    input_dir = Path(input_dir) if input_dir else DEFAULT_INPUT_DIR
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR / "msinfo"

    print("[msinfo32] parse Systembericht...")
    root = ET.parse(input_dir / "msinfo32_report.nfo").getroot()

    ergebnis: dict = {
        "systemuebersicht": _parse_element_wert_category(_find_category(root, "Systemübersicht")),
        "speicher": {
            _normalize_key(name): _parse_element_wert_category(_find_category(root, name))
            for name in _MSINFO_SPEICHER_KATEGORIEN
        },
    }
    for key, category_name in _MSINFO_TABELLEN_KATEGORIEN.items():
        ergebnis[key] = _parse_data_rows(_find_category(root, category_name))

    _save_json(output_dir / "systemuebersicht.json", ergebnis["systemuebersicht"])
    _save_json(output_dir / "speicher.json", ergebnis["speicher"])
    for key in _MSINFO_TABELLEN_KATEGORIEN:
        _save_json(output_dir / f"{key}.json", ergebnis[key])

    return ergebnis


# --------------------------------------------------------------------------
# Alles zusammen
# --------------------------------------------------------------------------

def parse(
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
) -> dict:
    """Parst alle vier Quellen und speichert sie strukturiert unter output_dir
    (Standard: nfoanalyst/data/output/<driverquery|pnputils|powercfg|msinfo>/).

    Das ist der Haupteinstiegspunkt für die Nutzung als Bibliothek:

        from nfoanalyst.parse.parse import parse
        ergebnisse = parse()
    """
    input_dir = Path(input_dir) if input_dir else DEFAULT_INPUT_DIR
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR

    ergebnisse = {
        "driverquery": parse_driverquery(input_dir, output_dir / "driverquery"),
        "pnputil": parse_pnputil(input_dir, output_dir / "pnputils"),
        "powercfg": parse_powercfg(input_dir, output_dir / "powercfg"),
        "msinfo32": parse_msinfo32(input_dir, output_dir / "msinfo"),
    }
    print("\nFertig.")
    return ergebnisse


def main() -> int:
    """Dünner CLI-Wrapper um parse() für den direkten Aufruf `python parse.py`."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    parse(input_dir=args.input_dir, output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
