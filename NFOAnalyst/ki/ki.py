"""Fragt eine KI (ein Modell von build.nvidia.com) zu einem einzelnen Treiber
aus dem letzten 'analyze'-Bericht: ist er wirklich veraltet, gibt es
vermutlich eine neuere Version, ist die eigene "kritisch"-Einstufung aus
analyze.py in diesem Fall überhaupt gerechtfertigt, und was wäre eine
konkrete Handlungsempfehlung? Ein Treiber wird über die Nummer ausgewählt,
die der Befehl 'treiber' (siehe anzeige.py) vor jeden Eintrag schreibt.

Nutzung als Bibliothek (einzeiliger Aufruf aus einem anderen Programm):

    from nfoanalyst.ki.ki import frage_ki_zu_treiber
    ergebnis = frage_ki_zu_treiber(1)

Für den API-Key und das Modell wird eine .env-Datei in nfoanalyst/ erwartet
(siehe .env.example daneben):

    NVIDIA_API_KEY=nvapi-...
    NVIDIA_MODEL=mistralai/mistral-nemotron

NVIDIA_MODEL ist optional, ohne sie wird DEFAULT_MODEL benutzt. Über den
REPL-Befehl 'ki modell <Name>' lässt sich NVIDIA_MODEL auch bequem von innen
heraus setzen (schreibt in dieselbe .env-Zeile), 'ki modelle [Suchtext]'
fragt den aktuellen Modell-Katalog live bei build.nvidia.com ab (siehe
liste_modelle()).

Außerdem lässt sich mit 'ki frage <Text>' eine persönliche Zusatzfrage
setzen (gespeichert in ZUSATZFRAGE_PATH, siehe lade_zusatzfrage() unten),
die ab dann bei jeder 'ki treiber <Nummer>'-Anfrage zusätzlich zum
Standard-Prompt mitgestellt wird, zum Beispiel "Ist dieser Treiber ein
Sicherheitsrisiko?" oder "Wie dringend ist ein Update wirklich?". 'ki frage
loeschen' entfernt sie wieder. Für eine einmalige Frage zu nur einem
bestimmten Treiber reicht auch 'ki treiber <Nummer> <Frage>' direkt (siehe
frage_ki_zu_treiber()s Parameter zusatzfrage): das überschreibt die
gespeicherte Zusatzfrage nur für diesen einen Aufruf, ohne sie zu verändern.

'ki reset' setzt Modell und Zusatzfrage wieder auf den Ausgangszustand
zurück (Standardmodell DEFAULT_MODEL, keine Zusatzfrage), falls man beim
Ausprobieren verschiedener Modelle/Fragen den Überblick verliert.

Fehler und Lösungen beim Entwickeln:
Mit dem ursprünglichen Standardmodell (meta/llama-3.3-70b-instruct) kam beim
Testen immer wieder ein 503 zurück ("Worker local total request limit reached").
Das lag nicht am eigenen API-Key (der wurde von der API akzeptiert, sonst gäbe
es einen 401/403), sondern schlicht daran, dass dieses beliebte Modell auf der
kostenlosen NVIDIA-Infrastruktur zeitweise ausgelastet ist. Ein kleineres Modell
(meta/llama-3.1-8b-instruct) lief sofort durch, deshalb ist das jetzt der
Standard. Bei Bedarf trotzdem auf ein größeres Modell wechseln (siehe
NVIDIA_MODEL oben), einfach mit der Erwartung, dass es ab und zu einen 503
gibt und man es dann nochmal versuchen muss.

Zweiter Fund: Der API-Key wurde aus Versehen in .env.example statt in .env
eingetragen (die Beispieldatei sieht der echten .env zum Verwechseln ähnlich).
_lade_env() liest aber ausschließlich ENV_PATH (also nfoanalyst/.env), .env.example
wird nie angefasst. Deshalb kam trotz "eingetragenem" Key die Fehlermeldung
"Kein NVIDIA_API_KEY gefunden". Merke: .env.example ist nur eine Vorlage zum
Kopieren, nie direkt zu befüllen.

Dritter Fund: meta/llama-3.1-8b-instruct (der bisherige Standard) wurde von
NVIDIA komplett abgeschaltet ("has reached its end of life", HTTP 410 Gone),
nicht nur überlastet wie beim ersten Fund. Bei der Suche nach einem Ersatz
über https://integrate.api.nvidia.com/v1/models fiel außerdem auf, dass
längst nicht jedes dort gelistete Modell mit diesem Account auch wirklich
aufrufbar ist. Mehrere (u.a. google/gemma-3-4b-it, ibm/granite-3.0-8b-instruct)
gaben einen 404 "Not found for account" zurück, obwohl sie in der Modell-Liste
auftauchen. Ein Modell testweise aufzurufen ist also der einzig sichere Weg,
es tatsächlich als nutzbar zu bestätigen, die Modell-Liste allein reicht nicht.
Ein weiterer Kandidat, openai/gpt-oss-20b, war zwar aufrufbar, ist aber ein
"Reasoning"-Modell: es verbrennt sein komplettes max_tokens-Budget für eine
lange, unsichtbare Gedankenkette (Feld "reasoning"/"reasoning_content" in der
Antwort) und "content" blieb dadurch bei diesem Prompt leer (None), was
_extrahiere_json() zum Absturz brachte, weil dort text.find() auf None
aufgerufen wurde. Neuer Standard ist jetzt mistralai/mistral-nemotron, das
im Test zuverlässig ein sauberes JSON in normaler Länge lieferte.

Vierter Fund: Beim Ausprobieren von moonshotai/kimi-k3 (über 'ki modell'
gesetzt) kam erst ein "NVIDIA-API-Fehler 500" zurück (vermutlich ein
transientes Problem auf NVIDIA-Seite, ähnlich dem 503 oben, einfach nochmal
versuchen oder ein anderes Modell nehmen), beim nächsten Versuch dann aber
ein kompletter Absturz der REPL mit einer nicht abgefangenen TimeoutError.
Die Verbindung stand also, aber response.read() brach beim Warten auf die
Antwort ab. TimeoutError ist keine urllib.error.URLError/HTTPError, landet
also in keinem der beiden bisherigen except-Zweige. Jetzt gibt es dafür
einen eigenen except-Zweig in _rufe_nvidia_api(), der daraus eine normale
RuntimeError macht (wie bei den anderen Fehlern auch), statt dass die ganze
REPL mit einem Traceback beendet wird.

Fünfter Fund, gleich zwei auf einmal: Erstens wurde per 'ki modell chat'
(Tippfehler, kein echter Modellname) klaglos NVIDIA_MODEL=chat in die .env
geschrieben, setze_modell() prüft den Namen nämlich gar nicht. Das ist erst
viel später bei einem völlig anderen Befehl mit einem verwirrenden "404 page
not found" aufgefallen, ohne erkennbaren Zusammenhang zum eigentlichen
Tippfehler. Jetzt prüft modell_existiert() nach dem Setzen sofort gegen den
echten Katalog und warnt direkt, falls der Name dort nicht auftaucht.

Zweitens (und das war der eigentliche Grund für die Verwirrung an diesem
Tag): mistralai/mistral-nemotron, bis dahin zuverlässig, fing plötzlich
mitten in der Sitzung an zu einem 500 "Inference connection error" oder
einer Zeitüberschreitung zu führen, komplett ohne Codeänderung dazwischen.
Direkt nachgestellt (mehrere Aufrufe desselben Modells kurz hintereinander,
dazu ein zweites, unabhängig zuvor funktionierendes Modell): beide
scheiterten gleichermaßen, mal sofort mit 500, mal nach 30 Sekunden mit
Timeout. Das bestätigt, dass es sich um eine vorübergehende Störung auf
NVIDIA-Seite handelte (freie/kostenlose build.nvidia.com-Infrastruktur),
nicht um einen Fehler in diesem Programm, im eigenen Netzwerk oder am
Account. Sowas lässt sich von hier aus nicht beheben, nur abwarten und
später nochmal versuchen, notfalls ein anderes Modell testen.

Sechster Fund: Der Timeout für den eigentlichen Chat-Aufruf stand fest auf
60 Sekunden (_rufe_nvidia_api()s timeout-Parameter). In der Praxis kam es
gerade bei etwas größeren/langsameren Modellen regelmäßig vor, dass die
Antwort erst kurz NACH diesen 60 Sekunden fertig war, die Anfrage also genau
an der Ziellinie mit einer Zeitüberschreitung abgebrochen wurde, obwohl das
Modell an sich funktioniert hätte. Zwei Änderungen dagegen: Erstens wurde
der Standard-Timeout auf DEFAULT_TIMEOUT_SEKUNDEN hochgesetzt und ist über
NVIDIA_TIMEOUT in der .env zusätzlich einstellbar (gleiches Muster wie
NVIDIA_MODEL, siehe aktuelles_timeout()), damit man bei einem besonders
langsamen Modell notfalls noch weiter hochdrehen kann. Zweitens wurde der
Prompt in baue_payload() um eine explizite Kürze-Anweisung ergänzt (kurze,
knappe Sätze statt ausführlicher Begründungen): eine kürzere Antwort braucht
schlicht weniger Zeit, um generiert zu werden, was die Wahrscheinlichkeit
einer Zeitüberschreitung zusätzlich senkt und nebenbei auch die spätere
Anzeige übersichtlicher macht.

Siebter Fund: Nach der Timeout-Erhöhung kam statt der Zeitüberschreitung nun
bei WIRKLICH JEDEM ausprobierten Modell (auch ganz unterschiedlichen, u.a.
einem reinen Vision-Modell) sofort ein "NVIDIA-API-Fehler 504" mit leerem
Fehlertext zurück. Wichtig zu verstehen: ein HTTP 504 ("Gateway Timeout")
kommt von urllib als ganz normale HTTPError zurück, es ist also keine
Zeitüberschreitung auf UNSERER Seite (die würde als eigene TimeoutError oder
mit einer anderen Fehlermeldung auftauchen, siehe oben), sondern eine
Antwort, die der vorgeschaltete API-Gateway von build.nvidia.com selbst
zurückschickt, weil ER innerhalb SEINES eigenen, kürzeren internen Timeouts
keine Antwort vom eigentlichen Modell-Backend bekommen hat. Dass das
ausnahmslos jedes Modell betrifft, spricht stark dafür, dass es sich (wie
schon beim Fünfter-Fund-Vorfall) um ein vorübergehendes, account- oder
infrastrukturweites Problem auf NVIDIA-Seite handelt und nicht um ein
einzelnes kaputtes Modell oder einen Fehler in diesem Programm. Die
Modell-Liste selbst (liste_modelle(), GET /v1/models) ist davon unabhängig
und lieferte weiterhin ganz normal alle 81 Katalogeinträge zurück, ein 504
kam ausschließlich beim eigentlichen Chat-Aufruf (POST /v1/chat/completions).
Um genau solche Fälle künftig schneller einzukreisen (liegt es an einem
einzelnen Modell oder an der ganzen Verbindung?), gibt es jetzt teste_modell()
und teste_katalog() (Befehl 'ki test'): eine winzige Testanfrage mit
max_tokens=5, die sich in Sekunden statt Minuten beantworten lässt und ohne
echte Treiberdaten funktioniert.

Wichtig: Die KI kennt nicht jede einzelne INF-Datei auf der Welt und rät bei
sehr speziellen/obskuren Treibern zwangsläufig auch mal daneben. Das hier ist
eine Zweitmeinung zur Einordnung, kein Ersatz für einen Blick auf die
Hersteller-Webseite.
"""

from __future__ import annotations

# json:            zum Bauen des Anfrage-Bodys, Lesen der Antwort und Speichern des Ergebnisses.
# os:               nur für os.environ.get(), falls der API-Key als echte Umgebungsvariable
#                   gesetzt ist statt in der .env-Datei zu stehen.
# urllib.request:   schickt die eigentliche HTTP-POST-Anfrage an die NVIDIA-API. Absichtlich
#                   Standardbibliothek statt der 'requests'-Bibliothek, damit man dafür nichts
#                   zusätzlich installieren muss (requests ist zwar auf diesem Rechner vorhanden,
#                   aber nicht garantiert auf jedem anderen).
# urllib.error:     die Fehlerklassen (HTTPError, URLError), die urllib.request bei einem
#                   fehlgeschlagenen Request wirft.
# pathlib:          für die Pfade zu .env, treiber.json und den Ergebnis-Dateien.
# time:             misst nur die Dauer der Testanfragen in teste_modell()/teste_katalog(),
#                   damit man beim Testen sieht, ob und wie schnell ein Modell antwortet.
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

KI_DIR = Path(__file__).resolve().parent
NFOANALYST_DIR = KI_DIR.parent
ENV_PATH = NFOANALYST_DIR / ".env"
ZUSATZFRAGE_PATH = NFOANALYST_DIR / "ki_zusatzfrage.txt"
DEFAULT_INPUT_DIR = NFOANALYST_DIR / "data" / "output" / "report"
DEFAULT_OUTPUT_DIR = NFOANALYST_DIR / "data" / "output" / "report" / "ki"

# Irgendein solides Allzweck-Modell von build.nvidia.com als Startwert. Über
# NVIDIA_MODEL in der .env kann man das jederzeit gegen ein anderes Modell
# aus dem Katalog austauschen, ohne den Code anzufassen. Nicht jedes dort
# gelistete Modell ist mit dem eigenen Account auch aufrufbar, siehe Dritter
# Fund im Modul-Docstring, also vor dem Eintragen lieber kurz testen.
DEFAULT_MODEL = "mistralai/mistral-nemotron"

# Wie lange (in Sekunden) auf die Antwort der KI gewartet wird, bevor
# _rufe_nvidia_api() aufgibt. War früher fest auf 60 Sekunden, was bei
# etwas langsameren Modellen regelmäßig knapp VOR einer eigentlich noch
# erfolgreichen Antwort abgebrochen hat (siehe Sechster Fund im
# Modul-Docstring). Über NVIDIA_TIMEOUT in der .env zusätzlich einstellbar,
# gleiches Muster wie NVIDIA_MODEL (siehe aktuelles_timeout()).
DEFAULT_TIMEOUT_SEKUNDEN = 360

# Eigener, viel kürzerer Timeout für teste_modell()/teste_katalog() (Befehl
# 'ki test'): dort geht es nur um eine winzige Erreichbarkeits-Anfrage
# ("Antworte nur mit OK", max_tokens=5), die im Erfolgsfall in wenigen
# Sekunden durch sein sollte. DEFAULT_TIMEOUT_SEKUNDEN wäre für einen
# schnellen Check viel zu lang, vor allem bei teste_katalog() über den
# ganzen Modell-Katalog hinweg (siehe Siebter Fund im Modul-Docstring).
TEST_TIMEOUT_SEKUNDEN = 30

API_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
# Gleiche Basis-URL wie API_URL, aber der Katalog-Endpunkt statt Chat
# (OpenAI-kompatible APIs bieten beide normalerweise nebeneinander an).
MODELS_URL = "https://integrate.api.nvidia.com/v1/models"


# --------------------------------------------------------------------------
# .env laden (eigener kleiner Loader statt einer zusätzlichen Bibliothek wie
# python-dotenv, damit das Projekt weiterhin ganz ohne externe Pakete auskommt)
# --------------------------------------------------------------------------

def _lade_env(path: Path = ENV_PATH) -> dict[str, str]:
    werte: dict[str, str] = {}
    # Eine fehlende .env-Datei ist an dieser Stelle noch kein Fehler. Ob am Ende
    # wirklich kein Key vorhanden ist, wird erst in _api_key() geprüft.
    if not path.exists():
        return werte
    for zeile in path.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#") or "=" not in zeile:
            continue
        key, _, value = zeile.partition("=")
        # .strip('"').strip("'") entfernt Anführungszeichen, falls jemand
        # NVIDIA_API_KEY="nvapi-..." mit Quotes schreibt (in .env-Dateien
        # allgemein üblich, wird aber hier nicht zwingend gebraucht).
        werte[key.strip()] = value.strip().strip('"').strip("'")
    return werte


def _api_key() -> str:
    # Eine echte Umgebungsvariable (falls gesetzt) hat Vorrang vor der .env-Datei,
    # damit man den Key z.B. in CI auch ohne .env-Datei setzen kann.
    key = os.environ.get("NVIDIA_API_KEY") or _lade_env().get("NVIDIA_API_KEY")
    if not key:
        raise RuntimeError(
            f"Kein NVIDIA_API_KEY gefunden. Datei {ENV_PATH} anlegen (siehe .env.example) "
            "mit einer Zeile wie:\nNVIDIA_API_KEY=nvapi-..."
        )
    return key


def aktuelles_modell() -> str:
    """Welches Modell gerade benutzt wird, wenn frage_ki_zu_treiber() keins
    explizit übergeben bekommt: echte Umgebungsvariable > NVIDIA_MODEL in
    der .env > DEFAULT_MODEL. Öffentlich (kein führender Unterstrich mehr),
    weil das jetzt auch von nfoanalyst.py für den Befehl 'ki modell' (ohne
    Argument, zum Anzeigen) gebraucht wird."""
    return os.environ.get("NVIDIA_MODEL") or _lade_env().get("NVIDIA_MODEL") or DEFAULT_MODEL


def aktuelles_timeout() -> int:
    """Wie lange (in Sekunden) auf eine KI-Antwort gewartet wird, nach
    demselben Muster wie aktuelles_modell(): echte Umgebungsvariable >
    NVIDIA_TIMEOUT in der .env > DEFAULT_TIMEOUT_SEKUNDEN. Ein ungültiger
    Wert (z.B. Tippfehler) fällt auf den Standard zurück statt mit einem
    ValueError abzubrechen, damit ein kaputter .env-Eintrag nicht gleich
    jede KI-Abfrage verhindert."""
    roh = os.environ.get("NVIDIA_TIMEOUT") or _lade_env().get("NVIDIA_TIMEOUT")
    if not roh:
        return DEFAULT_TIMEOUT_SEKUNDEN
    try:
        return int(roh)
    except ValueError:
        return DEFAULT_TIMEOUT_SEKUNDEN


def _setze_env_wert(key: str, value: str) -> None:
    """Setzt key=value in der .env-Datei, ohne andere Zeilen anzufassen
    (insbesondere nicht NVIDIA_API_KEY oder Kommentare). Ersetzt die
    passende Zeile, falls der Key schon vorkommt, sonst wird eine neue Zeile
    ans Ende angehängt. Bewusst kein Einsatz von _lade_env() hier: die liest
    zwar bequem in ein dict ein, wirft dabei aber Kommentare und die
    Zeilenreihenfolge weg, beides soll beim Zurückschreiben erhalten bleiben.
    """
    zeilen = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    neue_zeile = f"{key}={value}"
    for i, zeile in enumerate(zeilen):
        stripped = zeile.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == key:
            zeilen[i] = neue_zeile
            break
    else:
        zeilen.append(neue_zeile)
    ENV_PATH.write_text("\n".join(zeilen) + "\n", encoding="utf-8")


def setze_modell(name: str) -> None:
    """Setzt NVIDIA_MODEL in der .env auf name (Befehl 'ki modell <Name>').
    Prüft absichtlich NICHT vorher per Testaufruf, ob das Modell mit diesem
    Account wirklich nutzbar ist (das würde unnötig Tokens/Zeit kosten),
    siehe Dritter Fund im Modul-Docstring dazu, dass ein Eintrag im Katalog
    das nicht garantiert. Am besten direkt danach mit 'ki treiber <Nummer>'
    ausprobieren."""
    _setze_env_wert("NVIDIA_MODEL", name)


def _entferne_env_wert(key: str) -> None:
    """Gegenstück zu _setze_env_wert(): entfernt die Zeile zu key komplett
    aus der .env, falls vorhanden. Andere Zeilen bleiben unangetastet."""
    if not ENV_PATH.exists():
        return
    zeilen = ENV_PATH.read_text(encoding="utf-8").splitlines()
    neue_zeilen = [
        zeile for zeile in zeilen
        if not (
            "=" in zeile.strip()
            and not zeile.strip().startswith("#")
            and zeile.strip().split("=", 1)[0].strip() == key
        )
    ]
    inhalt = "\n".join(neue_zeilen)
    ENV_PATH.write_text(inhalt + "\n" if inhalt else "", encoding="utf-8")


def setze_modell_standard() -> None:
    """Entfernt eine evtl. gesetzte NVIDIA_MODEL-Zeile aus der .env, damit
    aktuelles_modell() wieder auf DEFAULT_MODEL zurückfällt (Teil von 'ki
    reset'). Absichtlich die Zeile entfernen statt sie explizit auf den
    aktuellen Wert von DEFAULT_MODEL zu setzen: sonst würde ein späteres
    Ändern von DEFAULT_MODEL im Code von einem alten, expliziten
    .env-Eintrag überschattet bleiben."""
    _entferne_env_wert("NVIDIA_MODEL")


def liste_modelle(suchtext: str | None = None) -> list[str]:
    """Fragt den Modell-Katalog von build.nvidia.com live ab (GET
    /v1/models, dieselbe Authentifizierung wie beim eigentlichen
    Chat-Aufruf) und gibt die Modellnamen zurück, alphabetisch sortiert und
    optional nach suchtext gefiltert (Teilstring, Groß-/Kleinschreibung
    egal, z.B. "llama"). Enthält absichtlich alle Modelle, auch
    Embedding-/Vision-/Audio-Modelle, die für diesen Text-Chat nicht
    passen: eine Filterung nach Namensmustern wäre nur geraten, siehe
    Dritter Fund im Modul-Docstring dazu, dass selbst naheliegend aussehende
    Namen (z.B. mit "instruct") nicht zuverlässig etwas über die
    tatsächliche Nutzbarkeit aussagen.
    """
    api_key = _api_key()
    request = urllib.request.Request(
        MODELS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            antwort = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        fehlertext = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"NVIDIA-API-Fehler {exc.code}: {fehlertext}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"NVIDIA-API nicht erreichbar: {exc.reason}") from exc
    except TimeoutError as exc:
        # Derselbe Fall wie in _rufe_nvidia_api() (siehe Vierter Fund im
        # Modul-Docstring): eine rohe TimeoutError ist keine urllib.error-Klasse.
        raise RuntimeError("Zeitüberschreitung nach 30s beim Abfragen des Modell-Katalogs.") from exc

    namen = [eintrag["id"] for eintrag in antwort.get("data", [])]
    if suchtext:
        namen = [name for name in namen if suchtext.lower() in name.lower()]
    return sorted(namen)


def modell_existiert(name: str) -> bool | None:
    """Prüft, ob name exakt im aktuellen Modell-Katalog auftaucht (siehe
    liste_modelle()). Gibt None zurück, statt einen Fehler zu werfen, wenn
    der Katalog gerade nicht abgefragt werden konnte (z.B. weil die
    NVIDIA-Infrastruktur selbst gerade Probleme hat): 'ki modell <Name>'
    soll trotzdem funktionieren, auch wenn nur dieser Extra-Check gerade
    nicht klappt. Fängt zum Beispiel einen Tippfehler wie 'ki modell chat'
    ab, der sonst erst beim nächsten 'ki treiber' mit einem verwirrenden
    404 auffällt (das ist beim Testen tatsächlich passiert)."""
    try:
        katalog = liste_modelle()
    except RuntimeError:
        return None
    return name in katalog


# --------------------------------------------------------------------------
# Persönliche Zusatzfrage (Befehl 'ki frage'), wird ab dann bei jeder
# 'ki treiber <Nummer>'-Anfrage zusätzlich zum Standard-Prompt mitgestellt
# --------------------------------------------------------------------------

def lade_zusatzfrage() -> str | None:
    """Liest die gespeicherte Zusatzfrage aus ZUSATZFRAGE_PATH, falls
    gesetzt (sonst None). Kein Fehler, wenn die Datei fehlt: das ist der
    normale Zustand, solange niemand 'ki frage <Text>' benutzt hat."""
    if not ZUSATZFRAGE_PATH.exists():
        return None
    text = ZUSATZFRAGE_PATH.read_text(encoding="utf-8").strip()
    return text or None


def setze_zusatzfrage(text: str) -> None:
    ZUSATZFRAGE_PATH.write_text(text.strip(), encoding="utf-8")


def loesche_zusatzfrage() -> None:
    # missing_ok=True statt vorher extra .exists() zu prüfen: unlink() selbst
    # kümmert sich darum, dass "Zusatzfrage löschen" auch dann kein Fehler
    # ist, wenn ohnehin gar keine gesetzt war.
    ZUSATZFRAGE_PATH.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Treiber aus dem letzten analyze-Bericht per Nummer laden
# --------------------------------------------------------------------------

def _load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def lade_treiber_nach_nummer(index: int, input_dir: Path | str | None = None) -> dict:
    """Lädt Treiber Nummer `index` aus treiber.json (dieselbe, älteste-zuerst
    sortierte Liste, die auch der Befehl 'treiber' nummeriert anzeigt)."""
    input_dir = Path(input_dir) if input_dir else DEFAULT_INPUT_DIR
    pfad = input_dir / "treiber.json"
    if not pfad.exists():
        raise FileNotFoundError(f"{pfad} nicht gefunden. Erst 'analyze' ausführen.")

    treiberpakete = _load_json(pfad)["treiberpakete"]
    if index < 1 or index > len(treiberpakete):
        raise IndexError(
            f"Ungültige Treiber-Nummer {index}. Gültig sind 1 bis {len(treiberpakete)} "
            "(siehe Befehl 'treiber' für die nummerierte Liste)."
        )
    return treiberpakete[index - 1]


# --------------------------------------------------------------------------
# Payload bauen und an die NVIDIA-API schicken
# --------------------------------------------------------------------------

def baue_payload(treiber: dict, zusatzfrage: str | None = None) -> list[dict]:
    """Baut den Prompt für die KI. zusatzfrage ist optional (siehe
    lade_zusatzfrage()): wenn gesetzt, wird sie als zusätzlicher Punkt in den
    Prompt eingebaut und die KI muss dafür ein weiteres Feld "zusatzantwort"
    im JSON beantworten.
    """
    # Lazy-Import (wie green.green in analyze.erstelle_bericht()): baue_payload()
    # soll auch dann funktionieren, wenn analyze.py aus irgendeinem Grund mal
    # nicht importierbar ist, und wer nur ki.py als Bibliothek nutzt, muss
    # analyze.py nicht zwangsläufig mitladen.
    from analyze.analyze import STANDARD_KRITISCH_JAHRE, STANDARD_WARNUNG_JAHRE

    # Das Antwortformat wird hier als Liste von (Feldname, Beispielwert)
    # zusammengebaut statt als fester String, weil das optionale
    # "zusatzantwort"-Feld nur angehängt werden soll, wenn tatsächlich eine
    # Zusatzfrage gestellt wird. Ein Format mit unbenutztem Feld würde die KI
    # nur verwirren ("was soll ich da reinschreiben?").
    felder = [
        '"wirklich_veraltet": true',
        '"vermutlich_neuere_version_verfuegbar": true',
        '"weiterhin_kritisch": true',
        '"empfehlung": "..."',
        '"begruendung": "..."',
    ]
    zusatz_absatz = ""
    if zusatzfrage:
        felder.append('"zusatzantwort": "..."')
        zusatz_absatz = (
            "\nZusätzliche Frage, die du im Feld 'zusatzantwort' in maximal zwei kurzen "
            f"Sätzen beantwortest: {zusatzfrage}\n"
        )
    format_zeile = "{" + ", ".join(felder) + "}"

    # Der Prompt schreibt das JSON-Antwortformat ganz genau vor (inklusive dem
    # "unsicher"-Ausweg für die beiden Wahrheitswert-Felder), weil große
    # Sprachmodelle sonst gerne mal einen erklärenden Satz vor oder nach dem
    # JSON dazuschreiben. _extrahiere_json() fängt das zwar zur Not ab, aber
    # ein sauberer Prompt spart unnötige Fehlversuche.
    frage = (
        "Du bist ein Experte für Windows-Gerätetreiber. Ich gebe dir Metadaten zu einem "
        "installierten Treiberpaket. Schätze anhand deines Wissens ein, ob dieser Treiber "
        "wirklich veraltet ist, ob es wahrscheinlich eine neuere Version des Treibers gibt, "
        "und ob eine Einstufung als 'kritisch' hier gerechtfertigt ist oder überzogen wäre. "
        "Ein Analyseprogramm hat diesen Treiber bereits rein nach seinem Alter automatisch "
        f"eingestuft: ab {STANDARD_WARNUNG_JAHRE:.0f} Jahren als 'warnung', ab "
        f"{STANDARD_KRITISCH_JAHRE:.0f} Jahren als 'kritisch', ohne dabei die Art des Treibers "
        "zu berücksichtigen. Genau diese pauschale Regel sollst du fachlich einordnen: manche "
        "Treiber, zum Beispiel für einfache Chipsatz- oder USB-Komponenten, ändern sich "
        "jahrelang nicht und sind trotzdem völlig in Ordnung, andere (z.B. Netzwerk-, "
        "Grafik- oder sicherheitsrelevante Treiber) sollten deutlich öfter aktualisiert werden. "
        "Gib deshalb im Feld 'empfehlung' einen kurzen, konkreten nächsten Schritt an (zum "
        "Beispiel 'Über Windows Update aktualisieren', 'Kein Handlungsbedarf', 'Bei "
        "Hersteller-Webseite nach neuerer Version suchen' oder 'Prüfen, ob das Gerät überhaupt "
        "noch verwendet wird'), keine allgemeine Floskel.\n"
        "Sei ehrlich, wenn du dir bei einem so speziellen INF-Namen nicht sicher sein kannst, "
        "und antworte NUR mit einem JSON-Objekt in genau diesem Format, ohne weiteren Text "
        f"drumherum:\n{format_zeile}\n"
        "Antworte extrem kurz und knapp, damit die Antwort schnell fertig ist: 'empfehlung' "
        "in maximal 6 Worten als klarer nächster Schritt, 'begruendung' in maximal einem "
        "kurzen Satz (ca. 15 Worte). Kein einleitender Satz, keine Wiederholung der "
        "Eingabedaten, kein Codeblock (kein ```), nur das reine JSON-Objekt.\n"
        "Für wirklich_veraltet und vermutlich_neuere_version_verfuegbar darfst du statt "
        'true/false auch den String "unsicher" benutzen, wenn du es nicht einschätzen kannst.\n'
        "Manche Treiber haben keine Versions-/Datumsangabe (steht dann als None da). Nutze in "
        "dem Fall Beschreibung, Dateiname und dein Allgemeinwissen für deine Einschätzung.\n"
        f"{zusatz_absatz}\n"
        "Treiberdaten:\n"
        f"- Name/INF-Datei: {treiber.get('originalname')}\n"
        f"- Anbieter: {treiber.get('anbietername')}\n"
        f"- Geräteklasse: {treiber.get('klassenname')}\n"
        f"- Beschreibung: {treiber.get('beschreibung')}\n"
        f"- Treiberdatei: {treiber.get('datei')}\n"
        f"- Treiberversion: {treiber.get('version')}\n"
        f"- Datum laut Treiber: {treiber.get('datum')}\n"
        f"- Alter in Jahren (heute berechnet): {treiber.get('alter_jahre')}\n"
        f"- Signaturgeber: {treiber.get('signaturgeber')}\n"
        f"- Bisherige Alterseinstufung (kritisch/warnung/ok/unbekannt): {treiber.get('status')}\n"
        f"- Aktueller Laufzeitstatus: {treiber.get('laufzeitstatus')}\n"
    )
    return [{"role": "user", "content": frage}]


def _rufe_nvidia_api(
    messages: list[dict],
    modell: str,
    api_key: str,
    timeout: int = DEFAULT_TIMEOUT_SEKUNDEN,
    max_tokens: int = 512,
) -> tuple[str, dict | None]:
    # NVIDIAs NIM-API ist zur OpenAI-Chat-API kompatibel, "messages" hat also
    # genau die bekannte Form [{"role": "user", "content": "..."}]. temperature
    # niedrig halten (0.2), weil wir eine möglichst nüchterne Einschätzung
    # wollen und keine kreativen/abwechslungsreichen Antworten. max_tokens ist
    # ein Parameter (nicht mehr fest 512), damit teste_modell() eine winzige
    # Anfrage mit max_tokens=5 schicken kann, siehe Siebter Fund im
    # Modul-Docstring.
    body = {
        "model": modell,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            antwort = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # HTTPError: die API hat geantwortet, aber mit einem Fehlercode (401/403 =
        # Key falsch, 503 = Modell gerade überlastet, siehe Fehler-und-Lösungen
        # oben). exc.read() gibt den JSON-Fehlertext der API mit aus, der meistens
        # steht, was genau schiefging. 504 = "Gateway Timeout": NICHT unser
        # eigener Timeout, sondern der vorgeschaltete API-Gateway von NVIDIA hat
        # innerhalb seines eigenen (kürzeren) internen Timeouts keine Antwort vom
        # Modell-Backend bekommen, siehe Siebter Fund im Modul-Docstring.
        fehlertext = exc.read().decode("utf-8", errors="replace")
        hinweis = (
            " (Gateway Timeout: das ist NVIDIAs eigener Server, der beim Warten "
            "auf das Modell-Backend aufgegeben hat, keine Zeitüberschreitung auf "
            "unserer Seite. Tritt das bei mehreren/allen Modellen gleichzeitig "
            "auf, deutet das auf eine vorübergehende Störung der gesamten "
            "build.nvidia.com-Infrastruktur hin, siehe Modul-Docstring. Mit "
            "'ki test' lässt sich das schnell prüfen.)"
            if exc.code == 504 else ""
        )
        raise RuntimeError(f"NVIDIA-API-Fehler {exc.code}: {fehlertext}{hinweis}") from exc
    except urllib.error.URLError as exc:
        # URLError: die Verbindung kam gar nicht erst zustande (kein Internet,
        # DNS-Problem, Firewall). Anders als bei HTTPError gibt es hier keine
        # Antwort der API zum Auslesen.
        raise RuntimeError(f"NVIDIA-API nicht erreichbar: {exc.reason}") from exc
    except TimeoutError as exc:
        # Eigener except-Zweig, weil das beim Testen (Modell moonshotai/kimi-k3)
        # tatsächlich passiert ist und NICHT von den beiden Zweigen oben
        # abgefangen wurde: die Verbindung stand schon (sonst wäre es eine
        # URLError), aber response.read() ist beim Warten auf die Antwort mit
        # einer rohen TimeoutError abgebrochen, nicht mit einer der
        # urllib.error-Klassen. Ohne diesen Zweig stürzte die ganze REPL mit
        # einem Traceback ab, siehe Vierter Fund im Modul-Docstring.
        raise RuntimeError(
            f"Zeitüberschreitung nach {timeout}s beim Warten auf die Antwort von '{modell}'. "
            "Manche Modelle sind langsam oder gerade instabil, ein anderes Modell "
            "probieren (siehe 'ki modelle') oder es nochmal versuchen."
        ) from exc

    # Die NIM-API ist OpenAI-kompatibel und liefert deshalb (wie bei OpenAI)
    # neben der eigentlichen Antwort auch ein "usage"-Feld mit dem
    # Token-Verbrauch mit. .get() statt antwort["usage"], falls ein Modell
    # das mal nicht mitschickt, dann ist der Verbrauch eben nicht bekannt
    # (None), statt dass gleich die ganze Anfrage mit einem KeyError abbricht.
    verbrauch = antwort.get("usage")
    return antwort["choices"][0]["message"]["content"], verbrauch


# --------------------------------------------------------------------------
# Erreichbarkeits-Test (Befehl 'ki test'): eine winzige Anfrage statt einer
# echten Treiber-Einschätzung, um schnell zu sehen, ob ein Modell mit dem
# eigenen Account und der aktuellen Verbindung überhaupt antwortet, siehe
# Siebter Fund im Modul-Docstring.
# --------------------------------------------------------------------------

def teste_modell(modell: str | None = None, timeout: int | None = None) -> dict:
    """Schickt eine minimale Testanfrage ("Antworte nur mit dem einzigen Wort
    OK.", max_tokens=5) an ein Modell und misst, ob und wie schnell es
    antwortet. Anders als frage_ki_zu_treiber() braucht das weder einen
    vorherigen 'analyze'-Lauf noch echte Treiberdaten, und verbraucht dank
    max_tokens=5 auch praktisch keine Tokens.

    Wirft absichtlich KEINEN Fehler, sondern gibt immer ein dict zurück, auch
    im Fehlerfall (mit "erfolgreich": False und "fehler": <Meldung>). Das
    macht es einfach, mehrere Modelle in einer Schleife durchzutesten, ohne
    dass ein einzelner Fehlschlag den ganzen Durchlauf abbricht (siehe
    teste_katalog()).

    modell:  zu testendes Modell, Standard: aktuelles_modell().
    timeout: wie lange gewartet wird, Standard: TEST_TIMEOUT_SEKUNDEN (30s,
             absichtlich viel kürzer als DEFAULT_TIMEOUT_SEKUNDEN, siehe dort).
    """
    modell = modell or aktuelles_modell()
    timeout = timeout if timeout is not None else TEST_TIMEOUT_SEKUNDEN
    start = time.monotonic()

    try:
        api_key = _api_key()
    except RuntimeError as exc:
        return {
            "modell": modell, "erfolgreich": False, "dauer_sekunden": None,
            "fehler": str(exc), "antwort": None,
        }

    messages = [{"role": "user", "content": "Antworte nur mit dem einzigen Wort OK."}]
    try:
        antwort, _ = _rufe_nvidia_api(messages, modell, api_key, timeout=timeout, max_tokens=5)
    except RuntimeError as exc:
        return {
            "modell": modell, "erfolgreich": False,
            "dauer_sekunden": round(time.monotonic() - start, 1),
            "fehler": str(exc), "antwort": None,
        }

    return {
        "modell": modell, "erfolgreich": True,
        "dauer_sekunden": round(time.monotonic() - start, 1),
        "fehler": None, "antwort": antwort,
    }


def teste_katalog(suchtext: str | None = None, timeout: int = TEST_TIMEOUT_SEKUNDEN) -> list[dict]:
    """Testet reihum JEDES Modell aus dem Katalog (optional per suchtext
    gefiltert, siehe liste_modelle()) mit teste_modell() und gibt die
    Ergebnisliste zurück (Befehl 'ki test alle [Suchtext]').

    Gedacht, um bei einem Problem wie "jedes Modell liefert einen Fehler"
    schnell zu erkennen, ob das an einzelnen Modellen liegt oder wirklich die
    gesamte Verbindung/den ganzen Account betrifft (siehe Siebter Fund im
    Modul-Docstring). Bei über 80 Katalogeinträgen kann das trotz des kurzen
    Timeouts pro Modell insgesamt einige Minuten dauern, deshalb wird der
    Fortschritt direkt beim Testen ausgegeben statt erst ganz am Ende.
    """
    ergebnisse = []
    for name in liste_modelle(suchtext):
        print(f"  teste {name}...", end=" ", flush=True)
        ergebnis = teste_modell(name, timeout=timeout)
        if ergebnis["erfolgreich"]:
            print(f"OK ({ergebnis['dauer_sekunden']}s)")
        else:
            print(f"FEHLER ({ergebnis['fehler']})")
        ergebnisse.append(ergebnis)
    return ergebnisse


def _extrahiere_json(text: str | None) -> dict:
    """Die KI soll nur JSON antworten, hält sich aber nicht immer daran (manchmal
    mit ```json-Codeblock drumherum oder einem Satz davor/danach). Deshalb wird
    hier robust nur das erste {...} aus der Antwort herausgeschnitten, statt
    json.loads() strikt auf die komplette Antwort anzuwenden.

    text kann auch None sein: manche "Reasoning"-Modelle (bei denen die
    Gedankenkette in einem extra Feld "reasoning_content" statt in "content"
    steht) haben beim Testen ihr ganzes max_tokens-Budget für die
    unsichtbare Gedankenkette verbraucht und "content" blieb leer. Ohne
    diese Prüfung wäre text.find() weiter unten mit einer AttributeError
    abgestürzt, siehe Dritter Fund im Modul-Docstring.
    """
    if text is None:
        return {"rohtext": "(keine Antwort erhalten, evtl. Reasoning-Modell ohne genug max_tokens)"}
    start = text.find("{")
    ende = text.rfind("}")
    if start == -1 or ende == -1:
        return {"rohtext": text.strip()}
    try:
        return json.loads(text[start:ende + 1])
    except json.JSONDecodeError:
        return {"rohtext": text.strip()}


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  gespeichert: {path.parent.name}/{path.name}")


# --------------------------------------------------------------------------
# Alles zusammen
# --------------------------------------------------------------------------

def frage_ki_zu_treiber(
    index: int,
    input_dir: Path | str | None = None,
    output_dir: Path | str | None = None,
    modell: str | None = None,
    zusatzfrage: str | None = None,
    timeout: int | None = None,
) -> dict:
    """Lädt Treiber Nummer `index`, schickt seine Daten an ein NVIDIA-NIM-Modell
    und gibt die Einschätzung der KI zurück. Speichert das Ergebnis zusätzlich
    unter data/output/report/ki/treiber_<index>.json.

    zusatzfrage: einmalige Zusatzfrage nur für diesen einen Aufruf (Befehl
    'ki treiber <Nummer> <Frage>'). Ohne dieses Argument wird automatisch die
    dauerhaft gespeicherte Zusatzfrage benutzt (siehe lade_zusatzfrage(),
    Befehl 'ki frage'), falls eine gesetzt ist. Die einmalige Frage
    überschreibt die gespeicherte nur für diesen Aufruf, ohne sie zu ändern.

    timeout: wie lange (in Sekunden) auf die Antwort gewartet wird, bevor
    aufgegeben wird. Ohne Angabe wird aktuelles_timeout() benutzt (Standard
    DEFAULT_TIMEOUT_SEKUNDEN, per NVIDIA_TIMEOUT in der .env anpassbar).
    """
    output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    modell = modell or aktuelles_modell()
    timeout = timeout if timeout is not None else aktuelles_timeout()

    # Bewusst zuerst den Treiber laden und erst danach den API-Key prüfen:
    # eine falsche Nummer soll man sofort erfahren, ohne dass man sich vorher
    # fragen muss, ob vielleicht der API-Key das Problem war.
    treiber = lade_treiber_nach_nummer(index, input_dir)
    api_key = _api_key()

    aktive_zusatzfrage = zusatzfrage if zusatzfrage is not None else lade_zusatzfrage()
    messages = baue_payload(treiber, aktive_zusatzfrage)
    rohantwort, token_verbrauch = _rufe_nvidia_api(messages, modell, api_key, timeout=timeout)
    einschaetzung = _extrahiere_json(rohantwort)

    ergebnis = {
        "index": index,
        "treiber": {
            "quelle": treiber.get("quelle"),
            "originalname": treiber.get("originalname"),
            "beschreibung": treiber.get("beschreibung"),
            "version": treiber.get("version"),
            "datum": treiber.get("datum"),
            "alter_jahre": treiber.get("alter_jahre"),
            "bisherige_einstufung": treiber.get("status"),
        },
        "modell": modell,
        "zusatzfrage": aktive_zusatzfrage,
        "token_verbrauch": token_verbrauch,
        "ki_einschaetzung": einschaetzung,
    }

    _save_json(output_dir / f"treiber_{index}.json", ergebnis)
    return ergebnis


def main() -> int:
    """Dünner CLI-Wrapper für den direkten Aufruf `python ki.py treiber <Nummer>`."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("art", choices=["treiber"], help="Bisher nur 'treiber' unterstützt.")
    parser.add_argument("nummer", type=int, help="Nummer aus der Liste des Befehls 'treiber'.")
    parser.add_argument("--modell", default=None, help="Überschreibt NVIDIA_MODEL aus der .env.")
    args = parser.parse_args()

    try:
        ergebnis = frage_ki_zu_treiber(args.nummer, modell=args.modell)
    except (FileNotFoundError, IndexError, RuntimeError) as exc:
        print(f"Fehler: {exc}")
        return 1

    print(json.dumps(ergebnis, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
