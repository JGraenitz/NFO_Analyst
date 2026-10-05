"""Formatiert bereits geparste/analysierte Daten aus data/output/ hübsch für
die Anzeige in der Kommandozeile. Reine Darstellungsschicht: rechnet selbst
nichts nach, sondern liest nur ein, was parse.py/analyze.py/green.py schon
berechnet und als JSON gespeichert haben.

Diese Funktionen werden von nfoanalyst.py als CLI-Befehle aufgerufen (siehe
COMMANDS dort), lassen sich aber auch einzeln importieren:

    from nfoanalyst.anzeige import zeige_laufwerke
    zeige_laufwerke()
"""

from __future__ import annotations

# json:     zum Einlesen der JSON-Berichte, die parse.py/analyze.py/green.py/ki.py
#           schon auf die Platte geschrieben haben (hier wird nichts geschrieben, nur gelesen).
# pathlib:  für den Pfad zu data/output/, siehe OUTPUT_DIR/REPORT_DIR unten.
import json
from pathlib import Path

# anzeige.py liegt direkt in nfoanalyst/, ein einziges .parent reicht deshalb,
# um von dieser Datei aus zum nfoanalyst-Ordner zu kommen (dort liegt auch data/).
# Wichtig: das ist relativ zum Speicherort DIESER DATEI, nicht zum Ordner, aus
# dem heraus man `python nfoanalyst.py` gerade aufruft. Deshalb funktioniert es
# auf jedem Rechner und unabhängig vom Arbeitsverzeichnis.
OUTPUT_DIR = Path(__file__).resolve().parent / "data" / "output"
REPORT_DIR = OUTPUT_DIR / "report"

# Falls die Ordner noch nicht existieren (z.B. ganz frischer Checkout), gleich anlegen.
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# Kurzform-Marker fürs Terminal statt Farben, die nicht überall funktionieren.
_STATUS_MARKER = {"kritisch": "!!", "warnung": " !", "ok": "  ", "unbekannt": " ?"}
_EINSTUFUNG_MARKER = {"hoch": "!!", "mittel": " !", "niedrig": "  ", "unbekannt": " ?"}


def _load(path: Path):
    if not path.exists():
        # path.relative_to(OUTPUT_DIR) statt des kompletten Pfads in der Meldung,
        # sonst hätte man bei jedem fehlenden Bericht den ganzen absoluten
        # Windows-Pfad im Terminal stehen. So bleibt nur der interessante Teil übrig.
        print(
            f"Keine Daten gefunden unter data/output/{path.relative_to(OUTPUT_DIR)}. "
            "Erst 'collect', 'parse' (und ggf. 'analyze'/'green') ausführen."
        )
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _kuerze(text: str | None, laenge: int) -> str:
    # "…" statt "..." spart zwei Zeichen Platz in den ohnehin schon engen
    # Tabellenspalten. Bei 116 Treibern in einer Zeile macht das schon etwas aus.
    text = text if text else "?"
    return text if len(text) <= laenge else text[: laenge - 1] + "…"


def _gb(n: int | None) -> str:
    return f"{n / 1_000_000_000:.1f} GB" if n is not None else "?"


def zeige_laufwerke() -> None:
    """Befehl 'laufwerke': zeigt alle Laufwerke mit Füllstand und Status."""
    daten = _load(REPORT_DIR / "laufwerke.json")
    if daten is None:
        return

    print(f"{'':3}{'Laufwerk':10}{'Größe':>10}{'Frei':>10}{'Frei %':>8}  Status")
    print("-" * 55)
    for lw in daten["laufwerke"]:
        marker = _STATUS_MARKER.get(lw["status"], " ?")
        frei_prozent = f"{lw['frei_prozent']}%" if lw["frei_prozent"] is not None else "?"
        print(
            f"{marker:<3}{_kuerze(lw['laufwerk'], 9):10}"
            f"{_gb(lw['groesse_bytes']):>10}"
            f"{_gb(lw['frei_bytes']):>10}"
            f"{frei_prozent:>8}  {lw['status']}"
        )

    z = daten["zusammenfassung"]
    print(
        f"\n{z['anzahl_kritisch']} kritisch (!!, unter {z['kritisch_unter_prozent']:.0f}% frei), "
        f"{z['anzahl_warnung']} Warnung (!, unter {z['warnung_unter_prozent']:.0f}% frei), "
        f"von {z['anzahl_laufwerke']} Laufwerken."
    )


def zeige_treiber() -> None:
    """Befehl 'treiber': zeigt ALLE Treiberpakete als nummerierte Liste (älteste
    zuerst) und alle unsignierten Geräte. Die Nummer vorne ist wichtig: über sie
    kann man mit 'ki treiber <Nummer>' eine KI-Zweitmeinung zu genau diesem
    Treiber einholen (siehe ki/ki.py)."""
    daten = _load(REPORT_DIR / "treiber.json")
    if daten is None:
        return

    z = daten["zusammenfassung"]
    print(
        f"{z['anzahl_treiberpakete']} Treiberpakete geprüft: "
        f"{z['anzahl_kritisch']} kritisch (ab {z['kritisch_ab_jahren']:.0f} Jahre), "
        f"{z['anzahl_warnung']} Warnung (ab {z['warnung_ab_jahren']:.0f} Jahre)."
    )
    print(f"{z['anzahl_signaturen_geprueft']} Signaturen geprüft, {z['anzahl_unsigniert']} unsigniert.\n")

    # start=1 ist hier wichtig: das ist genau die Nummer, die später bei
    # 'ki treiber <Nummer>' eingegeben wird. ki.py lädt Treiber über denselben
    # Index (treiberpakete[index - 1]), und die Liste kommt 1:1 aus derselben
    # treiber.json. Solange die Datei nicht neu erzeugt wird (also 'analyze'
    # nochmal läuft), bleibt die Nummerierung stabil.
    treiberpakete = daten["treiberpakete"]
    print(f"Alle Treiberpakete (älteste zuerst, {len(treiberpakete)} insgesamt):")
    print(f"{'':5}{'':3}{'Name':28}{'Version':16}{'Datum':>12}{'Alter':>8}")
    print("-" * 72)
    for i, t in enumerate(treiberpakete, start=1):
        marker = _STATUS_MARKER.get(t["status"], " ?")
        alter = f"{t['alter_jahre']:.0f} J" if t["alter_jahre"] is not None else "?"
        print(
            f"{i:>4}. {marker:<3}{_kuerze(t['originalname'], 27):28}"
            f"{_kuerze(t['version'], 15):16}"
            f"{(t['datum'] or '?'):>12}"
            f"{alter:>8}"
        )
    print("\nTipp: 'ki treiber <Nummer>' fragt eine KI, ob dieser Treiber wirklich veraltet ist.\n")

    unsigniert = daten["unsignierte_geraete"]
    if unsigniert:
        print(f"Unsignierte Geräte ({len(unsigniert)}):")
        for s in unsigniert:
            print(f"  !! {s['geraetename']} ({s['infname']}, Hersteller: {s['hersteller']})")
    else:
        print("Keine unsignierten Geräte gefunden.")


def _ja_nein(wert) -> str:
    if wert is True:
        return "ja"
    if wert is False:
        return "nein"
    return str(wert) if wert is not None else "?"


def zeige_ki_treiber_ergebnis(ergebnis: dict) -> None:
    """Zeigt das Ergebnis von ki.frage_ki_zu_treiber() hübsch an (Befehl 'ki treiber <Nummer>')."""
    treiber = ergebnis["treiber"]
    einschaetzung = ergebnis["ki_einschaetzung"]

    print(f"KI-Einschätzung zu Treiber #{ergebnis['index']}: {treiber['originalname']}")
    if treiber.get("version") or treiber.get("datum"):
        print(
            f"  Version {treiber['version']}, Datum {treiber['datum']}, "
            f"Alter {treiber['alter_jahre']} Jahre, Modell: {ergebnis['modell']}"
        )
    else:
        # Treiber aus msinfo32 (Quelle "msinfo32") haben kein Datum/keine Version,
        # dafür aber eine Beschreibung, siehe ki.baue_payload().
        print(f"  {treiber.get('beschreibung') or 'keine weiteren Angaben'}, Modell: {ergebnis['modell']}")

    # token_verbrauch kann None sein, falls die API das "usage"-Feld mal nicht
    # mitschickt (siehe ki._rufe_nvidia_api()). Deshalb "if verbrauch:" statt
    # die Schlüssel einfach anzunehmen.
    verbrauch = ergebnis.get("token_verbrauch")
    if verbrauch:
        print(
            f"  Token verbraucht: {verbrauch.get('prompt_tokens', '?')} Anfrage + "
            f"{verbrauch.get('completion_tokens', '?')} Antwort = {verbrauch.get('total_tokens', '?')} gesamt"
        )

    bisherige = treiber.get("bisherige_einstufung") or "unbekannt (kein Datum verfügbar)"
    print(f"  Bisherige Einstufung (Befehl 'analyze'): {bisherige}\n")

    # "rohtext" gibt es nur, wenn ki._extrahiere_json() kein gültiges JSON in
    # der KI-Antwort finden konnte (zum Beispiel wenn die KI trotz Anweisung
    # mit einem ganzen Satz statt nur JSON geantwortet hat). In diesem Fall
    # wird der Text lieber roh angezeigt, statt mit einem KeyError auf
    # einschaetzung.get(...) abzubrechen.
    if "rohtext" in einschaetzung:
        print("Antwort der KI (ließ sich nicht als JSON lesen, hier roh):")
        print(f"  {einschaetzung['rohtext']}")
        return

    print(f"  Wirklich veraltet laut KI:               {_ja_nein(einschaetzung.get('wirklich_veraltet'))}")
    print(f"  Vermutlich neuere Version verfügbar:     {_ja_nein(einschaetzung.get('vermutlich_neuere_version_verfuegbar'))}")
    print(f"  Weiterhin als kritisch einzustufen:      {_ja_nein(einschaetzung.get('weiterhin_kritisch'))}")
    print(f"  Empfehlung: {einschaetzung.get('empfehlung', '?')}")
    print(f"  Begründung: {einschaetzung.get('begruendung', '?')}")

    # zusatzfrage ist nur gesetzt, wenn beim Aufruf gerade eine persönliche
    # Zusatzfrage aktiv war (Befehl 'ki frage', siehe ki.lade_zusatzfrage()).
    if ergebnis.get("zusatzfrage"):
        print(f"\n  Zusatzfrage: {ergebnis['zusatzfrage']}")
        print(f"  Antwort: {einschaetzung.get('zusatzantwort', '?')}")


def zeige_autostart() -> None:
    """Befehl 'autostart': zeigt alle Autostart-Programme."""
    daten = _load(OUTPUT_DIR / "msinfo" / "autostartprogramme.json")
    if daten is None:
        return

    print(f"{len(daten)} Autostart-Einträge:\n")
    print(f"{'Programm':24}{'Benutzer':22}Ort/Befehl")
    print("-" * 70)
    for eintrag in daten:
        ort_oder_befehl = eintrag.get("ort") or eintrag.get("befehl") or "?"
        print(
            f"{_kuerze(eintrag.get('programm'), 23):24}"
            f"{_kuerze(eintrag.get('benutzername'), 21):22}"
            f"{_kuerze(ort_oder_befehl, 40)}"
        )


def zeige_problemgeraete() -> None:
    """Befehl 'problemgeraete': zeigt Geräte mit Problemen laut msinfo32."""
    daten = _load(OUTPUT_DIR / "msinfo" / "problemgeraete.json")
    if daten is None:
        return

    if not daten:
        print("Keine Problemgeräte gefunden.")
        return

    print(f"{len(daten)} Problemgerät(e):\n")
    for eintrag in daten:
        print(f"!! {eintrag.get('gerät')}")
        print(f"     PNP-Kennung: {eintrag.get('pnp_gerätekennung')}")
        print(f"     Fehler:      {eintrag.get('fehlercode')}")


def zeige_systemuebersicht() -> None:
    """Befehl 'systemuebersicht': zeigt die msinfo32-Systemübersicht als Tabelle."""
    daten = _load(OUTPUT_DIR / "msinfo" / "systemuebersicht.json")
    if daten is None:
        return

    # Ein paar Einträge sind eher ganze Sätze als kurze Schlüssel (zum Beispiel
    # ein Hyper-V-Hinweistext). Die Spaltenbreite wird deshalb gedeckelt, damit
    # die Tabelle lesbar bleibt.
    breite = min(max((len(k) for k in daten), default=0), 45)
    for key, value in daten.items():
        print(f"{_kuerze(key, breite):<{breite}} : {value}")


_EINSTUFUNG_TEXT = {"niedrig": "gut", "mittel": "mittel", "hoch": "schlecht", "unbekannt": "unbekannt"}


def zeige_greenreport() -> None:
    """Befehl 'greenreport': zeigt die Green-/CO2-Einschätzung inklusive
    Handlungsempfehlungen und Quellenangaben, was am Stromverbrauch verbessert
    werden kann."""
    daten = _load(REPORT_DIR / "green.json")
    if daten is None:
        return

    print(f"Gesamteinstufung: {_EINSTUFUNG_TEXT.get(daten['gesamteinstufung'], daten['gesamteinstufung'])}\n")

    # Kleine Hilfsfunktion nur für diese Stelle (nicht als eigene _funktion oben
    # im Modul), weil sie ausschließlich diese eine Zeilenform braucht und sich
    # sonst mit Parametern für Marker, Titel und Text unnötig aufblähen würde.
    def zeile(titel: str, teil: dict) -> None:
        marker = _EINSTUFUNG_MARKER.get(teil["einstufung"], " ?")
        print(f"{marker} {titel}: {teil['begruendung']}")
        if teil.get("empfehlung"):
            print(f"     -> {teil['empfehlung']}")

    zeile("Energieschema", daten["energieschema"])
    for teil in daten["timeouts"].values():
        zeile(teil["name"], teil)
    zeile("Autostart", daten["autostart"])

    co2 = daten["co2_schaetzung"]
    print(
        f"\nGeschätzter Mehrverbrauch: {co2['zusatzverbrauch_kwh_pro_jahr']} kWh/Jahr "
        f"(rund {co2['zusatz_co2_kg_pro_jahr']} kg CO2/Jahr) gegenüber einem sparsamen Setup."
    )
    print("Annahmen:")
    for annahme in co2["annahmen"]:
        print(f"  - {annahme}")

    quellen = daten.get("quellen")
    if quellen:
        print("\nQuellen:")
        for quelle in quellen:
            print(f"  - {quelle['thema']}: {quelle['quelle']}")
            print(f"    {quelle['url']}")
