"""Interaktiver Einstiegspunkt für NFOAnalyst.

Beim Start öffnet sich eine Eingabeaufforderung, in die Befehle eingetippt
werden können. Der eingegebene Text wird mit den bekannten Befehlsnamen
verglichen (siehe COMMANDS) und die passende Funktion wird ausgeführt.

Aufruf:  python nfoanalyst.py
Befehle: collect, parse, analyze, green, laufwerke, treiber,
         ki treiber <Nummer> [Frage] | ki frage [Text|loeschen] | ki modell [Name] |
         ki modelle [Suchtext] | ki test [Modell] | ki test alle [Suchtext] | ki reset,
         autostart, problemgeraete, systemuebersicht, greenreport, cache,
         cacheleeren, defrag, hilfe, ende

Als Administrator starten (nötig für den vollen Umfang von collect,
z.B. pnputil /enum-drivers oder powercfg /energy):
  1. PowerShell oder Windows Terminal per Rechtsklick "Als Administrator
     ausführen" öffnen, dann wie gewohnt in den Ordner wechseln und
     `python nfoanalyst.py` starten.
  2. Oder aus einer normalen (nicht erhöhten) PowerShell heraus ein neues
     erhöhtes Fenster öffnen lassen:
     Start-Process python -ArgumentList "nfoanalyst.py" -Verb RunAs
     (Windows fragt dann per UAC-Dialog nach, ob das erlaubt werden soll.)
"""

# from __future__ import annotations: sorgt dafür, dass Typ-Hints wie
# "list[str]" oder "int | None" nicht sofort ausgewertet werden, sondern nur
# als Text im Hintergrund stehen. Ohne die Zeile würden manche der neueren
# Schreibweisen in diesem Projekt (z.B. in ki.py/green.py) auf älteren
# Python-Versionen mit einem Fehler abbrechen.
from __future__ import annotations

# Die eigentliche Arbeit passiert nicht in dieser Datei, sondern in den
# Geschwisterordnern. nfoanalyst.py bindet hier nur alles zu einem Menü zusammen:
#   collect/collect.py  -> sammelt die Windows-Rohdaten (driverquery, pnputil, msinfo32, powercfg)
#   parse/parse.py      -> wandelt die Rohdaten in sauberes JSON um
#   analyze/analyze.py  -> Kritikalitäts-Check (Treiberalter, Signatur, Laufwerksplatz)
#   green/green.py      -> CO2-/Energie-Schätzung
#   ki/ki.py             -> KI-Zweitmeinung zu einzelnen Treibern über build.nvidia.com
#   cleanup/cleanup.py   -> prüft/leert die Windows-Temp-/Cache-Ordner (unabhängig von collect/parse/analyze)
#   defrag/defrag.py     -> prüft die Fragmentierung aller lokalen Laufwerke (auch unabhängig von collect/parse/analyze)
#   anzeige.py           -> formatiert die schon berechneten Berichte hübsch für die Konsole
from analyze.analyze import erstelle_bericht
from anzeige import (
    zeige_autostart,
    zeige_greenreport,
    zeige_ki_treiber_ergebnis,
    zeige_laufwerke,
    zeige_problemgeraete,
    zeige_systemuebersicht,
    zeige_treiber,
)
from cleanup.cleanup import leere_cache, pruefe_cache
from collect.collect import collect
from defrag.defrag import pruefe_fragmentierung
from green.green import berechne as green_berechne
from ki.ki import (
    aktuelles_modell,
    frage_ki_zu_treiber,
    lade_zusatzfrage,
    liste_modelle,
    loesche_zusatzfrage,
    modell_existiert,
    setze_modell,
    setze_modell_standard,
    setze_zusatzfrage,
    teste_katalog,
    teste_modell,
)
from parse.parse import parse


def cmd_collect() -> None:
    """Beispielbefehl: ruft die Sammel-Funktion aus collect/collect.py auf."""
    print("Starte Datensammlung (driverquery, pnputil, msinfo32, powercfg)...")
    try:
        zielordner = collect()
        print(f"Fertig. Daten liegen in: {zielordner}")
    except RuntimeError as exc:
        # collect() wirft RuntimeError nur, wenn man nicht unter Windows ist.
        # Die Windows-Tools (driverquery etc.) gibt es auf anderen Systemen nicht.
        print(f"Fehler: {exc}")


def cmd_parse() -> None:
    """Ruft die Parse-Funktion aus parse/parse.py auf und wandelt die gesammelten
    Rohdaten in strukturiertes JSON unter data/output/ um."""
    print("Parse gesammelte Rohdaten...")
    try:
        parse()
    except FileNotFoundError as exc:
        # Passiert zum Beispiel, wenn man 'parse' aufruft, bevor 'collect' gelaufen ist.
        # Dann fehlen die Rohdateien in data/input/ noch komplett.
        print(f"Fehler: {exc}. Erst 'collect' ausführen?")


def cmd_analyze() -> None:
    """Ruft erstelle_bericht() aus analyze/analyze.py auf: prüft Treiberalter,
    Signaturen und freien Laufwerksspeicher und speichert mehrere Berichte
    unter data/output/report/ (inklusive der Green-Einschätzung aus green.py)."""
    print("Analysiere Treiber und Laufwerke...")
    try:
        bericht = erstelle_bericht()
    except FileNotFoundError as exc:
        # analyze() braucht die JSON-Dateien aus 'parse' und nicht direkt die
        # Rohdaten aus 'collect'. Deshalb werden hier beide Vorstufen als
        # möglicher Hinweis genannt.
        print(f"Fehler: {exc}. Erst 'collect' und 'parse' ausführen?")
        return

    # erstelle_bericht() gibt viele Daten zurück (Treiber, Laufwerke, Green,
    # Gesamtbericht). Für dieses Menü interessiert aber nur die kurze Liste an
    # kritischen Punkten aus dem Gesamtbericht. Der Rest lässt sich über die
    # eigenen Befehle 'laufwerke', 'treiber' und 'greenreport' im Detail ansehen.
    kritische_punkte = bericht["gesamtbericht"]["kritische_punkte"]
    if kritische_punkte:
        print(f"\n{len(kritische_punkte)} kritische Punkte gefunden:")
        for punkt in kritische_punkte:
            print(f"  - {punkt}")
    else:
        print("\nKeine kritischen Punkte gefunden.")


def cmd_green() -> None:
    """Ruft nur die Green-/CO2-Einschätzung aus green/green.py auf (ohne die
    Treiber-/Laufwerksanalyse aus 'analyze')."""
    print("Berechne Green-/CO2-Einschätzung...")
    try:
        green_berechne()
    except FileNotFoundError as exc:
        print(f"Fehler: {exc}. Erst 'collect' und 'parse' ausführen?")


def cmd_laufwerke() -> None:
    """Zeigt die Laufwerke aus dem letzten 'analyze'-Lauf mit !!/!-Markern
    für kritisch/Warnung (siehe anzeige.py)."""
    zeige_laufwerke()


def cmd_treiber() -> None:
    """Zeigt Datum, Version und Alter der ältesten Treiber sowie alle
    unsignierten Geräte aus dem letzten 'analyze'-Lauf (siehe anzeige.py)."""
    zeige_treiber()


def cmd_autostart() -> None:
    """Zeigt alle Autostart-Programme aus dem letzten 'parse'-Lauf (siehe anzeige.py)."""
    zeige_autostart()


def cmd_problemgeraete() -> None:
    """Zeigt alle laut msinfo32 gemeldeten Problemgeräte (siehe anzeige.py)."""
    zeige_problemgeraete()


def cmd_systemuebersicht() -> None:
    """Zeigt die msinfo32-Systemübersicht als Tabelle (siehe anzeige.py)."""
    zeige_systemuebersicht()


def cmd_greenreport() -> None:
    """Zeigt den letzten Green-/CO2-Bericht inklusive Empfehlungen und Quellen,
    was am Stromverbrauch verbessert werden kann (siehe anzeige.py)."""
    zeige_greenreport()


def _cmd_ki_treiber(args: tuple[str, ...]) -> None:
    """Unterbefehl 'ki treiber <Nummer> [Frage]': schickt die Daten des
    Treibers mit dieser Nummer (siehe Befehl 'treiber') an eine KI
    (build.nvidia.com, siehe ki/ki.py) und fragt, ob er wirklich veraltet
    ist, ob es eine neuere Version gibt und was eine sinnvolle
    Handlungsempfehlung wäre. Zusätzliche Wörter nach der Nummer werden als
    einmalige Zusatzfrage NUR für diesen Aufruf an die KI mitgestellt, ohne
    eine mit 'ki frage' gespeicherte Zusatzfrage zu verändern."""
    # args[0].isdigit() prüft nur, ob es eine positive Zahl ist, bevor int()
    # darauf angewendet wird. Ohne diese Prüfung würde zum Beispiel
    # "ki treiber abc" mit einer unklaren ValueError-Meldung abbrechen,
    # statt eine verständliche Hinweismeldung auszugeben.
    if not args or not args[0].isdigit():
        print("Nutzung: ki treiber <Nummer> [Frage]  (Nummer siehe Befehl 'treiber')")
        return

    nummer = int(args[0])
    # Weitere Wörter nach der Nummer sind eine einmalige Frage nur zu diesem
    # einen Treiber, zum Beispiel "ki treiber 5 Ist das ein Sicherheitsrisiko?".
    einmalige_frage = " ".join(args[1:]) if len(args) > 1 else None

    print(f"Frage KI zu Treiber Nummer {nummer}...")
    try:
        ergebnis = frage_ki_zu_treiber(nummer, zusatzfrage=einmalige_frage)
    except (FileNotFoundError, IndexError, RuntimeError) as exc:
        # Drei ganz unterschiedliche Fehlerquellen landen hier zusammen:
        # FileNotFoundError = kein treiber.json (also erst 'analyze' fehlt),
        # IndexError = die Nummer gibt es in der Liste gar nicht,
        # RuntimeError = z.B. fehlender API-Key oder ein Fehler von der NVIDIA-API.
        print(f"Fehler: {exc}")
        return

    zeige_ki_treiber_ergebnis(ergebnis)


def _cmd_ki_frage(args: tuple[str, ...]) -> None:
    """Unterbefehl 'ki frage [Text|loeschen]': setzt, zeigt oder entfernt
    eine persönliche Zusatzfrage, die ab dann bei jedem 'ki treiber <Nummer>'
    zusätzlich zum Standard-Prompt an die KI gestellt wird (siehe
    ki.lade_zusatzfrage())."""
    if not args:
        aktuell = lade_zusatzfrage()
        if aktuell:
            print(f"Aktuelle Zusatzfrage: {aktuell}")
        else:
            print("Keine Zusatzfrage gesetzt.")
        print("Nutzung: 'ki frage <Text>' zum Setzen, 'ki frage loeschen' zum Entfernen.")
        return

    if len(args) == 1 and args[0].lower() == "loeschen":
        loesche_zusatzfrage()
        print("Zusatzfrage entfernt.")
        return

    text = " ".join(args)
    setze_zusatzfrage(text)
    print(f"Zusatzfrage gesetzt: {text}")
    print("Wird ab jetzt bei jedem 'ki treiber <Nummer>' zusätzlich gestellt.")


def _cmd_ki_modell(args: tuple[str, ...]) -> None:
    """Unterbefehl 'ki modell [Name]': zeigt das aktuell benutzte Modell oder
    setzt ein neues (schreibt NVIDIA_MODEL in die .env, siehe
    ki.setze_modell())."""
    if not args:
        print(f"Aktuelles Modell: {aktuelles_modell()}")
        print("Nutzung: 'ki modell <Name>' zum Wechseln, 'ki modelle' zeigt verfügbare Modelle.")
        return

    name = args[0]
    setze_modell(name)
    print(f"Modell gesetzt: {name}")

    # Prüft den Namen gegen den echten Katalog, um Tippfehler sofort zu
    # melden (z.B. 'ki modell chat' statt eines echten Modellnamens), statt
    # dass das erst beim nächsten 'ki treiber' mit einem verwirrenden 404
    # auffällt. existiert ist None, wenn der Katalog gerade nicht abgefragt
    # werden konnte (z.B. NVIDIA-Infrastruktur down), dann wird nichts
    # behauptet, was nicht geprüft werden konnte.
    existiert = modell_existiert(name)
    if existiert is False:
        print(f"Warnung: '{name}' taucht nicht im aktuellen Modell-Katalog auf. Tippfehler? Siehe 'ki modelle'.")
    else:
        print(
            "Hinweis: nicht jedes Modell aus dem Katalog ist mit dem eigenen Account "
            "nutzbar, am besten kurz mit 'ki treiber <Nummer>' ausprobieren."
        )


def _cmd_ki_modelle(args: tuple[str, ...]) -> None:
    """Unterbefehl 'ki modelle [Suchtext]': fragt den Modell-Katalog live bei
    build.nvidia.com ab (siehe ki.liste_modelle())."""
    suchtext = args[0] if args else None
    print("Frage verfügbare Modelle bei build.nvidia.com ab...")
    try:
        modelle = liste_modelle(suchtext)
    except RuntimeError as exc:
        print(f"Fehler: {exc}")
        return

    if not modelle:
        print("Keine Modelle gefunden." + (f" (Suchtext: {suchtext})" if suchtext else ""))
        return

    print(f"{len(modelle)} Modelle gefunden:")
    for name in modelle:
        print(f"  {name}")
    print(
        "\nMit 'ki modell <Name>' auswählen. Nicht jedes Modell ist zwingend für "
        "Chat/Text geeignet oder mit diesem Account nutzbar, siehe Modul-Docstring von ki.py."
    )


def _cmd_ki_reset(args: tuple[str, ...]) -> None:
    """Unterbefehl 'ki reset': setzt Modell und Zusatzfrage wieder auf den
    Ausgangszustand zurück (Standardmodell, keine Zusatzfrage), falls man
    beim Ausprobieren verschiedener Modelle/Fragen den Überblick verliert
    oder ein Modell sich als unbrauchbar herausgestellt hat."""
    setze_modell_standard()
    loesche_zusatzfrage()
    print(f"Zurückgesetzt. Modell: {aktuelles_modell()} (Standard), keine Zusatzfrage mehr gesetzt.")


def _cmd_ki_test(args: tuple[str, ...]) -> None:
    """Unterbefehl 'ki test [Modell]' | 'ki test alle [Suchtext]': schickt
    eine winzige Testanfrage (siehe ki.teste_modell()), um schnell zu prüfen,
    ob ein Modell mit dem eigenen Account und der aktuellen NVIDIA-Verbindung
    überhaupt antwortet, ohne dafür einen echten Treiber zu brauchen oder
    nennenswert Tokens zu verbrauchen. Praktisch, um bei einem Fehler wie
    'NVIDIA-API-Fehler 504' bei JEDEM Modell schnell zu sehen, ob das ein
    Problem der ganzen Verbindung ist (siehe Modul-Docstring von ki.py,
    Siebter Fund) oder nur ein einzelnes Modell betrifft.

    Mit 'alle' wird statt eines einzelnen Modells der komplette Katalog
    (optional gefiltert per Suchtext) durchgetestet, siehe ki.teste_katalog().
    Das kann bei über 80 Modellen mehrere Minuten dauern, der Fortschritt
    wird deshalb direkt beim Testen mit ausgegeben.
    """
    if args and args[0].lower() == "alle":
        suchtext = args[1] if len(args) > 1 else None
        print(
            "Teste alle Modelle im Katalog"
            + (f" (Suchtext: {suchtext})" if suchtext else "")
            + ", kann je nach Anzahl mehrere Minuten dauern...\n"
        )
        ergebnisse = teste_katalog(suchtext)
        erfolgreiche = [e for e in ergebnisse if e["erfolgreich"]]
        print(f"\n{len(erfolgreiche)} von {len(ergebnisse)} Modellen ansprechbar.")
        if erfolgreiche:
            print("Ansprechbar (Name, Antwortzeit):")
            for e in erfolgreiche:
                print(f"  {e['modell']} ({e['dauer_sekunden']}s)")
        elif ergebnisse:
            print(
                "Kein einziges Modell hat geantwortet. Das spricht für ein "
                "Problem der ganzen Verbindung/des Accounts (API-Key, Netzwerk, "
                "oder eine vorübergehende Störung bei build.nvidia.com), nicht "
                "für einzelne kaputte Modelle."
            )
        return

    modell = args[0] if args else aktuelles_modell()
    print(f"Teste Modell '{modell}' (Timeout 30s)...")
    ergebnis = teste_modell(args[0] if args else None)
    if ergebnis["erfolgreich"]:
        print(f"OK, Antwort nach {ergebnis['dauer_sekunden']}s: {ergebnis['antwort']!r}")
    else:
        print(f"Fehler nach {ergebnis['dauer_sekunden']}s: {ergebnis['fehler']}")


def cmd_ki(*args: str) -> None:
    """Befehl 'ki': bündelt sechs Unterbefehle rund um die KI-Zweitmeinung zu
    Treibern (build.nvidia.com, siehe ki/ki.py):
      ki treiber <Nummer> [Frage]   Fragt die KI zu einem einzelnen Treiber,
                                     optional mit einer einmaligen Zusatzfrage.
      ki frage [Text|loeschen]      Setzt/zeigt/entfernt eine dauerhafte Zusatzfrage.
      ki modell [Name]              Zeigt/setzt das benutzte Modell.
      ki modelle [Suchtext]         Listet verfügbare Modelle vom Katalog.
      ki test [Modell]              Winzige Testanfrage, prüft ob ein Modell ansprechbar ist.
      ki test alle [Suchtext]       Testet den ganzen Katalog (oder einen Ausschnitt) durch.
      ki reset                      Setzt Modell und Zusatzfrage zurück auf den Standard.
    """
    if not args:
        print(
            "Nutzung: 'ki treiber <Nummer> [Frage]', 'ki frage [Text|loeschen]', "
            "'ki modell [Name]', 'ki modelle [Suchtext]', 'ki test [Modell]', "
            "'ki test alle [Suchtext]' oder 'ki reset'."
        )
        return

    unterbefehl = args[0].lower()
    rest = args[1:]

    if unterbefehl == "treiber":
        _cmd_ki_treiber(rest)
    elif unterbefehl == "frage":
        _cmd_ki_frage(rest)
    elif unterbefehl == "modell":
        _cmd_ki_modell(rest)
    elif unterbefehl == "modelle":
        _cmd_ki_modelle(rest)
    elif unterbefehl == "test":
        _cmd_ki_test(rest)
    elif unterbefehl == "reset":
        _cmd_ki_reset(rest)
    else:
        print(f"Unbekannter ki-Unterbefehl: '{unterbefehl}'.")
        print(
            "Nutzung: 'ki treiber <Nummer>', 'ki frage', 'ki modell', 'ki modelle', "
            "'ki test' oder 'ki reset'."
        )


def _lesbare_groesse(anzahl_bytes: int) -> str:
    """Wandelt eine Byte-Zahl in eine gut lesbare Einheit um (KB/MB/GB/TB),
    je nachdem, wie groß der Wert ist. Anders als _gb() in anzeige.py (zeigt
    Laufwerksgrößen immer fest in GB an), weil Temp-Ordner oft nur ein paar
    MB oder sogar nur ein paar KB groß sind und "0.0 GB" da nichtssagend wäre.
    """
    wert = float(anzahl_bytes)
    for einheit in ("Bytes", "KB", "MB", "GB"):
        if wert < 1024 or einheit == "GB":
            return f"{wert:.0f} {einheit}" if einheit == "Bytes" else f"{wert:.1f} {einheit}"
        wert /= 1024
    return f"{wert:.1f} TB"


def cmd_cache() -> None:
    """Zeigt Größe und Dateianzahl der Windows-Temp-/Cache-Ordner (Benutzer-Temp
    und Windows-Temp), ohne etwas zu löschen. Löschen geht über den eigenen
    Befehl 'cacheleeren', damit man sich vorher erst einen Überblick verschaffen
    kann (siehe cleanup/cleanup.py)."""
    print("Prüfe Temp-/Cache-Ordner (kann bei vielen Dateien etwas dauern)...")
    berichte = pruefe_cache()

    gesamt_bytes = 0
    for bericht in berichte:
        if not bericht["existiert"]:
            print(f"  {bericht['name']}: Ordner nicht gefunden ({bericht['pfad']}).")
            continue
        if not bericht["zugreifbar"]:
            print(f"  {bericht['name']}: kein Zugriff (evtl. als Administrator starten).")
            print(f"    ({bericht['pfad']})")
            continue
        gesamt_bytes += bericht["groesse_bytes"]
        hinweis = f", {bericht['anzahl_fehler']} nicht lesbar" if bericht["anzahl_fehler"] else ""
        print(
            f"  {bericht['name']}: {_lesbare_groesse(bericht['groesse_bytes'])}, "
            f"{bericht['anzahl_dateien']} Dateien{hinweis}"
        )
        print(f"    ({bericht['pfad']})")

    print(f"\nInsgesamt: {_lesbare_groesse(gesamt_bytes)}.")
    print("Mit 'cacheleeren' lässt sich der Inhalt dieser Ordner löschen.")


def cmd_cache_leeren() -> None:
    """Löscht den Inhalt der Windows-Temp-/Cache-Ordner (siehe Befehl 'cache'
    für eine Vorschau, wie viel das ungefähr ist). Fragt vorher extra nach,
    weil das Löschen nicht rückgängig zu machen ist."""
    antwort = input(
        "Wirklich den Inhalt von Benutzer-Temp und Windows-Temp löschen? "
        "Das lässt sich nicht rückgängig machen. (j/n): "
    ).strip().lower()
    if antwort not in ("j", "ja", "y", "yes"):
        print("Abgebrochen, es wurde nichts gelöscht.")
        return

    print("Leere Temp-/Cache-Ordner...")
    ergebnisse = leere_cache()

    gesamt_bytes = 0
    for ergebnis in ergebnisse:
        if not ergebnis["existiert"]:
            print(f"  {ergebnis['name']}: Ordner nicht gefunden ({ergebnis['pfad']}).")
            continue
        if not ergebnis["zugreifbar"]:
            print(f"  {ergebnis['name']}: kein Zugriff (evtl. als Administrator starten).")
            continue
        gesamt_bytes += ergebnis["geloescht_bytes"]
        hinweis = (
            f", {ergebnis['anzahl_fehler']} übersprungen (in Benutzung oder keine Berechtigung)"
            if ergebnis["anzahl_fehler"] else ""
        )
        print(
            f"  {ergebnis['name']}: {ergebnis['geloescht_eintraege']} Einträge gelöscht "
            f"({_lesbare_groesse(ergebnis['geloescht_bytes'])}){hinweis}"
        )

    print(f"\nInsgesamt {_lesbare_groesse(gesamt_bytes)} freigegeben.")


_FRAGMENTIERUNG_MARKER = {"kritisch": "!!", "warnung": " !", "ok": "  ", "unbekannt": " ?"}


def cmd_defrag() -> None:
    """Zeigt den Fragmentierungsstatus aller lokalen Festplatten (ruft
    'defrag <Laufwerk>: /A' auf, das analysiert nur und verändert nichts) und
    speichert die komplette Rohausgabe je Laufwerk plus einen Gesamtbericht
    unter data/output/defrag. Braucht wie 'pnputil /enum-drivers'
    Administratorrechte, siehe defrag/defrag.py."""
    print("Analysiere Fragmentierung aller lokalen Laufwerke (kann je nach Laufwerksgröße etwas dauern)...")
    bericht = pruefe_fragmentierung()

    if not bericht["admin"]:
        print("Hinweis: nicht als Administrator gestartet, 'defrag /A' schlägt deshalb wahrscheinlich fehl.\n")

    if not bericht["laufwerke"]:
        print("Keine lokalen Festplatten gefunden.")
        return

    print(f"{'':3}{'Laufwerk':10}{'Typ':6}{'Gesamt':>8}{'Frag. Dateien':>15}{'Ø Frag/Datei':>14}  Status")
    print("-" * 66)
    for lw in bericht["laufwerke"]:
        marker = _FRAGMENTIERUNG_MARKER.get(lw["status"], " ?")
        typ = "SSD" if lw["ist_ssd"] else ("HDD" if lw["gesamtfragmentierung_prozent"] is not None else "?")
        gesamt = f"{lw['gesamtfragmentierung_prozent']}%" if lw["gesamtfragmentierung_prozent"] is not None else "?"
        frag_dateien = (
            str(lw["fragmentierte_dateien_anzahl"]) if lw["fragmentierte_dateien_anzahl"] is not None else "?"
        )
        frag_pro_datei = (
            str(lw["durchschnittliche_fragmente_pro_datei"])
            if lw["durchschnittliche_fragmente_pro_datei"] is not None else "?"
        )
        print(f"{marker:<3}{lw['laufwerk']:10}{typ:6}{gesamt:>8}{frag_dateien:>15}{frag_pro_datei:>14}  {lw['status']}")
        if not lw["erfolgreich"]:
            print("     Hinweis: Analyse fehlgeschlagen (evtl. Administratorrechte nötig).")
        elif lw["empfehlung_laut_defrag"]:
            print(f"     defrag meint: {lw['empfehlung_laut_defrag']}")


def cmd_hilfe() -> None:
    print("Verfügbare Befehle:")
    for name, (beschreibung, _) in COMMANDS.items():
        print(f"  {name:<10} {beschreibung}")


def cmd_ende() -> None:
    print("Beende NFOAnalyst.")


# Jeder Befehl wird hier als (Beschreibung, Funktion) hinterlegt. Neue Befehle
# (z.B. später für parse/ki/model) kann man einfach als weiteren Eintrag
# ergänzen, main() muss dafür nicht angepasst werden.
COMMANDS: dict[str, tuple[str, callable]] = {
    "collect": ("Sammelt Windows-Systeminformationen und speichert sie in data/input.", cmd_collect),
    "parse": ("Parst die gesammelten Rohdaten zu JSON in data/output.", cmd_parse),
    "analyze": ("Prüft Treiberalter/-signatur und Laufwerksplatz, speichert Berichte in data/output/report.", cmd_analyze),
    "green": ("Schätzt den Energie-/CO2-Fußabdruck von Autostart und Energieschema.", cmd_green),
    "systemuebersicht": ("Zeigt die msinfo32-Systemübersicht (aus 'parse').", cmd_systemuebersicht),
    "laufwerke": ("Zeigt Laufwerke mit Füllstand und !!/!-Markern (aus 'analyze').", cmd_laufwerke),
    "treiber": ("Zeigt Alter/Version der ältesten Treiber und unsignierte Geräte (aus 'analyze').", cmd_treiber),
    "autostart": ("Zeigt alle Autostart-Programme (aus 'parse').", cmd_autostart),
    "problemgeraete": ("Zeigt alle laut msinfo32 gemeldeten Problemgeräte (aus 'parse').", cmd_problemgeraete),
    "greenreport": ("Zeigt den Green-/CO2-Bericht mit Empfehlungen (aus 'green').", cmd_greenreport),
    "ki": (
        "Nutzung: 'ki treiber <Nummer> [Frage]' | 'ki frage [Text|loeschen]' | "
        "'ki modell [Name]' | 'ki modelle [Suchtext]' | 'ki test [Modell]' | "
        "'ki test alle [Suchtext]' | 'ki reset'.",
        cmd_ki,
    ),
    "cache": ("Zeigt Größe/Dateianzahl der Windows-Temp-/Cache-Ordner.", cmd_cache),
    "cacheleeren": ("Löscht den Inhalt der Windows-Temp-/Cache-Ordner (nach Rückfrage).", cmd_cache_leeren),
    "defrag": ("Zeigt den Fragmentierungsstatus aller lokalen Festplatten (nur Analyse).", cmd_defrag),
    "hilfe": ("Zeigt diese Liste der Befehle an.", cmd_hilfe),
    "ende": ("Beendet das Programm.", cmd_ende),
}


def main() -> int:
    print("NFOAnalyst, interaktive Kommandozeile.")
    print("Gib 'hilfe' für die Befehlsliste ein oder 'ende' zum Beenden.\n")

    while True:
        try:
            eingabe = input("nfoanalyst> ")
        except (EOFError, KeyboardInterrupt):
            # EOFError tritt zum Beispiel auf, wenn Befehle per printf/echo
            # eingespeist werden und der Input irgendwann endet (ein
            # abschließendes 'ende' ist dann nicht nötig). KeyboardInterrupt
            # entspricht Strg+C. In beiden Fällen soll das Programm sauber
            # beenden, statt mit einem Traceback abzubrechen.
            print()
            break

        # Das erste Wort ist der Befehl (case-insensitiv, damit "Collect",
        # "collect" und "COLLECT" alle dasselbe auslösen), alles danach sind
        # Argumente für den Befehl, z.B. "ki treiber 3" -> befehl="ki",
        # args=("treiber", "3"). Befehle ohne Argumente ignorieren args einfach.
        teile = eingabe.split()
        if not teile:
            continue
        befehl = teile[0].lower()
        args = teile[1:]

        eintrag = COMMANDS.get(befehl)
        if eintrag is None:
            print(f"Unbekannter Befehl: '{eingabe}'. Gib 'hilfe' ein für eine Liste der Befehle.")
            continue

        _, funktion = eintrag
        # funktion(*args) statt funktion(): bei den meisten Befehlen ist args
        # eine leere Liste, dann ist das dasselbe wie funktion() ohne Argumente.
        # Nur cmd_ki() nimmt tatsächlich Argumente entgegen (siehe def cmd_ki(*args)).
        funktion(*args)

        if befehl == "ende":
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
