"""Sammelt Windows-Systeminformationen (driverquery, pnputil, msinfo32, powercfg)
und speichert die Rohdaten unter nfoanalyst/data/input/ (bzw. im übergebenen output_dir).

Nutzung als Bibliothek (einzeiliger Aufruf aus einem anderen Programm):

    from nfoanalyst.collect.collect import collect
    collect()

collect() führt alle vier Sammler nacheinander aus und gibt den Pfad zum
Zielverzeichnis zurück. Optionale Parameter:

    collect(output_dir=..., include_energy=True, msinfo_timeout=180)

Achtung: Dateien mit gleichem Namen aus einem früheren Lauf werden dabei
überschrieben. Wer getrennte Datensätze pro Lauf oder Rechner haben will,
sollte einen eigenen output_dir übergeben.

Das Skript ist außerdem direkt per `python collect.py [--optionen]` als
CLI-Tool nutzbar, siehe main()/argparse ganz unten.

Fehler und Lösungen beim Entwickeln:
Zuerst wurde die Konsolenausgabe der vier Windows-Tools einfach mit einer
einzigen festen Kodierung gelesen (erst "oem", dann "utf-8"). Beim Testen kam
dabei aber immer wieder fehlerhafter Text heraus, mal bei driverquery, mal bei
pnputil, mal bei powercfg, und nicht mal immer beim selben Tool. Es stellte
sich heraus, dass die Tools ihre Ausgabe unterschiedlich kodieren (UTF-8, ANSI-Codepage
cp1252 oder OEM-Codepage cp850), je nachdem wie sie gerade aufgerufen wurden,
und das offenbar nicht mal zuverlässig gleich bei jedem Lauf. Die Lösung war,
erst UTF-8 zu versuchen und wenn das fehlschlägt beide übrigen Kodierungen
durchzuprobieren und die Variante zu nehmen, die am wenigsten unplausible
Zeichen enthält (siehe decode_console_output() und _SUSPICIOUS_RANGES unten).
"""

from __future__ import annotations

# argparse:   nur für die CLI-Nutzung (python collect.py --...) gebraucht, nicht beim Import als Bibliothek.
# ctypes:     Zugriff auf die Windows-API (shell32.IsUserAnAdmin), um vorher zu checken, ob wir mit
#             Adminrechten laufen. Ein paar Befehle (pnputil /enum-drivers, powercfg /energy) brauchen das.
# platform:   erkennt das Betriebssystem, damit das Skript auf Nicht-Windows-Systemen sauber abbricht.
# subprocess: startet die eigentlichen Windows-Kommandozeilentools (driverquery.exe, pnputil.exe,
#             msinfo32.exe, powercfg.exe) und liest deren Ausgabe ein. Das ist das eigentliche Sammeln.
# pathlib:    plattformunabhängiges, objektorientiertes Pfad-Handling statt String-Verkettung.
import argparse
import ctypes
import platform
import subprocess
import sys
from pathlib import Path

# Pfade werden relativ zu dieser Datei berechnet (nicht zum aktuellen Arbeitsverzeichnis des
# aufrufenden Prozesses), damit collect() auch dann ins richtige Verzeichnis schreibt, wenn es
# aus einem anderen Projekt/Skript mit anderem Arbeitsverzeichnis importiert wird.
COLLECT_DIR = Path(__file__).resolve().parent
NFOANALYST_DIR = COLLECT_DIR.parent
DEFAULT_OUTPUT_DIR = NFOANALYST_DIR / "data" / "input"


def is_admin() -> bool:
    """Prüft per Windows-API, ob der aktuelle Prozess mit Adminrechten läuft."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# Zeichen, die in echten Geräte-/Treibernamen so gut wie nie vorkommen. Wenn
# nach einem Fallback-Decode viele davon auftauchen, war die Kodierung falsch.
#   0x80-0x9F:     C1-Steuerzeichen, entstehen wenn eigentlich cp850-Bytes als
#                  cp1252 gelesen werden.
#   0x2000-0x206F: Anführungszeichen/Gedankenstriche, dasselbe Problem, nur
#                  für die Bytes, die cp1252 tatsächlich kennt.
#   0x2500-0x259F: Rahmen- und Blockzeichen, entstehen andersrum, wenn
#                  eigentlich cp1252-Bytes als cp850 gelesen werden.
_SUSPICIOUS_RANGES = ((0x80, 0x9F), (0x2000, 0x206F), (0x2500, 0x259F))


def _count_suspicious_chars(text: str) -> int:
    return sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _SUSPICIOUS_RANGES))


def decode_console_output(data: bytes) -> str:
    """Dekodiert die Rohbytes eines Konsolenbefehls.

    driverquery, pnputil und powercfg sind nicht einheitlich kodiert, und
    zwar nicht mal zuverlässig pro Tool. Mal schreiben sie UTF-8, mal die
    ANSI-Codepage (unter Windows meistens cp1252), mal die OEM-Codepage (laut
    `chcp` auf diesem Rechner 850), je nachdem wie sie gerade aufgerufen
    wurden (siehe Fehler-und-Lösungen-Abschnitt im Modul-Docstring).

    Deshalb wird hier zuerst UTF-8 strikt versucht. Klappt das nicht, werden
    cp1252 und cp850 beide durchprobiert, und es wird die Variante genommen,
    die am wenigsten "verdächtige" Zeichen enthält (siehe _SUSPICIOUS_RANGES).
    Das sind Zeichen, die nur auftauchen, wenn die falsche Kodierung
    verwendet wurde. cp1252 kennt ein paar Bytes gar nicht, zum Beispiel 0x81
    für ein cp850-'ü', und scheidet dann direkt aus.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    kandidaten = []
    for encoding in ("cp1252", "cp850"):
        try:
            kandidaten.append(data.decode(encoding))
        except UnicodeDecodeError:
            continue

    if not kandidaten:
        return data.decode("cp850", errors="replace")
    # min() mit key=_count_suspicious_chars: nimmt von den beiden Kandidaten
    # (cp1252-Decode und cp850-Decode) einfach den mit den wenigsten
    # verdächtigen Zeichen, ohne dass man selbst if/else schreiben muss.
    return min(kandidaten, key=_count_suspicious_chars)


def run_command(args: list[str], timeout: int = 120) -> tuple[bool, str]:
    """Führt einen Windows-Befehl aus und gibt (erfolgreich, dekodierte Ausgabe) zurück.

    Die Ausgabe wird bewusst als Rohbytes eingesammelt (subprocess.run ohne
    text=True/encoding=...) und erst danach über decode_console_output()
    dekodiert. Mit einer festen Kodierung würde subprocess sonst ein
    einziges Encoding für alle vier aufgerufenen Tools erzwingen, siehe
    Kommentar dort oben.
    """
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        # z.B. wenn driverquery.exe/pnputil.exe auf diesem Windows aus
        # irgendeinem Grund nicht im PATH steht (sehr selten, aber schöner
        # als ein rohes Traceback).
        return False, f"Befehl nicht gefunden: {args[0]}"
    except subprocess.TimeoutExpired:
        return False, f"Zeitüberschreitung nach {timeout}s: {' '.join(args)}"

    output = decode_console_output(result.stdout or b"")
    if result.returncode != 0:
        output += f"\n\n[stderr]\n{decode_console_output(result.stderr or b'')}"
    return result.returncode == 0, output


def save(run_dir: Path, filename: str, content: str) -> Path:
    """Schreibt den gesammelten Text als UTF-8-Datei in den Ergebnisordner."""
    path = run_dir / filename
    path.write_text(content, encoding="utf-8")
    print(f"  gespeichert: {path.name}")
    return path


def collect_driverquery(run_dir: Path) -> None:
    print("[driverquery] sammle Treiberliste...")
    _, out = run_command(["driverquery", "/v", "/fo", "csv"])
    save(run_dir, "driverquery_verbose.csv", out)

    _, out = run_command(["driverquery", "/fo", "list", "/v"])
    save(run_dir, "driverquery_verbose.txt", out)

    # /si listet zusätzlich, ob ein Treiber signiert ist (Signaturprüfung).
    _, out = run_command(["driverquery", "/si", "/fo", "csv"])
    save(run_dir, "driverquery_signed.csv", out)


def collect_pnputil(run_dir: Path) -> None:
    print("[pnputil] sammle Geräte- und Treiberinformationen...")
    _, out = run_command(["pnputil", "/enum-devices", "/connected"])
    save(run_dir, "pnputil_devices.txt", out)

    # /enum-drivers listet den kompletten Treiber-Store (auch nicht aktive
    # Treiber) und braucht dafür Adminrechte. Ohne die schlägt der Befehl
    # zwar fehl, aber nicht gleich das ganze Skript, weil run_command den
    # Fehler abfängt.
    ok, out = run_command(["pnputil", "/enum-drivers"])
    save(run_dir, "pnputil_drivers.txt", out)
    if not ok:
        print("  Hinweis: 'pnputil /enum-drivers' braucht evtl. Administratorrechte.")


def collect_msinfo32(run_dir: Path, timeout: int) -> None:
    print("[msinfo32] erstelle Systembericht (.nfo), das kann bis zu einer Minute dauern...")
    # msinfo32 ist eigentlich eine GUI-Anwendung, blockiert aber mit /nfo den
    # aufrufenden Prozess, bis der Bericht fertig geschrieben ist. Man muss
    # also nicht selbst pollen, ob die Datei schon fertig ist. Dauert je
    # nach Rechner ca. 20 bis 60 Sekunden.
    nfo_path = run_dir / "msinfo32_report.nfo"
    _, out = run_command(["msinfo32", "/nfo", str(nfo_path)], timeout=timeout)
    if nfo_path.exists():
        print(f"  gespeichert: {nfo_path.name}")
    else:
        print(f"  Fehler: msinfo32 hat keine Datei erzeugt.\n{out}")


def collect_powercfg(run_dir: Path, include_energy: bool) -> None:
    print("[powercfg] sammle Energieeinstellungen...")
    _, out = run_command(["powercfg", "/list"])
    save(run_dir, "powercfg_list.txt", out)

    _, out = run_command(["powercfg", "/query"])
    save(run_dir, "powercfg_query.txt", out)

    _, out = run_command(["powercfg", "/availablesleepstates"])
    save(run_dir, "powercfg_sleepstates.txt", out)

    # Auf Desktop-PCs ohne Akku liefert /batteryreport keine sinnvolle Datei.
    # Das ist kein Fehler, sondern einfach erwartetes Verhalten.
    battery_path = run_dir / "powercfg_battery_report.html"
    run_command(["powercfg", "/batteryreport", "/output", str(battery_path)])
    if battery_path.exists():
        print(f"  gespeichert: {battery_path.name}")
    else:
        print("  Hinweis: kein Akku erkannt oder Batteriebericht nicht verfügbar.")

    if include_energy:
        # /energy läuft ca. 60 Sekunden lang und misst dabei aktiv (z.B.
        # CPU-Last, USB-Suspend-Verhalten). Braucht Adminrechte, deswegen
        # standardmäßig ausgeschaltet.
        print("  erstelle Energieeffizienz-Diagnose (~60s, braucht Adminrechte)...")
        energy_path = run_dir / "powercfg_energy_report.html"
        _, out = run_command(
            ["powercfg", "/energy", "/output", str(energy_path)], timeout=120
        )
        if energy_path.exists():
            print(f"  gespeichert: {energy_path.name}")
        else:
            print(f"  Fehler beim Energiebericht:\n{out}")


def collect(
    output_dir: Path | str | None = None,
    include_energy: bool = False,
    msinfo_timeout: int = 180,
) -> Path:
    """Führt alle vier Sammler aus und gibt das Zielverzeichnis zurück.

    Das ist der Haupteinstiegspunkt für die Nutzung als Bibliothek:

        from nfoanalyst.collect.collect import collect
        run_dir = collect()

    Parameter:
        output_dir:      Zielverzeichnis für die gesammelten Dateien (Standard:
                          nfoanalyst/data/input, unabhängig vom aufrufenden Skript).
                          Wird bei Bedarf angelegt. Vorhandene Dateien mit
                          gleichem Namen werden überschrieben.
        include_energy:  zusätzlich 'powercfg /energy' ausführen (~60s, braucht Adminrechte).
        msinfo_timeout:  Timeout in Sekunden für die msinfo32-Berichterstellung.

    Wirft RuntimeError, wenn nicht unter Windows ausgeführt.
    """
    if platform.system() != "Windows":
        raise RuntimeError("collect() funktioniert nur unter Windows.")

    if not is_admin():
        print(
            "Hinweis: nicht als Administrator gestartet. Einzelne Befehle "
            "(pnputil /enum-drivers, powercfg /energy) können deswegen fehlschlagen.\n"
        )

    # base_dir und run_dir sind hier bewusst zwei Namen für denselben Pfad:
    # früher gab es noch einen Zeitstempel-Unterordner pro Lauf (run_dir war
    # base_dir + Hostname + Zeitstempel), das wurde später wieder entfernt,
    # weil "Achtung, überschreibt" (siehe Modul-Docstring) einfacher zu verstehen
    # war als jedes Mal einen neuen Ordner zu suchen.
    base_dir = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT_DIR
    run_dir = base_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Sammle Systemdaten nach: {run_dir}\n")

    collect_driverquery(run_dir)
    collect_pnputil(run_dir)
    collect_msinfo32(run_dir, msinfo_timeout)
    collect_powercfg(run_dir, include_energy)

    print("\nFertig.")
    return run_dir


def main() -> int:
    """Dünner CLI-Wrapper um collect() für den direkten Aufruf `python collect.py`."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--include-energy",
        action="store_true",
        help="zusätzlich 'powercfg /energy' ausführen (~60s, braucht Adminrechte)",
    )
    parser.add_argument("--msinfo-timeout", type=int, default=180)
    args = parser.parse_args()

    try:
        collect(
            output_dir=args.output_dir,
            include_energy=args.include_energy,
            msinfo_timeout=args.msinfo_timeout,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
