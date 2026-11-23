# Lesesaal

Eine lokale Pipeline, die Podcasts, YouTube-Videos, TikTok-Videos, Fediverse-Konten und Terminkalender abfragt, Audio transkribiert, alles mit einem lokalen Sprachmodell auswertet und das Ergebnis als kleinen, durchsuchbaren Online-Zettelkasten veröffentlicht. Zu jeder neuen Notiz geht ein Hinweis als Thread auf Mastodon. Am Ende jedes Laufs kommt ein Bericht als Direktnachricht.

Alles läuft auf einem eigenen Rechner. Kein Text und kein Audio geht an einen externen KI-Dienst.

Der Code ist mit Hilfe eines KI-Assistenten entstanden und wird von Hand getestet.

## Was dabei herauskommt

Aus einer Folge von mehreren Stunden wird eine Notiz mit höchstens acht Einträgen pro Liste. Ausgewählt wird nach einem Interessenprofil, das ich selbst schreibe. Jeder Eintrag hat einen Titel, eine Art (Werkzeug, Projekt, Organisation, These und so weiter), ein bis drei Sätze und eine Zeitmarke. Ein Klick auf die Zeitmarke spielt die Folge genau an dieser Stelle ab, im Player der Seite oder im Video.

Formate:

- **Tech-Radar:** Liste der Fundstücke einer Folge.
- **Gesprächsnotiz:** für Interviews, getrennt in „Thesen und Begriffe“ und „Hinweise“.
- **Finanznotiz:** wie die Gesprächsnotiz, mit Hinweisen auf Unternehmen, Institutionen und Ereignisse.
- **Lernnotiz:** Begriffe, Regeln und Methoden, dazu Gesetze, Standards und Literatur.
- **Meldungsliste:** Tagesnotiz aus öffentlichen Fediverse-Konten, je Eintrag mit Link zum Beitrag und den Links darin.
- **Termine:** eine Seite mit den ausgewählten Terminen der nächsten Wochen und ein Kalender zum Abonnieren.

Die Seite ist statisch (Hugo), hat eine Volltextsuche im Browser (Pagefind), Seiten pro Hashtag, Quelle und Format und RSS-Feeds. Sie liegt unter einem nicht erratbaren Pfad und ist für Suchmaschinen gesperrt: lesbar für alle, die den Link haben, aber nicht auffindbar.

## Ablauf

```
Feed, Kanal ──► holen ──► transkribieren ──► auswerten ──┐
(Podcast, YouTube,  (Audio)   (faster-whisper)   (Ollama)   │
 TikTok)                                                   ├──► bauen + hochladen ──► posten
Fediverse-Konten ──────────► szene ────────────────────────┤    (Hugo, Pagefind,      (Mastodon-
Kalender, Feeds, Pads ─────► termine ──────────────────────┘     lftp)                 Thread)
```

`tagesbetrieb.py` führt diese Schritte für alle Quellen nacheinander aus und merkt sich den Stand jeder Folge.

1. **Feeds abfragen.** Beim ersten Lauf einer Quelle kommen die neueste Folge und `rueckstand` ältere dazu, danach nur Folgen, die neuer sind als die bekannten. Mit `vorauswahl` bewertet das Modell vorher die Titel gegen das Interessenprofil und nimmt nur die passendsten.
2. **Laden und transkribieren**, neue Folgen zuerst. Bei Videos nur die Tonspur. Die Audiodatei wird danach gelöscht.
3. **Auswerten**, erst wenn alles transkribiert ist. Whisper und das Sprachmodell passen nicht gleichzeitig in den Grafikspeicher.
4. **Fedi:** öffentliche Beiträge ausgewählter Fediverse-Konten lesen und als Tagesnotiz „Fedi am TT.MM.JJJJ“ auswerten. Die Quellenseite zeigt alle beobachteten Konten mit Profil, Zustand des Feeds und Zahl der gelesenen Beiträge (`inhalt/quelle/szene/_index.md`, bei jedem Lauf erneuert). Intern heißen Skript, Ordner und Kennung weiter `szene`.
5. **Termine:** Kalender, Feeds und Pads lesen, Termine auswählen, Seite und Kalender schreiben.
6. **Veröffentlichen:** Seite bauen und hochladen, je neue Notiz einen Thread posten, die Notizen im Notizen-Repository committen.
7. **Bericht** als Datei und als Direktnachricht, mit den Terminen der nächsten 48 Stunden, den neuen Notizen nach Quelle gruppiert und einem Link zur Seite.

Stufen je Folge: `gefunden`, `geladen`, `transkribiert`, `ausgewertet`, `veroeffentlicht`, `ohne_funde` oder `uebersprungen`. Der Stand liegt in `DATEN/stand.json`. Bricht ein Lauf ab, macht der nächste dort weiter. Was schon existiert, wird nicht noch einmal gemacht. Nach drei Fehlern bleibt eine Folge liegen und erscheint im Bericht.

**Stoppsignal:** Die Datei `DATEN/stopp`. Zwischen zwei Schritten hält das Programm sofort an. Eine laufende Transkription oder Auswertung macht es fertig, wenn der geschätzte Rest in `stopp_hoechstens_minuten` passt, sonst bricht es sie ab. Fertige Notizen werden noch veröffentlicht. Steht in der Datei `aus`, fährt der Rechner danach herunter.

**Temperatur:** Erreicht eine Grafikkarte `temperatur_pause`, wird der laufende Schritt mit SIGSTOP angehalten, bis alle Karten unter `temperatur_weiter` liegen.

## Auswertung im Detail

Das lokale Modell (qwen3:32b mit 8.192 Token Kontext) bekommt das Transkript in Abschnitten von rund 1.800 Wörtern. Was sich bewährt hat:

- **Einordnen statt verbieten.** Verbote in der Anweisung befolgt das Modell nicht zuverlässig. Es liefert deshalb zu jedem Kandidaten eine Einordnung: konkreter Name, Teil einer Erzählung, Hinweis der Sendung auf sich selbst, Relevanz von 1 bis 5. Der Code sortiert danach aus und zählt die Gründe.
- **Belegstelle.** Zu jedem Kandidaten gibt das Modell ein wörtliches Stück aus dem Transkript an. Der Code sucht es. Fehlt es, oder steht in einer Minute Umkreis kein Wort des Titels, fällt der Eintrag weg. Die Zeitmarke kommt sekundengenau aus der gefundenen Stelle. Das verhindert erfundene Einträge, die nur zum Interessenprofil passen.
- **Zusammenführen mit Nachdenken.** Die relevantesten Kandidaten jeder Liste gehen in einen letzten Durchgang, der Doppeltes zusammenfasst. Der Code prüft, dass nur eingereichte Zeitmarken zurückkommen, und bringt sie vorher in eine einheitliche Form. Hat die Folge nur einen Abschnitt und nicht mehr Einträge, als die Notiz aufnimmt, entfällt der Durchgang, weil es nichts zusammenzuführen gibt. Erreicht das Nachdenken die Grenze von 4.000 Token, ohne zu antworten, folgt ein zweiter Versuch ohne Nachdenken. Erst wenn auch der scheitert, wird nach Relevanz ausgewählt.
- **Hashtags ohne Interessenprofil.** Ein eigener Durchgang sieht nur die fertigen Einträge. Danach gleicht der Code die Schreibweise an vorhandene Hashtags an, und ein kurzer Durchgang meldet echte Entsprechungen wie KI und KuenstlicheIntelligenz. Neue Hashtags bleiben erlaubt, aber nur als Schlagwort: Hashtags aus mehr als drei Wortteilen oder mit Füllwörtern wie Und, Ist oder Von verwirft der Code, etwa KriegIstProfitabelFuerKapital.
- **Schutz gegen Überlauf.** Läuft ein Abschnitt fast über den Kontext, wird er geteilt.
- **Entscheidungen speichern.** Das Modell antwortet auf dieselbe Frage nicht immer gleich. Bei Terminen bewertet es jede Reihe deshalb nur einmal, die Entscheidung bleibt gespeichert und lässt sich von Hand ändern.
- **Zählen und Daten macht der Code**, nicht das Modell.

Jede Notiz hat zuerst den Status „ungeprüft“. Nach dem Abgleich mit dem Original wird sie mit `pruefen.py` auf „geprüft“ gesetzt.

## Dateien

| Datei | Aufgabe |
|---|---|
| `holen.py` | Feed, YouTube-Kanal oder TikTok-Konto anzeigen, eine Folge laden, Angaben als `.meta.json` daneben |
| `transkribieren.py` | Audio mit faster-whisper transkribieren, Ergebnis als JSON und Text |
| `auswerten.py` | Transkript mit dem Sprachmodell auswerten, Notiz schreiben; enthält die Formate |
| `szene.py` | Fediverse-Konten über ihre RSS-Feeds lesen, mit Ausweichsuche über WebFinger, als Tagesnotiz auswerten |
| `termine.py` | Termine aus iCal, Feeds und Pads sammeln, auswählen, Seite und Kalender schreiben |
| `bauen.py` | Seite mit Hugo bauen, Suchindex mit Pagefind, per FTPS hochladen |
| `posten.py` | Hinweis auf eine Notiz als Thread bei Mastodon |
| `threads_loeschen.py` | alle Threads des Kontos löschen, Direktnachrichten bleiben |
| `tagesbetrieb.py` | alle Schritte für alle Quellen, mit Stand, Stopp, Temperatur, Bericht |
| `pruefen.py` | Status einer Notiz auf „geprüft“ setzen, committen, hochladen |
| `config.toml` | Pfade, Modell, Whisper, Quellen, Tagesbetrieb, Fedi (Abschnitt `[szene]`), Termine, Adresse der Seite |
| `seite/` | Hugo-Konfiguration und Vorlagen |
| `requirements.txt` | Python-Pakete |

Notizen, Interessenprofil und die persönlichen Listen liegen in einem eigenen, privaten Repository. Hugo liest die Notizen von dort (`--contentDir`).

## Voraussetzungen

- Linux mit NVIDIA-Grafikkarten. Getestet auf Ubuntu Studio 26.04 mit zwei RTX 3060 (je 12 GB) und einem Intel Core i7-2700K.
- Python 3.12 (pyenv). Das System-Python 3.14 ist für faster-whisper und CTranslate2 zu neu.
- [Ollama](https://ollama.com) mit `qwen3:32b`. Dauerhafte Einstellungen: `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_CONTEXT_LENGTH=8192`, `OLLAMA_KV_CACHE_TYPE=q8_0`. So liegt das Modell vollständig auf zwei 12-GB-Karten.
- Ein Whisper-Modell im CTranslate2-Format, hier das Modell „precise“ von noScribe.
- ffmpeg, lftp, git, [yt-dlp](https://github.com/yt-dlp/yt-dlp) (am besten über pipx).
- [Hugo](https://gohugo.io) extended (getestet 0.164.0).
- [Pagefind](https://pagefind.app) (getestet 1.6.0). Auf Prozessoren ohne AVX2 stürzen die fertigen Pakete ab. Dann selbst kompilieren: `cargo install pagefind --locked --root ~/.local`.
- Webspace mit FTPS-Zugang und ein Mastodon-Konto mit einem Token für `write:statuses`, `read:statuses` und `profile`. Das Leserecht braucht nur `threads_loeschen.py`.

## Einrichtung

```
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Die CUDA-Bibliotheken kommen als Python-Pakete in die `.venv` (nvidia-cublas-cu12, nvidia-cudnn-cu12, nvidia-cuda-nvrtc-cu12). `transkribieren.py` startet sich einmal neu und setzt `LD_LIBRARY_PATH` darauf. Das Audio dekodiert ffmpeg, nicht PyAV.

`.env` im Projektordner, Rechte 600, nie im Repository:

```
FTP_HOST=...
FTP_BENUTZER=...
FTP_PASSWORT=...
GEHEIMER_PFAD=...          # etwa python3 -c "import secrets; print(secrets.token_urlsafe(12))"
MASTODON_ADRESSE=https://...
MASTODON_TOKEN=...
```

Im Notizen-Repository:

- `interessenprofil.md` mit den Abschnitten `## Ausrichtung`, `## Tech`, `## Lernstoff`, `## Theorie`, `## Finanzen`, `## Netzwerk und Andocken`, `## Grenzfälle`
- der Ordner `inhalt/notizen/`
- `szene.txt`: eine Fediverse-Adresse pro Zeile, etwa `@konto@instanz.example`
- `termine.txt`: eine Terminquelle pro Zeile, `typ adresse Name | Ort`, mit den Typen `ical` (Kalenderdatei), `feed` (RSS, Datum im Titel wie „am 25. November um 19 Uhr“) und `pad` (Textexport eines Pads mit Zeilen „★ TT.MM. HH:MM - HH:MM ~ Titel“)
- `belegt.txt`, freiwillig: eigene feste Termine, etwa `Di Do 18:00-21:30 Kurs ab 2026-09-01 bis 2027-06-30`. Termine, die darin aufgehen, kommen nicht in den Kalender und nicht in die Direktnachricht, sondern auf der Seite in einen eigenen Abschnitt.

Zeilen mit `#` sind in allen drei Listen Kommentare.

## Konfiguration

Eine Podcast-Quelle in `config.toml`:

```toml
[[quelle]]
kennung = "netzpolitik-offon"          # wird zur Adresse der Quellenseite
name = "netzpolitik.org Off/On"        # wird angezeigt
typ = "podcast"
feed = "https://netzpolitik.org/feed/podcast/off-on/"
sprache = "de"                          # ohne Angabe erkennt Whisper die Sprache selbst
zweck = "tech"
rueckstand = 1                          # optional, sonst Vorgabe aus [betrieb]
formate = [{muster = "On The Record", format = "gespraech"}, {muster = "Off The Record", format = "tech"}]
hinweise = "..."                        # optional, geht in die Anweisung an das Modell
```

Ein YouTube-Kanal und ein TikTok-Konto:

```toml
[[quelle]]
kennung = "beispielkanal"
name = "Beispielkanal"
typ = "youtube"
kanal_id = "UC..."
sprache = "en"
zweck = "finanzen"
format = "finanzen"
sprunglink = "&t={sekunden}s"           # Zeitmarke als Link ins Video
hoechstens_pro_lauf = 1                 # optional
vorauswahl = true                       # optional: Titel vorher bewerten lassen
vorauswahl_ab = 3                       # optional: Mindestrelevanz
ohne_livestreams = true                 # optional
hoechstdauer_minuten = 90               # optional

[[quelle]]
kennung = "beispielkonto"
name = "Beispielkonto"
typ = "tiktok"
konto = "beispielkonto"
sprache = "en"
zweck = "finanzen"
format = "finanzen"
ohne_titel_mit = ["#ad"]                # optional
```

Formate: `tech`, `gespraech`, `finanzen`, `lernstoff`. Die Meldungsliste schreibt `szene.py`.

Der Tagesbetrieb, Fedi und die Termine:

```toml
[betrieb]
rueckstand = 3
stopp_hoechstens_minuten = 10
temperatur_pause = 83
temperatur_weiter = 70
fehlversuche = 3
bericht_an = "konto"                    # Empfangskonto der Direktnachricht, ohne @
bericht_als_direktnachricht = true

[szene]
aktiv = true
name = "Fedi"

[termine]
aktiv = true
tage = 28
```

## Aufrufe

Aus dem Projektordner:

```
.venv/bin/python tagesbetrieb.py                  ein Lauf über alle Quellen
.venv/bin/python tagesbetrieb.py --danach-aus     danach herunterfahren
.venv/bin/python tagesbetrieb.py --stand          Stufe jeder Folge anzeigen
.venv/bin/python tagesbetrieb.py --neu-auswerten AUSWAHL
                                                  fertige Folgen neu auswerten: alle, Quelle oder Namensteil

.venv/bin/python holen.py QUELLE                  neueste Folgen anzeigen
.venv/bin/python holen.py QUELLE --laden NR       Folge Nr. NR laden
.venv/bin/python transkribieren.py AUDIODATEI     transkribieren (--neu, --sprache de)
.venv/bin/python auswerten.py TRANSKRIPT.json     auswerten und Notiz schreiben
.venv/bin/python szene.py --nur-zeigen            neue Beiträge der beobachteten Konten anzeigen (--tage N)
.venv/bin/python termine.py --nur-zeigen          Termine und Auswahl anzeigen
.venv/bin/python termine.py --an "MUSTER"         eine Terminreihe künftig aufnehmen (--aus: weglassen)
.venv/bin/python bauen.py --vorschau              lokale Vorschau unter http://localhost:1313/
.venv/bin/python bauen.py --hochladen             bauen und hochladen
.venv/bin/python posten.py NOTIZ.md               Hinweis nur anzeigen (--senden, --neu)
.venv/bin/python threads_loeschen.py              anzeigen, was gelöscht würde (--loeschen)
.venv/bin/python pruefen.py TEIL_DES_NAMENS       Notiz auf „geprüft“ setzen (--zurueck)
```

Aliase in `~/.bash_aliases`:

```
lesesaal              Tagesbetrieb im Hintergrund starten, Ausgabe nach DATEN/logs/tagesbetrieb.log
lesesaal-aus          dasselbe, danach herunterfahren
lesesaal-stand        letzte 40 Zeilen des Verlaufs
lesesaal-folgen       Stufe jeder Folge
lesesaal-stopp        an der nächsten sinnvollen Stelle anhalten
lesesaal-feierabend   anhalten und herunterfahren
lesesaal-geprueft     Notiz als geprüft markieren
```

## Arbeitsdaten

```
DATEN/
  audio/<quelle>/           Audio (wird nach der Transkription gelöscht) und .meta.json
  transkripte/<quelle>/     Transkripte als .json und .txt
  auswertungen/<quelle>/    Rohprotokolle jeder Auswertung, mit allen Kandidaten
  szene/                    gesehene Beiträge und funktionierende Feed-Adressen
  termine/                  Auswahl je Terminreihe, bekannte Termine, Termine der nächsten 48 Stunden
  seite/                    gebaute Seite
  berichte/                 Tagesberichte
  logs/tagesbetrieb.log     Verlauf
  stand.json                Stufe jeder Folge
  gepostet.json             gepostete Notizen mit Link zum Thread
```

## Gemessen

- Transkription mit gut 13-facher Echtzeit, Auswertung rund 11 Minuten pro Stunde Audio. Eine Folge von vier Stunden braucht zusammen gut eine Stunde.
- Erster Tageslauf: 14 Folgen aus sechs Podcasts, rund 24 Stunden Audio, in gut sechs Stunden, ohne Fehler und ohne Temperaturpause.
- Terminauswahl: rund 50 neue Terminreihen in zwei Stapeln in zwei Minuten. Danach nur noch neue Reihen.
- Offene Desktop-Programme mit Grafikbeschleunigung (Browser, Office) verdrängen einen Teil des Modells auf den Prozessor. Dann wird die Auswertung etwa dreimal langsamer. Der Tagesbetrieb prüft das über die Schnittstelle von Ollama und warnt im Bericht.

## Lizenz

GPL-3.0, siehe `LICENSE`.
