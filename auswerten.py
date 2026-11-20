"""Ein Transkript mit dem lokalen Sprachmodell auswerten und als Notiz schreiben.

Aufruf:
  .venv/bin/python auswerten.py TRANSKRIPT.json

Ablauf:
  1. Format wählen: Regeln der Quelle in config.toml nach Folgentitel, sonst Format der Quelle.
  2. Transkript in Abschnitte teilen, Zeitmarken höchstens alle 30 Sekunden. Pro Abschnitt
     sammelt das Modell Einträge in getrennten Listen (etwa Thesen und Hinweise) und ordnet
     jeden ein. Zu jedem Eintrag nennt es eine wörtliche Belegstelle. Der Code sucht sie im
     Transkript. Fehlt sie oder steht kein Wort des Titels in ihrer Nähe, fällt der Eintrag weg.
     Die Zeitmarke kommt aus der gefundenen Stelle. Gefiltert wird im Code. Läuft ein Abschnitt
     fast über den Kontext, wird er geteilt und neu ausgewertet.
  3. Die relevantesten Einträge jeder Liste führt das Modell mit Nachdenken zusammen.
  4. Hashtags vergibt ein eigener Durchgang, der nur die fertigen Einträge sieht und nicht das
     Interessenprofil. Danach angleichen: Der Code übernimmt die Schreibweise vorhandener Hashtags, wenn sie
     sich nur in Groß- und Kleinschreibung oder Umlauten unterscheiden. Für die übrigen fragt
     ein kurzer Durchgang ohne Nachdenken, ob ein vorhandener Hashtag dasselbe meint, etwa KI
     und KuenstlicheIntelligenz. Nur dann wird ersetzt. Neue Hashtags bleiben erlaubt.
  5. Markdown-Notiz nach NOTIZEN/inhalt/notizen/ schreiben, gegliedert in Teile.

Die rohen Antworten des Modells landen unter DATEN/auswertungen/<quelle>/.

Mit --stoppdatei prüft das Skript vor jedem Abschnitt, ob das Stoppsignal gesetzt ist. Dann
schätzt es die restliche Dauer. Passt sie in --hoechstens Minuten, macht es fertig, sonst
bricht es mit Exit-Code 3 ab, ohne Notiz. So nutzt es tagesbetrieb.py.
"""

import argparse
import json
import sys
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
SEKUNDEN_PRO_ABSCHNITT = 150  # Schätzung, solange noch kein Abschnitt gemessen ist
SEKUNDEN_ZUSAMMENFUEHREN = 150
ABGEBROCHEN = 3
BELEG_ANTEIL = 0.6
BELEG_UMFELD_SEKUNDEN = 60
VORHANDENE_HOECHSTENS = 300  # so viele vorhandene Hashtags, die häufigsten zuerst, gehen in den Abgleich
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
                "hoechstens": 8,
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
FORMATE["finanzen"] = {
    "name": "Finanznotiz",
    "profil": ["Finanzen"],
    "listen": {
        "thesen": {
            "arten": ["These", "Begriff"],
            "was": "Kernaussagen, Thesen und Fachbegriffe zu Märkten, Geld, Unternehmen, Wirtschaft und "
                   "Steuern, die erklärt oder vertreten werden, mit dem historischen Zusammenhang, wenn er genannt wird",
            "pro_abschnitt": 4,
            "hoechstens": 8,
        },
        "hinweise": {
            "arten": ["Unternehmen", "Institution", "Organisation", "Ereignis", "Projekt", "Literatur"],
            "was": "Unternehmen, Institutionen wie Zentralbanken oder Behörden, Organisationen, benannte Ereignisse, "
                   "Projekte und Literatur, die genannt werden",
            "pro_abschnitt": 4,
            "hoechstens": 8,
        },
    },
}
FORMATE["lernstoff"] = {
    "name": "Lernnotiz",
    "profil": ["Lernstoff"],
    "listen": {
        "thesen": {
            "arten": ["Begriff", "Regel", "Methode"],
            "was": "Fachbegriffe, Regeln und Verfahren aus Buchhaltung, Bilanzierung, Steuern und Recht, "
                   "die in der Folge erklärt werden, so beschrieben, dass sie ohne die Folge verständlich sind",
            "pro_abschnitt": 4,
            "hoechstens": 10,
        },
        "hinweise": {
            "arten": ["Gesetz", "Standard", "Literatur", "Werkzeug", "Organisation"],
            "was": "Gesetze und Paragraphen, Rechnungslegungsstandards, Literatur, Werkzeuge und "
                   "Organisationen, die genannt werden",
            "pro_abschnitt": 4,
            "hoechstens": 8,
        },
    },
}
OHNE_NAMEN = {"These", "Begriff", "Regel", "Methode"}
REIHENFOLGE = ["Thesen und Begriffe", "Hinweise", "Literatur"]

FELDER = """Fülle für jeden Eintrag diese Felder ehrlich aus:
- zeitmarke: die Zeitmarke aus dem Text in eckigen Klammern, in deren Zeile er zuerst vorkommt, im Format hh:mm:ss.
- titel: der Name, bei Thesen die These in wenigen Worten. Höchstens acht Wörter, ohne Klammern.
- art: eine der Arten, die für die Liste vorgegeben sind. Bücher, Aufsätze und Studien haben immer die Art Literatur.
- beschreibung: ein bis drei Sätze auf Deutsch, nur was im Text gesagt wird. Bei Terminen und Veranstaltungen Datum und Ort, wenn genannt. Kein eigenes Wissen ergänzen.
- beleg: ein wörtliches Stück aus dem Text, drei bis acht Wörter, genau so, wie es im Transkript steht, an der Stelle, an der der Eintrag vorkommt. Nicht umformulieren, nicht übersetzen.
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
                "beleg": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "konkreter_name": {"type": "boolean"},
                "erzaehlung": {"type": "boolean"},
                "sendungshinweis": {"type": "boolean"},
                "relevanz": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["zeitmarke", "titel", "art", "beschreibung", "beleg", "hashtags",
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


def einheitliche_marke(marke):
    """Bringt eine Zeitmarke vom Modell in die Form hh:mm:ss, etwa „[1:00:25]“ zu „01:00:25“."""
    try:
        return zeitmarke(in_sekunden(re.sub(r"[^\d:]", "", str(marke))))
    except ValueError:
        return ""


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


def vergleichsform(wort):
    """Schreibweise für den Vergleich: klein, Umlaute ausgeschrieben."""
    wort = wort.lower()
    for alt, neu in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        wort = wort.replace(alt, neu)
    return wort


def vorhandene_hashtags(ordner, ausser):
    """Hashtags aus dem Kopfbereich aller Notizen, nach Häufigkeit. Die Notiz `ausser` zählt nicht mit."""
    zaehler, schreibweise = Counter(), {}
    for datei in sorted(ordner.glob("*.md")):
        if datei.stem == ausser:
            continue
        for zeile in datei.read_text(encoding="utf-8").splitlines()[1:]:
            if zeile == "---":
                break
            if zeile.startswith("tags:"):
                try:
                    tags = json.loads(zeile[5:])
                except json.JSONDecodeError:
                    tags = []
                for tag in tags:
                    tag = sauberer_hashtag(str(tag))
                    if tag:
                        schluessel = vergleichsform(tag)
                        zaehler[schluessel] += 1
                        schreibweise.setdefault(schluessel, tag)
                break
    return [schreibweise[s] for s, _ in sorted(zaehler.items(), key=lambda e: (-e[1], e[0]))]


def gleiche_an(konfiguration, kandidaten, vorrat):
    """Fragt das Modell, welcher Kandidat dasselbe meint wie ein vorhandener Hashtag.

    Gibt ein Wörterbuch Kandidat -> vorhandener Hashtag zurück, nur für echte Entsprechungen."""
    if not kandidaten or not vorrat:
        return {}, None
    inhalt = (
        f"Vorhandene Hashtags: {', '.join(vorrat)}\n\n"
        f"Neue Hashtags: {', '.join(kandidaten)}\n\n"
        "Aufgabe: Prüfe für jeden neuen Hashtag, ob einer der vorhandenen Hashtags genau dasselbe meint. "
        "Das ist der Fall bei einer anderen Schreibweise, einer Abkürzung, Einzahl und Mehrzahl, "
        "einer Übersetzung oder einem gleichbedeutenden Wort, etwa KI und KuenstlicheIntelligenz "
        "oder Vektordatenbank und VectorDatabase. Ein nur thematisch verwandter, übergeordneter oder "
        "untergeordneter Hashtag zählt nicht. Im Zweifel: keiner.\n"
        "Gib für jeden neuen Hashtag genau einen Eintrag zurück: hashtag ist der neue Hashtag, "
        "entspricht ist der vorhandene Hashtag mit derselben Bedeutung oder keiner."
    )
    schema = {
        "type": "object",
        "properties": {
            "zuordnung": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "hashtag": {"type": "string", "enum": kandidaten},
                        "entspricht": {"type": "string", "enum": vorrat + ["keiner"]},
                    },
                    "required": ["hashtag", "entspricht"],
                },
            },
        },
        "required": ["zuordnung"],
    }
    roh, statistik = frage_modell(konfiguration, inhalt, schema)
    try:
        zuordnung = json.loads(roh).get("zuordnung", [])
    except json.JSONDecodeError:
        return {}, {"schritt": "hashtags angleichen", "roh": roh, **statistik}
    ergebnis = {
        z["hashtag"]: z["entspricht"] for z in zuordnung
        if z.get("hashtag") in kandidaten and z.get("entspricht") in vorrat
    }
    return ergebnis, {"schritt": "hashtags angleichen", "antwort": zuordnung, **statistik}


def vergib_hashtags(konfiguration, meta, funde):
    """Hashtags nur aus den fertigen Einträgen, ohne Interessenprofil."""
    eintraege = "\n".join(f"- {f['titel']} ({f['art']}): {f['beschreibung']}" for f in funde)
    inhalt = (
        f"Einträge einer Notiz zur Folge „{meta['titel']}“:\n\n{eintraege}\n\n---\n\n"
        "Aufgabe: Vergib drei bis sechs Hashtags, die die Themen dieser Einträge beschreiben. "
        "Nur Themen, die in den Einträgen tatsächlich vorkommen, keine allgemeinen Oberbegriffe, die nicht "
        "darin stehen. Deutsche Schlagwörter ohne Raute, zusammengeschrieben mit großem Anfangsbuchstaben. "
        "Eigennamen wie Palantir oder Mastodon dürfen Hashtags sein. Keine Namen von Sendung oder Sender."
    )
    schema = {"type": "object", "properties": {"hashtags": {"type": "array", "items": {"type": "string"}}},
              "required": ["hashtags"]}
    roh, statistik = frage_modell(konfiguration, inhalt, schema)
    try:
        hashtags = [str(h) for h in json.loads(roh).get("hashtags", [])][:6]
    except json.JSONDecodeError:
        hashtags = []
    return hashtags, {"schritt": "hashtags vergeben", "roh": roh, **statistik}


def erster_satz(text):
    return re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0][:220]


def schreibe_notiz(pfad, meta, fmt_kennung, funde, hashtags, sprung):
    fmt = FORMATE[fmt_kennung]
    j, m, t = meta["datum"].split("-")
    zeilen = [
        "---",
        f"title: {json.dumps(meta['titel'], ensure_ascii=False)}",
        f"date: {meta['datum']}",
        f"quelle: {json.dumps(meta['quelle'])}",
        f"quelle_name: {json.dumps(meta['quelle_name'], ensure_ascii=False)}",
        f"zweck: {json.dumps(meta['zweck'])}",
        f"format: {json.dumps(fmt_kennung)}",
        f"tags: {json.dumps(hashtags, ensure_ascii=False)}",
        'status: "ungeprüft"',
        f"folgenseite: {json.dumps(meta['seite'])}",
        f"audio: {json.dumps(meta['url'])}",
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
            if meta.get("url"):
                marke = f"[{marke}]({meta['url']}#t={in_sekunden(marke)})"
            elif sprung and meta.get("seite"):
                marke = f"[{marke}]({meta['seite']}{sprung.format(zeit=marke, sekunden=in_sekunden(marke))})"
            zeilen.append(f"- **{f['titel']}** ({f['art']}, {marke}): {f['beschreibung']}")
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")


def woerter(text):
    return re.findall(r"\w+", text.lower())


def finde_beleg(beleg, gruppe):
    """Sucht die Belegstelle im Abschnitt. Gibt den Index des Whisper-Abschnitts zurück oder None.

    Verglichen wird über ein Fenster aus drei aufeinanderfolgenden Abschnitten. Mindestens
    60 Prozent der Wörter des Belegs müssen darin vorkommen, und mindestens zwei."""
    gesucht = [w for w in woerter(beleg) if len(w) > 2]
    if len(gesucht) < 2:
        return None
    bester, bester_anteil = None, 0.0
    for i in range(len(gruppe)):
        fenster = set(woerter(" ".join(a["text"] for a in gruppe[i:i + 3])))
        anteil = sum(w in fenster for w in gesucht) / len(gesucht)
        if anteil > bester_anteil:
            bester, bester_anteil = i, anteil
    if bester_anteil < BELEG_ANTEIL:
        return None
    # Innerhalb des Fensters der Abschnitt mit den meisten Wörtern des Belegs
    fenster = range(bester, min(bester + 3, len(gruppe)))
    return max(fenster, key=lambda j: (len(set(woerter(gruppe[j]["text"])) & set(gesucht)), -j))


def titel_in_der_naehe(titel, gruppe, index):
    """Kommt ein Wort des Titels (die ersten fünf Buchstaben) bis zu einer Minute um die Belegstelle vor?"""
    mitte = gruppe[index]["start"]
    umfeld = " ".join(a["text"] for a in gruppe if abs(a["start"] - mitte) <= BELEG_UMFELD_SEKUNDEN).lower()
    teile = [w[:5] for w in woerter(titel) if len(w) >= 4]
    return not teile or any(w in umfeld for w in teile)


def pruefe(k, gruppe, gruende):
    """Gibt den Eintrag mit Zeitmarke aus der Belegstelle zurück oder None, wenn er herausfällt."""
    schwelle = MIN_RELEVANZ_THESEN if k.get("art") in OHNE_NAMEN else MIN_RELEVANZ
    index = finde_beleg(k.get("beleg", ""), gruppe)
    if index is None:
        gruende["kein Beleg"] += 1
    elif k.get("art") not in OHNE_NAMEN and not titel_in_der_naehe(k.get("titel", ""), gruppe, index):
        gruende["Titel nicht am Beleg"] += 1
    elif k.get("art") not in OHNE_NAMEN and not k.get("konkreter_name"):
        gruende["kein Name"] += 1
    elif k.get("erzaehlung"):
        gruende["Erzählung"] += 1
    elif k.get("sendungshinweis"):
        gruende["Sendung"] += 1
    elif k.get("relevanz", 0) < schwelle:
        gruende["Relevanz"] += 1
    else:
        k["zeitmarke"] = zeitmarke(gruppe[index]["start"])
        return k
    return None


def main():
    parser = argparse.ArgumentParser(description="Transkript auswerten und Notiz schreiben")
    parser.add_argument("transkript", type=Path, help="JSON-Datei unter DATEN/transkripte/<quelle>/")
    parser.add_argument("--stoppdatei", type=Path, help="bei gesetztem Stoppsignal fertig machen oder abbrechen")
    parser.add_argument("--hoechstens", type=float, default=10, help="Minuten, die nach dem Stoppsignal noch erlaubt sind")
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

    notizordner = notizen / "inhalt" / "notizen"
    bekannt = vorhandene_hashtags(notizordner, audio.stem)
    zweck_tags = {vergleichsform(t) for t in PROFIL_ABSCHNITTE.values()}
    vorrat = [t for t in bekannt if vergleichsform(t) not in zweck_tags][:VORHANDENE_HOECHSTENS]

    warteschlange = teile_auf(transkript["abschnitte"])
    print(f"{meta['titel']}: {fmt['name']}, {len(warteschlange)} Abschnitte, Modell {konfiguration['ollama']['modell']}")

    aufgabe = aufgabe_abschnitt(fmt)
    behalten = {name: [] for name in fmt["listen"]}
    protokoll, nr = [], 0
    beginn, fertig_machen = time.monotonic(), False
    while warteschlange:
        if args.stoppdatei and not fertig_machen and args.stoppdatei.exists():
            je_abschnitt = (time.monotonic() - beginn) / nr if nr else SEKUNDEN_PRO_ABSCHNITT
            rest = je_abschnitt * len(warteschlange) + SEKUNDEN_ZUSAMMENFUEHREN
            if rest > args.hoechstens * 60:
                print(f"Stoppsignal: noch etwa {rest / 60:.0f} Minuten, Auswertung wird abgebrochen.", flush=True)
                sys.exit(ABGEBROCHEN)
            print(f"Stoppsignal: noch etwa {rest / 60:.0f} Minuten, Auswertung wird fertig gemacht.", flush=True)
            fertig_machen = True
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
                if pruefe(k, gruppe, gruende):
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
                for f in antwort.get(name, []):
                    f["zeitmarke"] = einheitliche_marke(f.get("zeitmarke", ""))
                funde += [f for f in antwort.get(name, []) if f["zeitmarke"] in erlaubt][: liste["hoechstens"]]
            art = "mit Nachdenken"
        except json.JSONDecodeError:
            for name, liste in fmt["listen"].items():
                beste = sorted(eingereicht[name], key=lambda k: -k["relevanz"])[: liste["hoechstens"]]
                funde += [{key: k[key] for key in ("zeitmarke", "titel", "art", "beschreibung")} for k in beste]
            art = "ERSATZWEISE nach Relevanz, Antwort nicht lesbar"
        funde.sort(key=lambda f: in_sekunden(f["zeitmarke"]))
        uebersicht = ", ".join(f"{len(behalten[n])} {n}" for n in fmt["listen"])
        print(f"  Zusammenführen {art}: aus {uebersicht} wurden {len(funde)} Einträge; {bericht(statistik)}")
        protokoll.append({"schritt": "zusammenführen", "roh": roh, **statistik})

    if funde:
        hashtags, eintrag = vergib_hashtags(konfiguration, meta, funde)
        protokoll.append(eintrag)
        print(f"  Hashtags vergeben: {bericht(eintrag)}")
        if not hashtags:
            # Ersatz: die häufigsten Hashtags der Kandidaten aus den Abschnitten
            zaehler = Counter(sauberer_hashtag(h) for liste in behalten.values() for k in liste for h in k.get("hashtags", []))
            hashtags = [h for h, _ in zaehler.most_common(6) if h]

    tags, angeglichen = [], []
    if funde:
        # 1. Schreibweise: gleiche Hashtags bis auf Groß- und Kleinschreibung oder Umlaute
        schreibweise = {vergleichsform(t): t for t in reversed(bekannt)}
        kandidaten = []
        for h in [PROFIL_ABSCHNITTE[meta["zweck"]]] + list(hashtags):
            h = sauberer_hashtag(str(h))
            if not h:
                continue
            h = schreibweise.get(vergleichsform(h), h)
            if vergleichsform(h) not in {vergleichsform(t) for t in tags}:
                tags.append(h)
            if vergleichsform(h) not in schreibweise and vergleichsform(h) not in zweck_tags:
                kandidaten.append(h)
        # 2. Bedeutung: das Modell meldet echte Entsprechungen, der Code ersetzt nur diese
        zuordnung, eintrag = gleiche_an(konfiguration, kandidaten, vorrat)
        if eintrag:
            protokoll.append(eintrag)
            print(f"  Hashtags angleichen: {bericht(eintrag)}")
        ergebnis = []
        for h in tags:
            ersatz = zuordnung.get(h)
            if ersatz:
                angeglichen.append(f"#{h} → #{ersatz}")
                h = ersatz
            if vergleichsform(h) not in {vergleichsform(t) for t in ergebnis}:
                ergebnis.append(h)
        tags = ergebnis
        neu = [h for h in tags if vergleichsform(h) not in schreibweise and vergleichsform(h) not in zweck_tags]
        print(f"Hashtags: {' '.join('#' + t for t in tags)}")
        print(f"  neu: {' '.join('#' + t for t in neu) or 'keine'}; angeglichen: {', '.join(angeglichen) or 'keine'}; "
              f"{len(vorrat)} vorhandene im Abgleich")

    roh_pfad = Path(konfiguration["pfade"]["daten"]) / "auswertungen" / meta["quelle"] / f"{audio.stem}.json"
    roh_pfad.parent.mkdir(parents=True, exist_ok=True)
    roh_pfad.write_text(json.dumps(protokoll, ensure_ascii=False, indent=1), encoding="utf-8")

    if not funde:
        print("Nichts Relevantes gefunden, keine Notiz geschrieben.")
        return

    ziel = notizordner / f"{audio.stem}.md"
    schreibe_notiz(ziel, meta, fmt_kennung, funde, tags, quelle.get("sprunglink"))
    print(f"{len(funde)} Einträge, Notiz: {ziel}")


if __name__ == "__main__":
    main()
