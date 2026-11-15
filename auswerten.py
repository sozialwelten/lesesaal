"""Ein Transkript mit dem lokalen Sprachmodell auswerten und als Notiz schreiben.

Aufruf:
  .venv/bin/python auswerten.py TRANSKRIPT.json

Ablauf:
  1. Format wählen: Regeln der Quelle in config.toml nach Folgentitel, sonst Format der Quelle.
  2. Transkript in Abschnitte teilen, Zeitmarken höchstens alle 30 Sekunden. Pro Abschnitt
     sammelt das Modell Einträge in getrennten Listen (etwa Thesen und Hinweise) und ordnet
     jeden ein. Gefiltert wird im Code. Läuft ein Abschnitt fast über den Kontext, wird er
     geteilt und neu ausgewertet.
  3. Die relevantesten Einträge jeder Liste führt das Modell mit Nachdenken zusammen
     und vergibt Hashtags.
  4. Markdown-Notiz nach NOTIZEN/inhalt/notizen/ schreiben, gegliedert in Teile.

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
SEKUNDEN_PRO_ZEITMARKE = 30
KONTEXT = 8192
GRENZE = 0.9
MIN_RELEVANZ = 3
MIN_RELEVANZ_THESEN = 4
ZUM_ZUSAMMENFUEHREN_PRO_LISTE = 15
DENKEN_HOECHSTENS = 4000
PROFIL_ABSCHNITTE = {"tech": "Tech", "lernstoff": "Lernstoff", "theorie": "Theorie", "finanzen": "Finanzen"}

FORMATE = {
    "tech": {
        "name": "Tech-Radar",
        "profil": ["Tech"],
        "listen": {
            "hinweise": {
                "arten": ["Werkzeug", "Projekt", "Protokoll", "Organisation", "Initiative",
                          "Entwicklung", "Veranstaltung", "Termin", "Literatur"],
                "was": "Werkzeuge, Software, Projekte, Protokolle, Organisationen, Initiativen, "
                       "Entwicklungen, Veranstaltungen, Termine und Literatur, die im Text erwähnt werden",
                "pro_abschnitt": 8,
                "hoechstens": 8,
            },
        },
    },
    "gespraech": {
        "name": "Gesprächsnotiz",
        "profil": ["Tech", "Theorie"],
        "listen": {
            "thesen": {
                "arten": ["These", "Begriff"],
                "was": "Thesen und Begriffe, die im Gespräch vertreten oder erklärt werden",
                "pro_abschnitt": 3,
                "hoechstens": 6,
            },
            "hinweise": {
                "arten": ["Projekt", "Initiative", "Organisation", "Veranstaltung", "Termin",
                          "Werkzeug", "Literatur"],
                "was": "Projekte, Initiativen, Organisationen, Veranstaltungen, Termine, Werkzeuge "
                       "und Literatur, die genannt werden, auch wenn sie nur nebenbei fallen",
                "pro_abschnitt": 4,
                "hoechstens": 8,
            },
        },
    },
}
OHNE_NAMEN = {"These", "Begriff"}
REIHENFOLGE = ["Thesen und Begriffe", "Hinweise", "Literatur"]

FELDER = """Fülle für jeden Eintrag diese Felder ehrlich aus:
- zeitmarke: die Zeitmarke aus dem Text in eckigen Klammern, in deren Zeile er zuerst vorkommt, im Format hh:mm:ss.
- titel: der Name, bei Thesen die These in wenigen Worten. Höchstens acht Wörter, ohne Klammern.
- art: eine der Arten, die für die Liste vorgegeben sind. Bücher, Aufsätze und Studien haben immer die Art Literatur.
- beschreibung: ein bis drei Sätze auf Deutsch, nur was im Text gesagt wird. Bei Terminen und Veranstaltungen Datum und Ort, wenn genannt. Kein eigenes Wissen ergänzen.
- hashtags: ein bis drei deutsche Schlagwörter ohne Raute, zusammengeschrieben mit großem Anfangsbuchstaben.
- konkreter_name: true, wenn er einen eigenen Namen hat, etwa eine Software, ein Projekt, eine Initiative, eine Organisation oder ein Buch. false bei allgemeinen Begriffen wie Datenbank, Knowledge Graph oder Sprachmodell. Bei Thesen und Begriffen true.
- erzaehlung: true, wenn er Teil einer Erzählung, Fiktion oder eines Rollenspiels ist und so in der wirklichen Welt nicht existiert.
- sendungshinweis: true, wenn er ein Hinweis auf die Sendung selbst ist, etwa Livestream, Nachhören, Spendenaufruf oder die Webseite der Sendung.
- relevanz: 1 bis 5, wie gut er zum Interessenprofil passt. 5 heißt sehr gut, 1 heißt gar nicht.
"""


def listen_beschreibung(fmt, feld):
    zeilen = []
    for name, liste in fmt["listen"].items():
        zeilen.append(
            f"- {name}: {liste['was']}. Höchstens {liste[feld]}, die relevantesten. "
            f"Arten: {', '.join(liste['arten'])}."
        )
    return "\n".join(zeilen)


def aufgabe_abschnitt(fmt):
    return (
        f"Aufgabe: Suche im Transkript-Abschnitt oben nach Einträgen für eine {fmt['name']} "
        f"und ordne jeden ein. Sammle sie in diesen Listen:\n\n"
        f"{listen_beschreibung(fmt, 'pro_abschnitt')}\n\n"
        "Teilaspekte desselben Projekts oder derselben These bilden einen einzigen Eintrag. "
        "Gibt es für eine Liste nichts, bleibt sie leer.\n\n" + FELDER
    )


def aufgabe_zusammen(fmt):
    return (
        f"Aufgabe: Oben stehen geprüfte Einträge aus den einzelnen Abschnitten einer Folge, nach Listen getrennt "
        f"und jeweils mit einer Relevanz von 1 bis 5. Stelle daraus die endgültige {fmt['name']} der Folge "
        f"zusammen, in denselben Listen:\n\n{listen_beschreibung(fmt, 'hoechstens')}\n\n"
        "- Fasse Einträge innerhalb einer Liste zusammen, wenn sie dasselbe Projekt, dieselbe Initiative, "
        "dieselbe These oder dasselbe Thema betreffen, auch bei verschiedenen Titeln. Nimm dann die früheste Zeitmarke.\n"
        "- art aus den Einträgen oben übernehmen.\n"
        "- titel: ohne Klammerzusätze.\n"
        "- beschreibung: höchstens drei Sätze, gebildet aus den Beschreibungen oben. Kein eigenes Wissen ergänzen.\n"
        "- Zeitmarken nur aus der Liste oben übernehmen.\n"
        "- hashtags: drei bis sechs deutsche Schlagwörter für die ganze Folge, ohne Raute, zusammengeschrieben "
        "mit großem Anfangsbuchstaben. Keine Namen von Sendung oder Sender.\n"
    )


def schema_abschnitt(fmt):
    eigenschaften = {}
    for name, liste in fmt["listen"].items():
        eintrag = {
            "type": "object",
            "properties": {
                "zeitmarke": {"type": "string"},
                "titel": {"type": "string"},
                "art": {"type": "string", "enum": liste["arten"]},
                "beschreibung": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "konkreter_name": {"type": "boolean"},
                "erzaehlung": {"type": "boolean"},
                "sendungshinweis": {"type": "boolean"},
                "relevanz": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["zeitmarke", "titel", "art", "beschreibung", "hashtags",
                         "konkreter_name", "erzaehlung", "sendungshinweis", "relevanz"],
        }
        eigenschaften[name] = {"type": "array", "items": eintrag}
    return {"type": "object", "properties": eigenschaften, "required": list(eigenschaften)}


def schema_zusammen(fmt):
    eigenschaften = {}
    for name, liste in fmt["listen"].items():
        eintrag = {
            "type": "object",
            "properties": {
                "zeitmarke": {"type": "string"},
                "titel": {"type": "string"},
                "art": {"type": "string", "enum": liste["arten"]},
                "beschreibung": {"type": "string"},
            },
            "required": ["zeitmarke", "titel", "art", "beschreibung"],
        }
        eigenschaften[name] = {"type": "array", "items": eintrag}
    eigenschaften["hashtags"] = {"type": "array", "items": {"type": "string"}}
    return {"type": "object", "properties": eigenschaften, "required": list(eigenschaften)}


def teil_von(art):
    if art in OHNE_NAMEN:
        return "Thesen und Begriffe"
    if art == "Literatur":
        return "Literatur"
    return "Hinweise"


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


def als_text(gruppe):
    """Verbindet Abschnitte zu Zeilen, eine Zeitmarke höchstens alle 30 Sekunden."""
    zeilen, aktuell, beginn = [], [], None
    for a in gruppe:
        if beginn is None or a["start"] - beginn >= SEKUNDEN_PRO_ZEITMARKE:
            if aktuell:
                zeilen.append(f"[{zeitmarke(beginn)}] " + " ".join(aktuell))
            aktuell, beginn = [], a["start"]
        aktuell.append(a["text"])
    if aktuell:
        zeilen.append(f"[{zeitmarke(beginn)}] " + " ".join(aktuell))
    return "\n".join(zeilen)


def profil_auszug(text, abschnitte):
    gesucht = {"Ausrichtung", "Grenzfälle", *abschnitte}
    teile = re.split(r"^## ", text, flags=re.M)[1:]
    return "\n\n".join("## " + t.strip() for t in teile if t.split("\n", 1)[0].strip() in gesucht)


def waehle_format(quelle, titel):
    for regel in quelle.get("formate", []):
        if regel["muster"].lower() in titel.lower():
            return regel["format"]
    return quelle.get("format", "tech")


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
    return daten["message"].get("content", ""), statistik


def zu_voll(statistik):
    return statistik["eingabe_token"] + statistik["ausgabe_token"] > KONTEXT * GRENZE


def bericht(statistik):
    warnung = "  ACHTUNG: Kontext fast voll" if zu_voll(statistik) else ""
    return f"{statistik['eingabe_token']}+{statistik['ausgabe_token']} Token, {statistik['sekunden']:.0f} s{warnung}"


def sauberer_hashtag(wort):
    return re.sub(r"[^\wÄÖÜäöüß]", "", wort.replace("#", "").strip())


def erster_satz(text):
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0][:220]


def schreibe_notiz(pfad, meta, fmt_kennung, funde, hashtags, sprung):
    fmt = FORMATE[fmt_kennung]
    j, m, t = meta["datum"].split("-")
    zeilen = [
        "---",
        f"title: {json.dumps(meta['titel'], ensure_ascii=False)}",
        f"date: {meta['datum']}",
        f"quelle: {json.dumps(meta['quelle_name'], ensure_ascii=False)}",
        f"zweck: {json.dumps(meta['zweck'])}",
        f"format: {json.dumps(fmt_kennung)}",
        f"tags: {json.dumps(hashtags, ensure_ascii=False)}",
        'status: "ungeprüft"',
        f"folgenseite: {json.dumps(meta['seite'])}",
        "---",
        "",
        f"{fmt['name']} zur Folge [{meta['titel']}]({meta['seite']}) von {meta['quelle_name']} vom {t}.{m}.{j}.",
    ]
    for teil in REIHENFOLGE:
        eintraege = [f for f in funde if teil_von(f["art"]) == teil]
        if not eintraege:
            continue
        zeilen += ["", f"## {teil}", ""]
        for f in eintraege:
            marke = f["zeitmarke"]
            if sprung and meta.get("seite"):
                marke = f"[{marke}]({meta['seite']}{sprung.format(zeit=marke)})"
            zeilen.append(f"- **{f['titel']}** ({f['art']}, {marke}): {f['beschreibung']}")
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")


def pruefe(k, von, bis, gruende):
    """Gibt den Eintrag mit bereinigter Zeitmarke zurück oder None, wenn er herausfällt."""
    try:
        sek = in_sekunden(k["zeitmarke"])
    except (ValueError, KeyError):
        gruende["Zeitmarke"] += 1
        return None
    schwelle = MIN_RELEVANZ_THESEN if k.get("art") in OHNE_NAMEN else MIN_RELEVANZ
    if not von - 5 <= sek <= bis + 5:
        gruende["Zeitmarke"] += 1
    elif k.get("art") not in OHNE_NAMEN and not k.get("konkreter_name"):
        gruende["kein Name"] += 1
    elif k.get("erzaehlung"):
        gruende["Erzählung"] += 1
    elif k.get("sendungshinweis"):
        gruende["Sendung"] += 1
    elif k.get("relevanz", 0) < schwelle:
        gruende["Relevanz"] += 1
    else:
        k["zeitmarke"] = zeitmarke(sek)
        return k
    return None


def main():
    parser = argparse.ArgumentParser(description="Transkript auswerten und Notiz schreiben")
    parser.add_argument("transkript", type=Path, help="JSON-Datei unter DATEN/transkripte/<quelle>/")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)

    transkript = json.loads(args.transkript.read_text(encoding="utf-8"))
    audio = Path(transkript["audio"])
    meta = json.loads(audio.with_name(audio.stem + ".meta.json").read_text(encoding="utf-8"))
    quelle = next(q for q in konfiguration["quelle"] if q["kennung"] == meta["quelle"])
    fmt_kennung = waehle_format(quelle, meta["titel"])
    fmt = FORMATE[fmt_kennung]
    hinweise = f"Hinweise zu dieser Quelle: {quelle['hinweise']}\n\n" if quelle.get("hinweise") else ""

    notizen = Path(konfiguration["pfade"]["notizen"])
    profil = profil_auszug((notizen / "interessenprofil.md").read_text(encoding="utf-8"), fmt["profil"])

    warteschlange = teile_auf(transkript["abschnitte"])
    print(f"{meta['titel']}: {fmt['name']}, {len(warteschlange)} Abschnitte, Modell {konfiguration['ollama']['modell']}")

    aufgabe = aufgabe_abschnitt(fmt)
    behalten = {name: [] for name in fmt["listen"]}
    protokoll, nr = [], 0
    while warteschlange:
        gruppe = warteschlange.pop(0)
        von, bis = gruppe[0]["start"], gruppe[-1]["ende"]
        inhalt = (
            f"Transkript-Abschnitt aus der Folge „{meta['titel']}“ von {meta['quelle_name']}:\n\n"
            f"{als_text(gruppe)}\n\n---\n\nInteressenprofil:\n\n{profil}\n\n---\n\n{hinweise}{aufgabe}"
        )
        roh, statistik = frage_modell(konfiguration, inhalt, schema_abschnitt(fmt))
        if zu_voll(statistik) and len(gruppe) > 1:
            mitte = len(gruppe) // 2
            warteschlange[:0] = [gruppe[:mitte], gruppe[mitte:]]
            print(f"  Abschnitt {zeitmarke(von)} bis {zeitmarke(bis)} zu lang ({bericht(statistik)}), wird geteilt")
            continue
        nr += 1
        try:
            antwort = json.loads(roh)
        except json.JSONDecodeError:
            print(f"  Abschnitt {nr}: Antwort nicht lesbar, übersprungen")
            protokoll.append({"schritt": f"abschnitt {nr}", "roh": roh, **statistik})
            continue

        gruende, anzahl = Counter(), Counter()
        for name in fmt["listen"]:
            for k in antwort.get(name, []):
                anzahl[name] += 1
                if pruefe(k, von, bis, gruende):
                    behalten[name].append(k)
        gefunden = ", ".join(f"{anzahl[n]} {n}" for n in fmt["listen"])
        weg = ", ".join(f"{n} {g}" for g, n in gruende.items()) or "keine"
        print(f"  Abschnitt {nr} ({zeitmarke(von)} bis {zeitmarke(bis)}): {gefunden}, aussortiert: {weg}; {bericht(statistik)}")
        protokoll.append({"schritt": f"abschnitt {nr}", "von": von, "bis": bis, "antwort": antwort, **statistik})

    funde, hashtags = [], []
    if any(behalten.values()):
        bloecke, erlaubt, eingereicht = [], set(), {}
        for name in fmt["listen"]:
            auswahl = sorted(behalten[name], key=lambda k: -k["relevanz"])[:ZUM_ZUSAMMENFUEHREN_PRO_LISTE]
            auswahl.sort(key=lambda k: in_sekunden(k["zeitmarke"]))
            eingereicht[name] = auswahl
            erlaubt |= {k["zeitmarke"] for k in auswahl}
            zeilen = "\n".join(
                f"- [{k['zeitmarke']}] {k['titel']} ({k['art']}, Relevanz {k['relevanz']}): {erster_satz(k['beschreibung'])}"
                for k in auswahl
            ) or "(leer)"
            bloecke.append(f"Liste {name}:\n{zeilen}")
        inhalt = (
            f"Einträge aus der Folge „{meta['titel']}“ von {meta['quelle_name']}:\n\n"
            + "\n\n".join(bloecke)
            + f"\n\n---\n\nInteressenprofil:\n\n{profil}\n\n---\n\n{hinweise}{aufgabe_zusammen(fmt)}"
        )
        roh, statistik = frage_modell(konfiguration, inhalt, schema_zusammen(fmt), denken=True, hoechstens_token=DENKEN_HOECHSTENS)
        try:
            antwort = json.loads(roh)
            for name, liste in fmt["listen"].items():
                funde += [f for f in antwort.get(name, []) if f.get("zeitmarke") in erlaubt][: liste["hoechstens"]]
            hashtags = antwort.get("hashtags", [])
            art = "mit Nachdenken"
        except json.JSONDecodeError:
            zaehler = Counter()
            for name, liste in fmt["listen"].items():
                beste = sorted(eingereicht[name], key=lambda k: -k["relevanz"])[: liste["hoechstens"]]
                funde += [{key: k[key] for key in ("zeitmarke", "titel", "art", "beschreibung")} for k in beste]
                zaehler.update(sauberer_hashtag(h) for k in beste for h in k["hashtags"])
            hashtags = [h for h, _ in zaehler.most_common(6) if h]
            art = "ERSATZWEISE nach Relevanz, Antwort nicht lesbar"
        funde.sort(key=lambda f: in_sekunden(f["zeitmarke"]))
        uebersicht = ", ".join(f"{len(behalten[n])} {n}" for n in fmt["listen"])
        print(f"  Zusammenführen {art}: aus {uebersicht} wurden {len(funde)} Einträge; {bericht(statistik)}")
        protokoll.append({"schritt": "zusammenführen", "roh": roh, **statistik})

    roh_pfad = Path(konfiguration["pfade"]["daten"]) / "auswertungen" / meta["quelle"] / f"{audio.stem}.json"
    roh_pfad.parent.mkdir(parents=True, exist_ok=True)
    roh_pfad.write_text(json.dumps(protokoll, ensure_ascii=False, indent=1), encoding="utf-8")

    if not funde:
        print("Nichts Relevantes gefunden, keine Notiz geschrieben.")
        return

    tags = [PROFIL_ABSCHNITTE[meta["zweck"]]]
    for h in hashtags:
        h = sauberer_hashtag(h)
        if h and h not in tags:
            tags.append(h)

    ziel = notizen / "inhalt" / "notizen" / f"{audio.stem}.md"
    schreibe_notiz(ziel, meta, fmt_kennung, funde, tags, quelle.get("sprunglink"))
    print(f"{len(funde)} Einträge, Notiz: {ziel}")


if __name__ == "__main__":
    main()
