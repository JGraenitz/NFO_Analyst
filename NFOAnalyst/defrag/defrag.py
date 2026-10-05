"""Prüft den Fragmentierungsstatus aller lokalen Festplatten über das
Windows-eigene Kommandozeilentool `defrag /A` (nur Analyse, es wird dabei
NICHTS defragmentiert oder sonst verändert). Anders als collect/parse/analyze
wird dafür kein vorheriges 'collect'/'parse' gebraucht (nichts wird aus
data/input gelesen), das Ergebnis wird aber trotzdem nach data/output/defrag
geschrieben: pro Laufwerk die komplette Rohausgabe von defrag als eigene
.txt-Datei, plus ein gesamter bericht.json mit allen ausgewerteten Werten.

Bibliotheken:
- ctypes:     Zugriff auf die Windows-API. GetLogicalDrives()/GetDriveTypeW()
              liefern die Buchstaben aller lokal eingebauten (fest verbauten)
              Laufwerke, ohne dass man sie von Hand eintragen müsste. Netzlaufwerke,
              CD-Laufwerke und USB-Sticks werden dabei bewusst ausgeschlossen,
              defrag ergibt bei denen wenig bis keinen Sinn.
- re:         zum Herausgreifen der Fragmentierungswerte (Prozentzahlen) und
              des Medientyps (SSD/HDD) aus der Textausgabe von defrag.
- string:     liefert ascii_uppercase für die Laufwerksbuchstaben A-Z.
- json:       zum Speichern von bericht.json (gleiches Muster wie _save_json()
              in parse.py/analyze.py).
- pathlib:    für die Ausgabepfade unter data/output/defrag.

Nutzung als Bibliothek:

    from nfoanalyst.defrag.defrag import pruefe_fragmentierung
    bericht = pruefe_fragmentierung()

is_admin() und run_command() werden aus collect/collect.py wiederverwendet,
statt sie hier ein zweites Mal zu schreiben. defrag /A braucht wie
pnputil /enum-drivers und powercfg /energy Administratorrechte, siehe
is_admin() dort.

Fehler und Lösungen beim Entwickeln:
Ich konnte defrag /A /V beim Entwickeln nicht mit Administratorrechten
testen (die Entwicklungsumgebung lief ohne Admin-Rechte). Deshalb ist
_extrahiere_prozent()/_extrahiere_wert() unten bewusst nicht auf eine exakt
bekannte Zeile angewiesen, sondern sucht nur nach Zeilen, die bestimmte
Stichworte enthalten (zum Beispiel "Gesamtfragmentierung" und "%"), egal wie
genau drumherum formatiert ist. Das ist robuster gegen kleine Unterschiede
zwischen Windows-Versionen/Sprachen, als eine Zeile exakt nachzubauen, die
ich selbst noch nicht in einer erfolgreichen Analyse gesehen habe. "rohtext"
wird trotzdem immer mitgespeichert, damit man notfalls von Hand nachschauen
kann, falls die Werte doch mal leer bleiben. Das müsste ich eigentlich noch
auf einem Rechner mit Administratorrechten querchecken.

Zweiter Fund (den ohne Admin-Rechte laufenden Fall selbst betreffend): defrag
gibt bei fehlenden Berechtigungen die Meldung "Die Speicheroptimierung kann
nicht gestartet werden, da Sie keine ausreichenden Berechtigungen..." aus,
aber der Exit-Code des Prozesses war beim Testen trotzdem 0 (Erfolg). Ohne
weitere Prüfung hätte run_command() das also fälschlich als "erfolgreich"
durchgehen lassen, obwohl gar keine Analyse stattgefunden hat. Deshalb wird
in analysiere_laufwerk() zusätzlich nach dieser bekannten Fehlermeldung im
Text gesucht, um "erfolgreich" korrekt auf False zu setzen.

Dritter Fund, nachdem ich den echten Output mit Adminrechten sehen konnte
(vielen Dank an dieser Stelle für den Testlauf): mein erster Versuch war
schlicht am veralteten Format orientiert. Ich bin von den alten, oft
beschriebenen drei Prozentwerten "Gesamtfragmentierung"/"Dateifragmentierung"/
"Freie Speicherplatzfragmentierung" ausgegangen. Die tatsächliche Ausgabe
(Windows 11, deutsch) heißt "Post Defragmentation Report" und hat nur EINEN
Fragmentierungs-Prozentwert namens "Fragmentierter Speicherplatz insgesamt",
dafür aber ein paar zusätzliche Zähler ("Fragmentierte Dateien",
"Dateifragmente insgesamt", "Durchschnittliche Fragmente pro Datei"), die es
im alten Format so nicht gab. Es gibt außerdem keine eigene Prozentangabe
mehr für die Fragmentierung des freien Speicherplatzes. _extrahiere_zahl()
und die Feldnamen unten in analysiere_laufwerk() sind jetzt an dieses
tatsächlich beobachtete Format angepasst, mit den alten Stichworten als
zusätzlichem Versuch, falls doch mal eine ältere Windows-Version antwortet.
"""

from __future__ import annotations

import ctypes
import json
import re
import string
from pathlib import Path

# collect.collect() bringt is_admin() und run_command() schon mit (Adminrechte-
# Check bzw. das Ausführen+Dekodieren eines Windows-Kommandozeilentools über
# subprocess). Beides wird hier eins zu eins wiederverwendet.
from collect.collect import is_admin, run_command

# Pfade werden relativ zu dieser Datei berechnet, nicht zum aktuellen
# Arbeitsverzeichnis (gleiches Prinzip wie in collect/collect.py und parse/parse.py).
DEFRAG_DIR = Path(__file__).resolve().parent
NFOANALYST_DIR = DEFRAG_DIR.parent
DEFAULT_OUTPUT_DIR = NFOANALYST_DIR / "data" / "output" / "defrag"

# Laut Windows-API-Dokumentation steht 3 für DRIVE_FIXED, also ein fest
# eingebautes Laufwerk (im Gegensatz zu Wechseldatenträger, Netzlaufwerk, CD/DVD).
_DRIVE_FIXED = 3

# Daumenregel für die Einstufung: ab wann ein HDD-Laufwerk als "warnung" bzw.
# "kritisch" fragmentiert gilt. Angelehnt an die alte, oft zitierte
# Microsoft-Faustregel, ab der die automatische Windows-Datenträgeroptimierung
# ein Laufwerk selbst als defragmentierungswürdig einstuft (grob 10 %).
# "kritisch" ist eine zusätzliche, strengere Stufe für sehr stark fragmentierte
# Laufwerke, nach demselben Muster wie die Warnung-/Kritisch-Schwellen bei
# analyze.py (siehe dort STANDARD_WARNUNG_PROZENT/STANDARD_KRITISCH_PROZENT).
STANDARD_WARNUNG_PROZENT = 10.0
STANDARD_KRITISCH_PROZENT = 30.0


def lokale_laufwerksbuchstaben() -> list[str]:
    """Gibt alle Buchstaben fest eingebauter lokaler Laufwerke zurück (z.B.
    ["C", "D"]), ohne Wechseldatenträger, Netzlaufwerke oder CD/DVD-Laufwerke."""
    bitmaske = ctypes.windll.kernel32.GetLogicalDrives()
    buchstaben = []
    for index, buchstabe in enumerate(string.ascii_uppercase):
        # Bit "index" in der Bitmaske zeigt an, ob Laufwerk <buchstabe>: existiert,
        # siehe GetLogicalDrives()-Dokumentation. A=Bit 0, B=Bit 1, usw.
        if not (bitmaske & (1 << index)):
            continue
        laufwerkstyp = ctypes.windll.kernel32.GetDriveTypeW(f"{buchstabe}:\\")
        if laufwerkstyp == _DRIVE_FIXED:
            buchstaben.append(buchstabe)
    return buchstaben


def _extrahiere_prozent(text: str, *stichworte: str) -> int | None:
    """Sucht die erste Zeile, die alle übergebenen Stichworte enthält
    (Groß-/Kleinschreibung egal), und gibt die darin enthaltene Prozentzahl
    zurück. Wird benutzt, um z.B. "Gesamtfragmentierung = 3 %" zu finden,
    ohne auf die exakte Formatierung drumherum angewiesen zu sein."""
    for line in text.splitlines():
        untere_zeile = line.lower()
        if all(wort.lower() in untere_zeile for wort in stichworte):
            match = re.search(r"(\d+)\s*%", line)
            if match:
                return int(match.group(1))
    return None


def _extrahiere_wert(text: str, *stichworte: str) -> str | None:
    """Wie _extrahiere_prozent(), aber für Zeilen im Format "Label = Wert"
    ohne Prozentzeichen, zum Beispiel den Medientyp (SSD/HDD)."""
    for line in text.splitlines():
        untere_zeile = line.lower()
        if all(wort.lower() in untere_zeile for wort in stichworte) and "=" in line:
            return line.split("=", 1)[1].strip()
    return None


_ZAHL_RE = re.compile(r"[\d.,]+")


def _extrahiere_zahl(text: str, *stichworte: str) -> int | float | None:
    """Wie _extrahiere_wert(), wandelt den gefundenen Wert aber zusätzlich in
    eine Zahl um, zum Beispiel "6207" zu 6207 oder "1,01" (deutsches
    Dezimalkomma) zu 1.01. Für Zähler wie "Fragmentierte Dateien" oder
    "Durchschnittliche Fragmente pro Datei" aus dem Post-Defragmentation-
    Report von defrag /A /V (siehe Fehler-und-Lösungen-Abschnitt oben)."""
    wert = _extrahiere_wert(text, *stichworte)
    if wert is None:
        return None
    match = _ZAHL_RE.match(wert.strip())
    if not match:
        return None
    # Deutsches Zahlenformat: Punkt als Tausendertrenner, Komma als
    # Dezimaltrennzeichen, also erst die Punkte weg und dann Komma zu Punkt.
    bereinigt = match.group(0).replace(".", "").replace(",", ".")
    try:
        zahl = float(bereinigt)
    except ValueError:
        return None
    return int(zahl) if zahl.is_integer() else zahl


def _bewerte_fragmentierung(gesamt_prozent: int | None, ist_ssd: bool) -> str:
    """Ordnet den Fragmentierungswert eines Laufwerks in kritisch/warnung/ok/
    unbekannt ein, nach demselben Muster wie die Laufwerksplatz- und
    Treiberalter-Einstufung in analyze.py."""
    if ist_ssd:
        # SSDs werden nicht klassisch defragmentiert (das nutzt sich nur ab
        # und bringt bei wahlfreiem Zugriff nichts), Windows optimiert sie
        # stattdessen automatisch per TRIM/Retrim. Fragmentierung ist bei
        # einer SSD deshalb keine sinnvolle Kritikalität.
        return "ok"
    if gesamt_prozent is None:
        return "unbekannt"
    if gesamt_prozent >= STANDARD_KRITISCH_PROZENT:
        return "kritisch"
    if gesamt_prozent >= STANDARD_WARNUNG_PROZENT:
        return "warnung"
    return "ok"


def analysiere_laufwerk(buchstabe: str, timeout: int = 180) -> dict:
    """Führt `defrag <Buchstabe>: /A /V` aus (nur Analyse, verändert nichts)
    und wertet die Textausgabe aus. Braucht Administratorrechte, siehe
    Modul-Docstring."""
    ok, text = run_command(["defrag", f"{buchstabe}:", "/A", "/V"], timeout=timeout)

    # Beim Testen ohne Administratorrechte kam trotz der Fehlermeldung "Die
    # Speicheroptimierung kann nicht gestartet werden, da Sie keine
    # ausreichenden Berechtigungen..." ein Exit-Code 0 zurück, ok war also
    # fälschlich True. defrag.exe meldet diesen konkreten Fehler offenbar
    # nicht über den Rückgabewert, sondern nur über den Text. Deshalb wird
    # hier zusätzlich nach der bekannten Fehlermeldung gesucht, statt sich
    # allein auf run_command()s ok-Wert zu verlassen.
    ok = ok and "keine ausreichenden berechtigungen" not in text.lower()

    # Erst das alte, oft beschriebene Format versuchen ("Gesamtfragmentierung"),
    # dann das tatsächlich beobachtete aktuelle Format ("Fragmentierter
    # Speicherplatz insgesamt", siehe Dritter Fund im Modul-Docstring). Explizite
    # None-Prüfung statt "or": bei 0% Fragmentierung wäre "or" fälschlich zum
    # zweiten Versuch weitergesprungen, weil 0 in Python als falsy gilt.
    gesamt_prozent = _extrahiere_prozent(text, "Gesamtfragmentierung")
    if gesamt_prozent is None:
        gesamt_prozent = _extrahiere_prozent(text, "fragmentierter", "speicherplatz", "insgesamt")

    # Eine eigene Prozentangabe für Datei- bzw. freie-Speicherplatz-Fragmentierung
    # gibt es im aktuell beobachteten Format nicht mehr, nur noch absolute Zähler.
    fragmentierte_dateien = _extrahiere_zahl(text, "fragmentierte", "dateien")
    dateifragmente_gesamt = _extrahiere_zahl(text, "dateifragmente", "insgesamt")
    durchschnittliche_fragmente_pro_datei = _extrahiere_zahl(
        text, "durchschnittliche", "fragmente", "pro", "datei"
    )

    ist_ssd = "ssd" in text.lower() or "solid state" in text.lower()
    medientyp = _extrahiere_wert(text, "medientyp")
    if medientyp is None:
        medientyp = _extrahiere_wert(text, "datenträgertyp")

    # defrag schreibt am Ende meistens einen ganzen Satz mit einer klaren
    # Handlungsempfehlung ("Sie sollten dieses Volume defragmentieren." o.ä.).
    # Der wird als Rohsatz mitgegeben, weil er oft mehr Kontext liefert als
    # die reinen Prozentzahlen, aber nicht als alleinige Grundlage für
    # kritisch/warnung/ok benutzt (siehe _bewerte_fragmentierung()), weil der
    # genaue Wortlaut je nach Windows-Version variieren könnte.
    empfehlung_satz = next(
        (zeile.strip() for zeile in text.splitlines() if "defragmentier" in zeile.lower() and zeile.strip().endswith(".")),
        None,
    )

    status = _bewerte_fragmentierung(gesamt_prozent, ist_ssd)

    return {
        "laufwerk": f"{buchstabe}:",
        "erfolgreich": ok,
        "medientyp": medientyp,
        "ist_ssd": ist_ssd,
        "gesamtfragmentierung_prozent": gesamt_prozent,
        "fragmentierte_dateien_anzahl": fragmentierte_dateien,
        "dateifragmente_gesamt_anzahl": dateifragmente_gesamt,
        "durchschnittliche_fragmente_pro_datei": durchschnittliche_fragmente_pro_datei,
        "empfehlung_laut_defrag": empfehlung_satz,
        "status": status,
        "rohtext": text,
    }


def _speichere_bericht(output_dir: Path, bericht: dict) -> None:
    """Speichert den Fragmentierungsbericht unter output_dir: pro Laufwerk die
    komplette Rohausgabe von 'defrag /A /V' als eigene .txt-Datei (zum
    schnellen Nachschauen von Hand, ohne extra Python-Aufruf), und zusätzlich
    den kompletten Bericht (inklusive aller ausgewerteten Werte) als
    bericht.json, gleiches Muster wie _save_json() in parse.py/analyze.py."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for laufwerk in bericht["laufwerke"]:
        # "C:" -> "C", damit der Doppelpunkt nicht im Dateinamen landet
        # (unter Windows in Dateinamen sowieso nicht erlaubt).
        buchstabe = laufwerk["laufwerk"].rstrip(":")
        pfad = output_dir / f"{buchstabe}_rohtext.txt"
        pfad.write_text(laufwerk["rohtext"], encoding="utf-8")
        print(f"  gespeichert: {output_dir.name}/{pfad.name}")

    json_pfad = output_dir / "bericht.json"
    json_pfad.write_text(json.dumps(bericht, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  gespeichert: {output_dir.name}/{json_pfad.name}")


def pruefe_fragmentierung(
    laufwerke: list[str] | None = None,
    timeout: int = 180,
    output_dir: Path | str | None = None,
) -> dict:
    """Analysiert die Fragmentierung aller (oder der übergebenen) lokalen
    Laufwerke und speichert das Ergebnis unter output_dir (Standard:
    nfoanalyst/data/output/defrag). Gibt ein dict mit "admin" (bool, ob mit
    Adminrechten ausgeführt wurde) und "laufwerke" (Liste von
    analysiere_laufwerk()-Ergebnissen) zurück."""
    buchstaben = laufwerke if laufwerke is not None else lokale_laufwerksbuchstaben()
    ergebnisse = [analysiere_laufwerk(buchstabe, timeout=timeout) for buchstabe in buchstaben]
    bericht = {"admin": is_admin(), "laufwerke": ergebnisse}

    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    _speichere_bericht(output_dir, bericht)

    return bericht
