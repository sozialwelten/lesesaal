"""Ein Transkript mit dem lokalen Sprachmodell auswerten und als Notiz schreiben.

Aufruf:
  .venv/bin/python auswerten.py TRANSKRIPT.json

Ablauf:
  1. Transkript in Abschnitte teilen. Pro Abschnitt sammelt das Modell Kandidaten
     und ordnet jeden ein (Name, Erzählung, Sendungshinweis, Relevanz).
     Gefiltert wird im Code.
  2. Die übrigen Kandidaten führt das Modell mit Nachdenken zusammen und vergibt Hashtags.
  3. Markdown-Notiz nach NOTIZEN/inhalt/notizen/ schreiben.

Die rohen Antworten des Modells landen unter DATEN/auswertungen/<quelle>/.
"""

import argparse
import json
import re
import time
import tomllib
from collections import Counter
from pathlib import Path

import requests

PROJEKT = Path(__file__).resolve().parent
WOERTER_PRO_ABSCHNITT = 1800
KONTEXT = 8192
MIN_RELEVANZ = 3
HOECHSTENS_PRO_FOLGE = 8
DENKEN_HOECHSTENS = 5000
PROFIL_ABSCHNITTE = {"tech": "Tech", "lernstoff": "Lernstoff", "theorie": "Theorie", "finanzen": "Finanzen"}
ARTEN = ["Werkzeug", "Projekt", "Protokoll", "Organisation", "Entwicklung", "Termin", "Sonstiges"]

KANDIDAT = {
    "type": "object",
    "properties": {
        "zeitmarke": {"type": "string"},
        "titel": {"type": "string"},
        "art": {"type": "string", "enum": ARTEN},
        "beschreibung": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "konkreter_name": {"type": "boolean"},
        "erzaehlung": {"type": "boolean"},
        "sendungshinweis": {"type": "boolean"},
        "relevanz": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    "required": [
        "zeitmarke", "titel", "art", "beschreibung", "hashtags",
        "konkreter_name", "erzaehlung", "sendungshinweis", "relevanz",
    ],
}
SCHEMA_ABSCHNITT = {
    "type": "object",
    "properties": {"kandidaten": {"type": "array", "items": KANDIDAT}},
    "required": ["kandidaten"],
}
EINTRAG = {
    "type": "object",
    "properties": {
        "zeitmarke": {"type": "string"},
        "titel": {"type": "string"},
        "art": {"type": "string", "enum": ARTEN},
        "beschreibung": {"type": "string"},
    },
    "required": ["zeitmarke", "titel", "art", "beschreibung"],
}
SCHEMA_ZUSAMMEN = {
    "type": "object",
    "properties": {
        "funde": {"type": "array", "items": EINTRAG},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["funde", "hashtags"],
}

AUFGABE_ABSCHNITT = """Aufgabe: Suche im Transkript-Abschnitt oben nach Kandidaten für ein Tech-Radar und ordne jeden ein.

Kandidaten sind Werkzeuge, Software, Projekte, Protokolle, Organisationen, Entwicklungen und Termine, die im Text erwähnt werden. Teilaspekte desselben Projekts bilden einen einzigen Kandidaten. Höchstens acht Kandidaten. Gibt es keine, gib eine leere Liste zurück.

Fülle für jeden Kandidaten diese Felder ehrlich aus:
- zeitmarke: die Zeitmarke aus dem Text in eckigen Klammern, an der er zuerst vorkommt, im Format hh:mm:ss.
- titel: der Name, höchstens sechs Wörter, ohne Klammern.
- art: eine der vorgegebenen Arten.
- beschreibung: ein bis drei Sätze auf Deutsch, nur was im Text gesagt wird. Kein eigenes Wissen ergänzen.
- hashtags: ein bis drei deutsche Schlagwörter ohne Raute, zusammengeschrieben mit großem Anfangsbuchstaben.
- konkreter_name: true, wenn er einen eigenen Namen hat, etwa eine Software, ein Projekt, ein Protokoll oder eine Organisation. false bei allgemeinen Begriffen wie Datenbank, Knowledge Graph oder Sprachmodell.
- erzaehlung: true, wenn er Teil einer Erzählung, Fiktion oder eines Rollenspiels ist und so in der wirklichen Welt nicht existiert.
- sendungshinweis: true, wenn er ein Hinweis auf die Sendung selbst ist, etwa Livestream, Nachhören oder die Webseite der Sendung.
- relevanz: 1 bis 5, wie gut er zum Interessenprofil passt. 5 heißt sehr gut, 1 heißt gar nicht.
"""

AUFGABE_ZUSAMMEN = """Aufgabe: Oben stehen geprüfte Fundstücke aus den einzelnen Abschnitten einer Folge, jeweils mit einer Relevanz von 1 bis 5. Stelle daraus das endgültige Tech-Radar der Folge zusammen.

- Fasse Einträge zu einem zusammen, wenn sie dasselbe Werkzeug, Projekt oder Thema betreffen, auch bei verschiedenen Titeln. Nimm dann die früheste Zeitmarke.
- titel: der Name, ohne Klammerzusätze.
- beschreibung: höchstens drei Sätze, gebildet aus den Beschreibungen oben. Kein eigenes Wissen ergänzen.
- Behalte höchstens {hoechstens} Einträge, und zwar die mit der höchsten Relevanz.
- Zeitmarken nur aus der Liste oben übernehmen.
- hashtags: drei bis sechs deutsche Schlagwörter für die ganze Folge, ohne Raute, zusammengeschrieben mit großem Anfangsbuchstaben. Keine Namen von Sendung oder Sender.
"""


def zeitmarke(sekunden):
    s = int(sekunden)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def in_sekunden(marke):
    teile = [int(t) for t in marke.split(":")]
    while len(teile) < 3:
        teile.insert(0, 0)
    return teile[0] * 3600 + teile[1] * 60 + teile[2]


def teile_auf(abschnitte):
    gruppen, aktuell, woerter = [], [], 0
    for a in abschnitte:
        n = len(a["text"].split())
        if aktuell and woerter + n > WOERTER_PRO_ABSCHNITT:
            gruppen.append(aktuell)
            aktuell, woerter = [], 0
        aktuell.append(a)
        woerter += n
    if aktuell:
        gruppen.append(aktuell)
    return gruppen


def profil_auszug(text, zweck):
    gesucht = {"Ausrichtung", PROFIL_ABSCHNITTE[zweck], "Grenzfälle"}
    teile = re.split(r"^## ", text, flags=re.M)[1:]
    return "\n\n".join("## " + t.strip() for t in teile if t.split("\n", 1)[0].strip() in gesucht)


def frage_modell(konfiguration, inhalt, schema, denken=False, hoechstens_token=None):
    ollama = konfiguration["ollama"]
    optionen = {"temperature": 0.2}
    if hoechstens_token:
        optionen["num_predict"] = hoechstens_token
    beginn = time.monotonic()
    antwort = requests.post(
        f"{ollama['adresse']}/api/chat",
        json={
            "model": ollama["modell"],
            "messages": [{"role": "user", "content": inhalt}],
            "stream": False,
            "think": denken,
            "format": schema,
            "options": optionen,
            "keep_alive": "10m",
        },
        timeout=3600,
    )
    antwort.raise_for_status()
    daten = antwort.json()
    eingabe, ausgabe = daten.get("prompt_eval_count", 0), daten.get("eval_count", 0)
    statistik = {"eingabe_token": eingabe, "ausgabe_token": ausgabe, "sekunden": round(time.monotonic() - beginn, 1)}
    warnung = "  ACHTUNG: Kontext fast voll" if eingabe + ausgabe > KONTEXT * 0.9 else ""
    return daten["message"].get("content", ""), statistik, warnung


def sauberer_hashtag(wort):
    return re.sub(r"[^\wÄÖÜäöüß]", "", wort.replace("#", "").strip())


def bericht(statistik, warnung):
    return f"{statistik['eingabe_token']}+{statistik['ausgabe_token']} Token, {statistik['sekunden']:.0f} s{warnung}"


def schreibe_notiz(pfad, meta, funde, hashtags, sprung):
    j, m, t = meta["datum"].split("-")
    zeilen = [
        "---",
        f"title: {json.dumps(meta['titel'], ensure_ascii=False)}",
        f"date: {meta['datum']}",
        f"quelle: {json.dumps(meta['quelle_name'], ensure_ascii=False)}",
        f"zweck: {json.dumps(meta['zweck'])}",
        f"tags: {json.dumps(hashtags, ensure_ascii=False)}",
        'status: "ungeprüft"',
        f"folgenseite: {json.dumps(meta['seite'])}",
        "---",
        "",
        f"Tech-Radar zur Folge [{meta['titel']}]({meta['seite']}) von {meta['quelle_name']} vom {t}.{m}.{j}.",
        "",
    ]
    for f in funde:
        marke = f["zeitmarke"]
        if sprung and meta.get("seite"):
            marke = f"[{marke}]({meta['seite']}{sprung.format(zeit=marke)})"
        zeilen.append(f"- **{f['titel']}** ({f['art']}, {marke}): {f['beschreibung']}")
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Transkript auswerten und Notiz schreiben")
    parser.add_argument("transkript", type=Path, help="JSON-Datei unter DATEN/transkripte/<quelle>/")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)

    transkript = json.loads(args.transkript.read_text(encoding="utf-8"))
    audio = Path(transkript["audio"])
    meta = json.loads(audio.with_name(audio.stem + ".meta.json").read_text(encoding="utf-8"))
    zweck = meta["zweck"]
    if zweck != "tech":
        raise SystemExit(f"Für den Zweck '{zweck}' gibt es noch kein Format.")
    quelle = next(q for q in konfiguration["quelle"] if q["kennung"] == meta["quelle"])
    hinweise = f"Hinweise zu dieser Quelle: {quelle['hinweise']}\n\n" if quelle.get("hinweise") else ""

    notizen = Path(konfiguration["pfade"]["notizen"])
    profil = profil_auszug((notizen / "interessenprofil.md").read_text(encoding="utf-8"), zweck)

    gruppen = teile_auf(transkript["abschnitte"])
    print(f"{meta['titel']}: {len(gruppen)} Abschnitte, Modell {konfiguration['ollama']['modell']}")

    behalten, protokoll = [], []
    for nr, gruppe in enumerate(gruppen, start=1):
        von, bis = gruppe[0]["start"], gruppe[-1]["ende"]
        text = "\n".join(f"[{zeitmarke(a['start'])}] {a['text']}" for a in gruppe)
        inhalt = (
            f"Transkript-Abschnitt aus der Folge „{meta['titel']}“ von {meta['quelle_name']}:\n\n"
            f"{text}\n\n---\n\nInteressenprofil:\n\n{profil}\n\n---\n\n{hinweise}{AUFGABE_ABSCHNITT}"
        )
        roh, statistik, warnung = frage_modell(konfiguration, inhalt, SCHEMA_ABSCHNITT)
        try:
            kandidaten = json.loads(roh).get("kandidaten", [])
        except json.JSONDecodeError:
            print(f"  Abschnitt {nr}: Antwort nicht lesbar, übersprungen")
            protokoll.append({"schritt": f"abschnitt {nr}", "roh": roh, **statistik})
            continue

        gruende = Counter()
        for k in kandidaten:
            try:
                sek = in_sekunden(k["zeitmarke"])
            except (ValueError, KeyError):
                gruende["Zeitmarke"] += 1
                continue
            if not von - 5 <= sek <= bis + 5:
                gruende["Zeitmarke"] += 1
            elif not k.get("konkreter_name"):
                gruende["kein Name"] += 1
            elif k.get("erzaehlung"):
                gruende["Erzählung"] += 1
            elif k.get("sendungshinweis"):
                gruende["Sendung"] += 1
            elif k.get("relevanz", 0) < MIN_RELEVANZ:
                gruende["Relevanz"] += 1
            else:
                k["zeitmarke"] = zeitmarke(sek)
                behalten.append(k)
        weg = ", ".join(f"{n} {g}" for g, n in gruende.items()) or "keine"
        print(
            f"  Abschnitt {nr}/{len(gruppen)} ({zeitmarke(von)} bis {zeitmarke(bis)}): "
            f"{len(kandidaten)} Kandidaten, aussortiert: {weg}; {bericht(statistik, warnung)}"
        )
        protokoll.append({"schritt": f"abschnitt {nr}", "von": von, "bis": bis, "kandidaten": kandidaten, **statistik})

    funde, hashtags = [], []
    if behalten:
        liste = "\n".join(
            f"- [{k['zeitmarke']}] {k['titel']} ({k['art']}, Relevanz {k['relevanz']}): {k['beschreibung']}"
            for k in behalten
        )
        inhalt = (
            f"Fundstücke aus der Folge „{meta['titel']}“ von {meta['quelle_name']}:\n\n"
            f"{liste}\n\n---\n\nInteressenprofil:\n\n{profil}\n\n---\n\n{hinweise}"
            f"{AUFGABE_ZUSAMMEN.format(hoechstens=HOECHSTENS_PRO_FOLGE)}"
        )
        roh, statistik, warnung = frage_modell(
            konfiguration, inhalt, SCHEMA_ZUSAMMEN, denken=True, hoechstens_token=DENKEN_HOECHSTENS
        )
        erlaubt = {k["zeitmarke"] for k in behalten}
        try:
            antwort = json.loads(roh)
            funde = [f for f in antwort.get("funde", []) if f.get("zeitmarke") in erlaubt]
            hashtags = antwort.get("hashtags", [])
            art = "mit Nachdenken"
        except json.JSONDecodeError:
            beste = sorted(behalten, key=lambda k: -k["relevanz"])[:HOECHSTENS_PRO_FOLGE]
            funde = [{key: k[key] for key in ("zeitmarke", "titel", "art", "beschreibung")} for k in beste]
            zaehler = Counter(sauberer_hashtag(h) for k in beste for h in k["hashtags"])
            hashtags = [h for h, _ in zaehler.most_common(6) if h]
            art = "ERSATZWEISE nach Relevanz, Antwort nicht lesbar"
        funde.sort(key=lambda f: in_sekunden(f["zeitmarke"]))
        print(f"  Zusammenführen {art}: {len(behalten)} zu {len(funde)} Fundstücken; {bericht(statistik, warnung)}")
        protokoll.append({"schritt": "zusammenführen", "roh": roh, **statistik})

    roh_pfad = Path(konfiguration["pfade"]["daten"]) / "auswertungen" / meta["quelle"] / f"{audio.stem}.json"
    roh_pfad.parent.mkdir(parents=True, exist_ok=True)
    roh_pfad.write_text(json.dumps(protokoll, ensure_ascii=False, indent=1), encoding="utf-8")

    if not funde:
        print("Nichts Relevantes gefunden, keine Notiz geschrieben.")
        return

    zweck_tag = PROFIL_ABSCHNITTE[zweck]
    tags = [zweck_tag]
    for h in hashtags:
        h = sauberer_hashtag(h)
        if h and h not in tags:
            tags.append(h)

    ziel = notizen / "inhalt" / "notizen" / f"{audio.stem}.md"
    schreibe_notiz(ziel, meta, funde, tags, quelle.get("sprunglink"))
    print(f"{len(funde)} Fundstücke, Notiz: {ziel}")


if __name__ == "__main__":
    main()
