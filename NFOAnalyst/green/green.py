"""Bewertung der energierelevanten Einstellungen eines Windows-Rechners aus Sicht
der Green- bzw. Umweltinformatik.

Geprüft werden das aktive Energieschema, die Zeitschaltwerte für Anzeige und
Ruhezustand sowie die Zahl der Autostart-Programme. Grundlage sind die von
parse.py erzeugten JSON-Dateien aus powercfg und msinfo32.

Nutzung als Bibliothek:

    from nfoanalyst.green.green import berechne
    bericht = berechne()                                    # nur Bewertung
    bericht = berechne(leerlauf_watt=8.7, wach_stunden=4)   # mit Hochrechnung

Grundsatz: Das Modul arbeitet ausschließlich mit Werten, die
(1) amtlich oder in einer Herstellerdokumentation belegt sind, (2) direkt aus
Windows ausgelesen werden oder (3) als Messwert bzw. Nutzungsangabe übergeben
werden. Fehlt für eine Rechnung ein Messwert, gibt das Modul keine Zahl aus,
sondern nennt, welcher Wert fehlt.

Zur Einordnung typischer Werte: ENERGY-STAR-zertifizierte Bürorechner liegen im
Leerlauf bei etwa 6 bis 9 W und im Ruhezustand bei etwa 1 bis 2 W (U.S. EPA,
ENERGY STAR Certified Computers). Diese Werte dienen nur der Orientierung und
gehen nicht in die Rechnung ein.
"""

from __future__ import annotations

# json:     zum Einlesen der powercfg-/Autostart-JSONs von parse.py und zum
#           Schreiben von green.json am Ende.
# pathlib:  für die Pfade zu data/output/ (Eingabe) und data/output/report/ (Ausgabe).
import json
from pathlib import Path

GREEN_DIR = Path(__file__).resolve().parent
NFOANALYST_DIR = GREEN_DIR.parent
DEFAULT_INPUT_DIR = NFOANALYST_DIR / "data" / "output"
DEFAULT_OUTPUT_DIR = NFOANALYST_DIR / "data" / "output" / "report"


# --------------------------------------------------------------------------
# Belegte Grenz- und Rechenwerte. Jeder Wert verweist auf einen Eintrag in
# QUELLEN. Geschätzte Werte gibt es in diesem Modul nicht.
# --------------------------------------------------------------------------

CO2_GRAMM_PRO_KWH = 344.0
"""CO2-Emissionsfaktor des deutschen Strommix in Gramm je kWh, Wert des
Umweltbundesamts für 2025 [UBA2026]. Der Faktor ändert sich jährlich
(2022: 433, 2023: 386, 2024: 363 bzw. revidiert 353, 2025: 344) und ist für
andere Bezugsjahre oder Länder auszutauschen."""

RUHEZUSTAND_MAX_WATT = 5.0
"""Höchstzulässige Leistungsaufnahme eines Desktop-Rechners im Ruhezustand nach
EU-Verordnung 617/2013, Anhang II Nr. 2.2 [EU6172013]. Die tatsächliche Leistung
eines konformen Geräts liegt darunter. Wird dieser Grenzwert von der gemessenen
Leerlaufleistung abgezogen, ergibt sich daher eine Untergrenze für die Einsparung
durch den Ruhezustand."""

RUHEZUSTAND_WOL_ZUSCHLAG_WATT = 0.7
"""Zusätzlich zulässige Leistung im Ruhezustand, wenn Wake-on-LAN aktiv ist,
EU-Verordnung 617/2013, Anhang II Nr. 2.4 [EU6172013]."""

ANZEIGE_AUS_MAX_MINUTEN = 10
"""Die Anzeige muss nach höchstens 10 Minuten Inaktivität abschalten,
EU-Verordnung 617/2013, Anhang II Nr. 6.2.3 [EU6172013]."""

RUHEZUSTAND_MAX_MINUTEN = 30
"""Der Rechner muss nach höchstens 30 Minuten Inaktivität in den Ruhezustand
wechseln, EU-Verordnung 617/2013, Anhang II Nr. 6.2.5 [EU6172013]."""

PROZESSOR_MIN_ZUSTAND_OHNE_SKALIERUNG = 100
"""Minimaler Leistungszustand des Prozessors in Prozent (powercfg-Alias
PROCTHROTTLEMIN [MSMINPERF]), bei dem die Leistung nicht mehr dynamisch an die
Last angepasst wird. Microsoft beschreibt genau dieses Verhalten für das Schema
"Höchstleistung": Windows versetzt das System in den höchsten Leistungszustand
und schaltet die dynamische Skalierung ab [MSKB2207548]."""

TAGE_PRO_JAHR = 365

QUELLEN = [
    {
        "kuerzel": "UBA2026",
        "quelle": "Umweltbundesamt: CO2-Emissionen pro Kilowattstunde Strom 2025 nur leicht gesunken (2026)",
        "url": "https://www.umweltbundesamt.de/themen/co2-emissionen-pro-kilowattstunde-strom-2025-nur",
        "aussage": "344 g CO2 je kWh für 2025, revidierter Wert für 2024: 353 g/kWh.",
    },
    {
        "kuerzel": "EU6172013",
        "quelle": "Verordnung (EU) Nr. 617/2013 der Kommission vom 26. Juni 2013 (Ökodesign-Anforderungen an "
                  "Computer), ABl. L 175 vom 27.6.2013, S. 13",
        "url": "https://eur-lex.europa.eu/eli/reg/2013/617/oj",
        "aussage": "Anhang II: Ruhezustand bei Desktop-Rechnern höchstens 5,00 W (Nr. 2.2), mit Wake-on-LAN "
                   "zuzüglich 0,70 W (Nr. 2.4). Anzeige schaltet nach höchstens 10 Minuten ab (Nr. 6.2.3), "
                   "Ruhezustand nach höchstens 30 Minuten Inaktivität (Nr. 6.2.5).",
    },
    {
        "kuerzel": "MSKB2207548",
        "quelle": "Microsoft Support: Slow performance on Windows Server when using the Balanced power plan",
        "url": "https://support.microsoft.com/kb/2207548",
        "aussage": "Beim Schema 'Höchstleistung' versetzt Windows das System in den höchsten Leistungszustand "
                   "und schaltet die dynamische Anpassung an die Last ab.",
    },
    {
        "kuerzel": "MSMINPERF",
        "quelle": "Microsoft Learn: MinPerformance (Windows-Energieeinstellungen, powercfg-Alias PROCTHROTTLEMIN)",
        "url": "https://learn.microsoft.com/en-us/windows-hardware/customize/power-settings/"
               "options-for-perf-state-engine-minperformance",
        "aussage": "Gibt den minimalen Leistungszustand des Prozessors in Prozent der maximalen Leistung an "
                   "(0 bis 100).",
    },
    {
        "kuerzel": "DAYEM2016",
        "quelle": "Dayem, May-Ostendorp, Mercier (Xergy Consulting): Determining a Real-World Adjustment Factor "
                  "for Computer Energy Use, California Energy Commission, Docket 14-AAER-02, TN 211731 (2016)",
        "url": "https://efiling.energy.ca.gov/GetDocument.aspx?tn=211731",
        "aussage": "Laborversuche: Hintergrundsoftware erhöht die Leerlaufleistung, bei drei Desktop-Rechnern "
                   "im Kurzleerlauf um den Faktor 1,0 bis 1,1. Ein Wert je Autostart-Eintrag wird nicht "
                   "angegeben, das Modul rechnet deshalb keine kWh für den Autostart.",
    },
]
"""Quellen der Grenz- und Rechenwerte. Werden im Bericht mit ausgegeben."""


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


def _hex_zu_int(value) -> int | None:
    # powercfg speichert Werte als Hex-String, z.B. "0x000004b0" für 1200 Sekunden.
    if value is None:
        return None
    try:
        return int(str(value), 16)
    except ValueError:
        return None


def _finde_einstellung_nach_alias(einstellungen: dict, alias: str) -> dict | None:
    """Sucht in der von parse_powercfg() gebauten Baumstruktur nach einer
    Einstellung über ihren GUID-Alias (z.B. "VIDEOIDLE"). Die Aliase sind von
    Microsoft fest vergeben und damit unabhängig von der Systemsprache."""
    for untergruppe in einstellungen.get("untergruppen", []):
        for einstellung in untergruppe.get("einstellungen", []):
            if einstellung.get("alias") == alias:
                return einstellung
    return None


def _wert_netzbetrieb(einstellungen: dict, alias: str) -> int | None:
    """Liest den aktuellen Wert einer Einstellung im Netzbetrieb (Wechselstrom)."""
    einstellung = _finde_einstellung_nach_alias(einstellungen, alias)
    return _hex_zu_int(einstellung.get("aktueller_wert_wechselstrom")) if einstellung else None


# --------------------------------------------------------------------------
# Einzelbewertungen. Mögliche Einstufungen:
#   "niedrig"         belegter Grenzwert bzw. dokumentiertes Sparverhalten eingehalten
#   "hoch"            belegter Grenzwert überschritten bzw. Skalierung abgeschaltet
#   "unbekannt"       Einstellung konnte nicht gelesen werden
#   "nicht bewertet"  es gibt keinen belegten Grenzwert, nur Information
# --------------------------------------------------------------------------

_SCHEMA_NAMEN = {
    "SCHEME_MIN": "Höchstleistung",
    "SCHEME_BALANCED": "Ausbalanciert",
    "SCHEME_MAX": "Energiesparmodus",
}


def bewerte_energieschema(schemen: list[dict], einstellungen: dict) -> dict:
    """Bewertet das aktive Energieschema anhand des tatsächlich eingestellten
    minimalen Prozessorzustands (PROCTHROTTLEMIN) statt anhand des Namens.
    Ein umbenanntes oder angepasstes Schema wird dadurch richtig erkannt."""
    aktiv = next((s for s in schemen if s.get("aktiv")), None)
    alias = einstellungen.get("alias")
    min_zustand = _wert_netzbetrieb(einstellungen, "PROCTHROTTLEMIN")

    if min_zustand is None:
        einstufung = "unbekannt"
        begruendung = "Minimaler Prozessorzustand (PROCTHROTTLEMIN) nicht gefunden."
        empfehlung = None
    elif min_zustand >= PROZESSOR_MIN_ZUSTAND_OHNE_SKALIERUNG:
        einstufung = "hoch"
        begruendung = (f"Minimaler Prozessorzustand {min_zustand} %: Der Prozessor bleibt im höchsten "
                       "Leistungszustand, die dynamische Anpassung an die Last ist abgeschaltet [MSKB2207548].")
        empfehlung = ("Auf 'Ausbalanciert' wechseln, zum Beispiel mit 'powercfg /setactive SCHEME_BALANCED', "
                      "oder den minimalen Prozessorzustand senken.")
    else:
        einstufung = "niedrig"
        begruendung = (f"Minimaler Prozessorzustand {min_zustand} %: Die Leistung des Prozessors wird "
                       "an die Last angepasst.")
        empfehlung = None

    return {
        "name": aktiv.get("name") if aktiv else einstellungen.get("name"),
        "alias": alias,
        "standardschema": _SCHEMA_NAMEN.get(alias),
        "min_prozessorzustand_prozent": min_zustand,
        "einstufung": einstufung,
        "begruendung": begruendung,
        "empfehlung": empfehlung,
    }


# Alias zu (Anzeigename, belegter Höchstwert in Minuten oder None, Befehl zum Setzen).
_TIMEOUTS = {
    "VIDEOIDLE": ("Anzeige ausschalten nach", ANZEIGE_AUS_MAX_MINUTEN,
                  f"powercfg /change monitor-timeout-ac {ANZEIGE_AUS_MAX_MINUTEN}"),
    "STANDBYIDLE": ("Ruhezustand (Standby) nach", RUHEZUSTAND_MAX_MINUTEN,
                    f"powercfg /change standby-timeout-ac {RUHEZUSTAND_MAX_MINUTEN}"),
    # Für Festplatte und Ruhezustand auf Datenträger (Hibernate) gibt es keinen
    # belegten Grenzwert. Die Werte werden nur zur Information ausgegeben.
    "HIBERNATEIDLE": ("Ruhezustand auf Datenträger nach", None, None),
    "DISKIDLE": ("Festplatte ausschalten nach", None, None),
}


def _bewerte_timeout(sekunden: int | None, max_minuten: int | None, befehl: str | None):
    if sekunden is None:
        return "unbekannt", "Einstellung wurde nicht gefunden.", None
    text = "nie" if sekunden == 0 else f"nach {sekunden / 60:.0f} Minuten"
    if max_minuten is None:
        return "nicht bewertet", f"Steht auf '{text}'. Dafür gibt es keinen belegten Grenzwert.", None
    if sekunden == 0 or sekunden / 60 > max_minuten:
        return ("hoch",
                f"Steht auf '{text}'. Die EU-Verordnung 617/2013 sieht höchstens {max_minuten} Minuten vor "
                "[EU6172013].",
                f"Auf höchstens {max_minuten} Minuten setzen, zum Beispiel mit '{befehl}'.")
    return ("niedrig",
            f"Steht auf '{text}' und hält damit den Höchstwert von {max_minuten} Minuten ein [EU6172013].",
            None)


def bewerte_timeouts(einstellungen: dict) -> dict:
    """Prüft die Zeitschaltwerte im Netzbetrieb gegen die Vorgaben der
    EU-Verordnung 617/2013, Anhang II Nr. 6.2.3 und 6.2.5."""
    ergebnis = {}
    for alias, (anzeigename, max_minuten, befehl) in _TIMEOUTS.items():
        sekunden = _wert_netzbetrieb(einstellungen, alias)
        einstufung, begruendung, empfehlung = _bewerte_timeout(sekunden, max_minuten, befehl)
        ergebnis[alias.lower()] = {
            "name": anzeigename,
            "sekunden": sekunden,
            "hoechstwert_minuten": max_minuten,
            "einstufung": einstufung,
            "begruendung": begruendung,
            "empfehlung": empfehlung,
        }
    return ergebnis


def bewerte_autostart(autostart: list[dict]) -> dict:
    """Gibt die Zahl der Autostart-Einträge aus. Hintergrundsoftware erhöht die
    Leerlaufleistung nachweislich [DAYEM2016], einen belegten Wert je Eintrag gibt
    es aber nicht. Der Autostart wird deshalb nicht eingestuft und fließt weder in
    die Gesamteinstufung noch in die Hochrechnung ein."""
    anzahl = len(autostart)
    return {
        "anzahl": anzahl,
        "einstufung": "nicht bewertet",
        "begruendung": (f"{anzahl} Autostart-Einträge gefunden. Hintergrundsoftware kann die Leerlaufleistung "
                        "erhöhen [DAYEM2016], ein belegter Wert je Eintrag existiert nicht."),
        "empfehlung": ("Autostart-Liste prüfen und nicht benötigte Programme deaktivieren "
                       "(Task-Manager > Autostart). Die Wirkung lässt sich nur durch eine Messung beziffern."
                       if anzahl else None),
    }


# --------------------------------------------------------------------------
# Hochrechnung, nur mit Messwerten
# --------------------------------------------------------------------------

def hochrechnung(standby_einstufung: str, schema_einstufung: str,
                 leerlauf_watt: float | None = None, wach_stunden: float | None = None,
                 schema_differenz_watt: float | None = None, betrieb_stunden: float | None = None,
                 ruhezustand_watt: float | None = None) -> dict:
    """Rechnet Energie und CO2 nur dann hoch, wenn die nötigen Messwerte bzw.
    Nutzungsangaben übergeben wurden. Andernfalls wird angegeben, was fehlt.

    leerlauf_watt          gemessene Leerlaufleistung des Rechners in W
    wach_stunden           Stunden je Tag, die der Rechner wach statt im Ruhezustand ist
    schema_differenz_watt  gemessener Unterschied der Leerlaufleistung zwischen dem
                           aktiven Schema und 'Ausbalanciert' in W
    betrieb_stunden        Stunden je Tag, die der Rechner eingeschaltet ist
    ruhezustand_watt       gemessene Leistung im Ruhezustand in W. Ohne Messwert wird der
                           gesetzliche Höchstwert verwendet, das Ergebnis ist dann eine Untergrenze.
    """
    posten, offen = [], []

    if standby_einstufung == "hoch":
        if leerlauf_watt is None:
            offen.append("Ruhezustand: gemessene Leerlaufleistung fehlt (Parameter leerlauf_watt).")
        else:
            if ruhezustand_watt is None:
                ruhe, ruhe_text = RUHEZUSTAND_MAX_WATT, f"{RUHEZUSTAND_MAX_WATT:g} W Grenzwert"
                hinweis = ("Untergrenze, weil die tatsächliche Leistung im Ruhezustand unter dem "
                           "Grenzwert liegt [EU6172013].")
            else:
                ruhe, ruhe_text = ruhezustand_watt, f"{ruhezustand_watt:g} W gemessen"
                hinweis = "Beide Leistungswerte gemessen."
            je_stunde_wh = max(0.0, leerlauf_watt - ruhe)
            eintrag = {
                "posten": "Ruhezustand wird zu spät oder nie erreicht",
                "rechnung": f"({leerlauf_watt:g} W gemessen - {ruhe_text}) je Stunde",
                "wh_je_stunde": round(je_stunde_wh, 2),
                "hinweis": hinweis,
            }
            if wach_stunden is None:
                offen.append("Ruhezustand: Zahl der unnötigen Wachstunden je Tag fehlt (Parameter wach_stunden).")
            else:
                eintrag["kwh_pro_jahr"] = round(je_stunde_wh * wach_stunden * TAGE_PRO_JAHR / 1000, 2)
            posten.append(eintrag)

    if schema_einstufung == "hoch":
        if schema_differenz_watt is None or betrieb_stunden is None:
            offen.append("Energieschema: gemessener Leistungsunterschied zu 'Ausbalanciert' und Betriebsstunden "
                         "je Tag fehlen (Parameter schema_differenz_watt, betrieb_stunden). Ein belegter "
                         "Pauschalwert existiert nicht, weil die Wirkung von der Hardware abhängt.")
        else:
            posten.append({
                "posten": "Energieschema ohne dynamische Skalierung",
                "rechnung": f"{schema_differenz_watt:g} W gemessen x {betrieb_stunden:g} h je Tag",
                "kwh_pro_jahr": round(schema_differenz_watt * betrieb_stunden * TAGE_PRO_JAHR / 1000, 2),
            })

    summe_kwh = sum(p.get("kwh_pro_jahr", 0.0) for p in posten)
    berechnet = any("kwh_pro_jahr" in p for p in posten)
    return {
        "berechnet": berechnet,
        "zusatzverbrauch_kwh_pro_jahr": round(summe_kwh, 2) if berechnet else None,
        "zusatz_co2_kg_pro_jahr": round(summe_kwh * CO2_GRAMM_PRO_KWH / 1000, 2) if berechnet else None,
        "co2_faktor_g_pro_kwh": CO2_GRAMM_PRO_KWH,
        "posten": posten,
        "fehlende_angaben": offen,
    }


def _gesamteinstufung(einstufungen: list[str]) -> str:
    """Die Gesamteinstufung ist die ungünstigste belegte Einzelbewertung. Ein
    Punkteschema mit frei gewählten Schwellen wird bewusst nicht verwendet.
    'nicht bewertet' und 'unbekannt' gehen nicht ein."""
    bewertet = [e for e in einstufungen if e in ("niedrig", "hoch")]
    if not bewertet:
        return "unbekannt"
    return "hoch" if "hoch" in bewertet else "niedrig"


# --------------------------------------------------------------------------
# Gesamtbericht
# --------------------------------------------------------------------------

def berechne(
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
    leerlauf_watt: float | None = None,
    wach_stunden: float | None = None,
    schema_differenz_watt: float | None = None,
    betrieb_stunden: float | None = None,
    ruhezustand_watt: float | None = None,
) -> dict:
    """Lädt die von parse.py erzeugten powercfg- und Autostart-JSONs, bewertet
    Energieschema, Zeitschaltwerte und Autostart und rechnet Energie und CO2 hoch,
    soweit Messwerte übergeben wurden. Speichert das Ergebnis als green.json
    (inklusive der Quellen) und gibt es zusätzlich zurück."""
    input_dir = Path(input_dir) if input_dir else DEFAULT_INPUT_DIR
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR

    print("[green] bewerte Energieschema, Zeitschaltwerte und Autostart...")
    schemen = _load_json(input_dir / "powercfg" / "schemen.json")
    einstellungen = _load_json(input_dir / "powercfg" / "einstellungen.json")
    autostart = _load_json(input_dir / "msinfo" / "autostartprogramme.json")

    schema_bewertung = bewerte_energieschema(schemen, einstellungen)
    timeout_bewertung = bewerte_timeouts(einstellungen)
    autostart_bewertung = bewerte_autostart(autostart)

    energie = hochrechnung(
        timeout_bewertung["standbyidle"]["einstufung"], schema_bewertung["einstufung"],
        leerlauf_watt=leerlauf_watt, wach_stunden=wach_stunden,
        schema_differenz_watt=schema_differenz_watt, betrieb_stunden=betrieb_stunden,
        ruhezustand_watt=ruhezustand_watt,
    )

    gesamt = _gesamteinstufung(
        [schema_bewertung["einstufung"]] + [t["einstufung"] for t in timeout_bewertung.values()]
    )

    bericht = {
        "gesamteinstufung": gesamt,
        "energieschema": schema_bewertung,
        "timeouts": timeout_bewertung,
        "autostart": autostart_bewertung,
        "energie_hochrechnung": energie,
        "quellen": QUELLEN,
    }

    _save_json(output_dir / "green.json", bericht)
    print("[green] fertig.")
    return bericht


def main() -> int:
    """CLI-Wrapper um berechne() für den direkten Aufruf `python green.py`."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--leerlauf-watt", type=float, default=None,
                        help="gemessene Leerlaufleistung des Rechners in W")
    parser.add_argument("--wach-stunden", type=float, default=None,
                        help="Stunden je Tag, die der Rechner wach statt im Ruhezustand ist")
    parser.add_argument("--schema-differenz-watt", type=float, default=None,
                        help="gemessener Leistungsunterschied des aktiven Schemas zu 'Ausbalanciert' in W")
    parser.add_argument("--betrieb-stunden", type=float, default=None,
                        help="Stunden je Tag, die der Rechner eingeschaltet ist")
    parser.add_argument("--ruhezustand-watt", type=float, default=None,
                        help="gemessene Leistung im Ruhezustand in W (ohne Angabe: Grenzwert 5 W)")
    args = parser.parse_args()

    berechne(input_dir=args.input_dir, output_dir=args.output_dir,
             leerlauf_watt=args.leerlauf_watt, wach_stunden=args.wach_stunden,
             schema_differenz_watt=args.schema_differenz_watt, betrieb_stunden=args.betrieb_stunden,
             ruhezustand_watt=args.ruhezustand_watt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
