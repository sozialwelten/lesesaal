"""Termine aus der Berliner Szene sammeln: Seite „Termine“ im Lesesaal und Kalender zum Abonnieren.

Aufruf:
  .venv/bin/python termine.py                  Quellen lesen, auswählen, Seite und Kalender schreiben
  .venv/bin/python termine.py --nur-zeigen     nur anzeigen, was gefunden und ausgewählt würde
  .venv/bin/python termine.py --ohne-modell    alles übernehmen, keine Auswahl durch das Modell
  .venv/bin/python termine.py --aus "Star Trek"  Reihen, deren Titel das enthält, künftig weglassen
  .venv/bin/python termine.py --an "Star Trek"   Reihen, deren Titel das enthält, künftig aufnehmen

Quellen stehen in NOTIZEN/termine.txt, eine pro Zeile:
  typ adresse Name | Ort
Typen:
  ical  Kalenderdatei (.ics), wiederkehrende Termine werden aufgelöst
  feed  RSS-Feed, etwa WordPress; Datum und Uhrzeit stehen im Titel („am 14. November um 19 Uhr“).
        Hat der Feed Kategorien, zählen nur Beiträge der Kategorie „Termine“.
  pad   Textexport eines Pads, Termine als Zeilen „★ TT.MM. HH:MM - HH:MM ~ Titel“
„| Ort“ ist freiwillig und gilt für Termine der Quelle ohne eigene Ortsangabe.
Zeilen mit # sind Kommentare.

Termine mit „internal“, „intern“ oder „members only“ im Titel fallen weg. Über den Rest entscheidet
das Modell anhand des Interessenprofils, und zwar einmal je Reihe: Ein wöchentlicher Termin wird nur
beim ersten Mal bewertet. Die Entscheidungen stehen in DATEN/termine/auswahl.json und werden auch
beim Probelauf gespeichert. Ändern lassen sie sich mit --an und --aus.

Feste eigene Termine stehen in NOTIZEN/belegt.txt, einer pro Zeile:
  Di Do 18:00-21:30 Bilanzbuchhalter-Weiterbildung ab 2026-09-01 bis 2027-06-30
„ab“ und „bis“ sind freiwillig. Ein Termin, der in so ein Zeitfenster fällt, kommt nicht in den
Kalender und nicht in die Direktnachricht, sondern auf der Seite in einen eigenen Abschnitt.
Ausnahme: Bleibt vor oder nach dem Fenster mindestens FREI_MINDESTENS Minuten vom Termin übrig
(ein Drop-in ab 14 Uhr, ein offener Abend bis Mitternacht), bleibt er drin.

Ergebnis:
  NOTIZEN/inhalt/termine/index.md      Seite „Termine“, nach Tagen gegliedert, neue Termine markiert
  NOTIZEN/inhalt/termine/termine.ics   alle ausgewählten Termine als Kalender
  DATEN/termine/bald.json              Termine der nächsten 48 Stunden für die Direktnachricht
Letzte Zeile der Ausgabe: der Pfad der Seite, wenn sich etwas geändert hat, sonst „unverändert“.
"""

import argparse
import hashlib
import html
import json
import re
import tomllib
import urllib.request
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import icalendar
import recurring_ical_events

from auswerten import PROJEKT, frage_modell, profil_auszug

BERLIN = ZoneInfo("Europe/Berlin")
KENNUNG_PROGRAMM = "lesesaal/0.1"
PRO_ANFRAGE = 40
PROFIL = ["Tech", "Theorie", "Finanzen", "Netzwerk und Andocken"]
AUSSCHLUSS = re.compile(r"\binternal\b|\bintern\b|members only|nur für mitglieder", re.I)
MONATE = ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september",
          "oktober", "november", "dezember"]
FREI_MINDESTENS = 60
KURZTAGE = ["mo", "di", "mi", "do", "fr", "sa", "so"]
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


# Quellen

def lies_quellen(pfad):
    if not pfad.exists():
        raise SystemExit(f"Keine Quellenliste: {pfad}")
    quellen = []
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#"):
            continue
        zeile, _, ort = zeile.partition("|")
        teile = zeile.split(None, 2)
        if len(teile) < 3 or teile[0] not in ("ical", "feed", "pad"):
            print(f"  Zeile nicht verstanden: {zeile}")
            continue
        quellen.append({"typ": teile[0], "adresse": teile[1], "name": teile[2].strip(), "ort": ort.strip()})
    return quellen


def hole_text(adresse):
    anfrage = urllib.request.Request(adresse, headers={"User-Agent": KENNUNG_PROGRAMM})
    with urllib.request.urlopen(anfrage, timeout=60) as antwort:
        return antwort.read().decode("utf-8", errors="replace")


def als_zeit(wert):
    """Datum oder Zeitpunkt aus dem Kalender als Zeitpunkt in Berliner Zeit; ganztags um 0 Uhr."""
    if isinstance(wert, datetime):
        return wert.astimezone(BERLIN) if wert.tzinfo else wert.replace(tzinfo=BERLIN)
    return datetime.combine(wert, time(0), tzinfo=BERLIN)


def kurz(text, laenge=400):
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())
    return text if len(text) <= laenge else text[:laenge].rsplit(" ", 1)[0] + " …"


def aus_ical(quelle, von, bis):
    kalender = icalendar.Calendar.from_ical(hole_text(quelle["adresse"]))
    termine = []
    for ereignis in recurring_ical_events.of(kalender).between(von, bis):
        beginn = ereignis.get("DTSTART").dt
        ende = ereignis.get("DTEND").dt if ereignis.get("DTEND") else None
        ganztags = not isinstance(beginn, datetime)
        termine.append({
            "reihe": str(ereignis.get("UID", "")) or str(ereignis.get("SUMMARY", "")),
            "titel": " ".join(str(ereignis.get("SUMMARY", "")).split()),
            "beginn": als_zeit(beginn),
            "ende": als_zeit(ende) if ende else None,
            "ganztags": ganztags,
            "ort": " ".join(str(ereignis.get("LOCATION", "")).replace("\\n", " ").split()),
            "link": str(ereignis.get("URL", "")),
            "beschreibung": kurz(str(ereignis.get("DESCRIPTION", ""))),
        })
    return termine


def datum_im_text(text, bezug):
    """„14. November“ mit Uhrzeit „um 19 Uhr“ oder „ab 19:30 Uhr“; Jahr passend zum Erscheinungsdatum."""
    treffer = re.search(r"(\d{1,2})\.\s*(" + "|".join(MONATE) + r")", text, re.I)
    if not treffer:
        return None, False
    tag, monat = int(treffer.group(1)), MONATE.index(treffer.group(2).lower()) + 1
    jahr = bezug.year
    try:
        if date(jahr, monat, tag) < bezug.date() - timedelta(days=14):
            jahr += 1
        uhr = re.search(r"(?:um|ab)\s*(\d{1,2})(?:[:.](\d{2}))?\s*Uhr", text, re.I)
        if uhr:
            return datetime(jahr, monat, tag, int(uhr.group(1)), int(uhr.group(2) or 0), tzinfo=BERLIN), False
        return datetime(jahr, monat, tag, tzinfo=BERLIN), True
    except ValueError:
        return None, False


def aus_feed(quelle, von, bis):
    feed = feedparser.parse(quelle["adresse"], agent=KENNUNG_PROGRAMM)
    termine = []
    for e in feed.entries:
        kategorien = [t.get("term", "").lower() for t in e.get("tags", [])]
        if kategorien and "termine" not in kategorien:
            continue
        bezug = datetime(*e.published_parsed[:6], tzinfo=BERLIN) if e.get("published_parsed") else datetime.now(BERLIN)
        text = (e.get("content") or [{}])[0].get("value", "") or e.get("summary", "")
        beginn, ganztags = datum_im_text(e.get("title", ""), bezug)
        if not beginn:
            beginn, ganztags = datum_im_text(kurz(text, 2000), bezug)
        elif ganztags:
            uhr = re.search(r"(?:um|ab)\s*(\d{1,2})(?:[:.](\d{2}))?\s*Uhr", kurz(text, 2000), re.I)
            if uhr:
                beginn, ganztags = beginn.replace(hour=int(uhr.group(1)), minute=int(uhr.group(2) or 0)), False
        if not beginn:
            print(f"  {quelle['name']}: kein Datum in „{e.get('title', '')}“")
            continue
        if von <= beginn < bis:
            titel = html.unescape(e.get("title", ""))
            termine.append({"reihe": re.sub(r"\s+am\s+\d.*$", "", titel), "titel": titel, "beginn": beginn,
                            "ende": None, "ganztags": ganztags, "ort": "", "link": e.get("link", ""),
                            "beschreibung": kurz(text)})
    return termine


def aus_pad(quelle, von, bis):
    adresse = quelle["adresse"].rstrip("/")
    if adresse.startswith("http") and not adresse.endswith("/export/txt"):
        adresse += "/export/txt"
    heute = datetime.now(BERLIN)
    termine, aktuell = [], None
    for zeile in hole_text(adresse).splitlines():
        kopf = re.match(r"\s*★\s*(\d{1,2})\.(\d{1,2})\.?\s*(.*)$", zeile)
        if kopf:
            tag, monat, rest = int(kopf.group(1)), int(kopf.group(2)), kopf.group(3)
            try:
                jahr = heute.year + (1 if date(heute.year, monat, tag) < heute.date() - timedelta(days=60) else 0)
            except ValueError:
                aktuell = None
                continue
            zeiten = re.search(r"(\d{1,2})[:.](\d{2})(?:\s*(?:-|–|bis)\s*(\d{1,2})[:.](\d{2}))?", rest)
            titel = rest
            if zeiten:
                titel = rest[:zeiten.start()] + rest[zeiten.end():]
            titel = " ".join(re.sub(r"\s[-–]\s(?=\s*\(|$)", " ", titel.replace("~", " ")).strip(" -–").split())
            try:
                beginn = datetime(jahr, monat, tag, int(zeiten.group(1)) if zeiten else 0,
                                  int(zeiten.group(2)) if zeiten else 0, tzinfo=BERLIN)
            except ValueError:
                aktuell = None
                continue
            ende = None
            if zeiten and zeiten.group(3):
                ende = beginn.replace(hour=int(zeiten.group(3)) % 24, minute=int(zeiten.group(4)))
                if ende <= beginn:
                    ende += timedelta(days=1)
            aktuell = {"reihe": titel, "titel": titel, "beginn": beginn, "ende": ende, "ganztags": not zeiten,
                       "ort": "", "link": "", "beschreibung": ""}
            if von <= beginn < bis:
                termine.append(aktuell)
            continue
        if aktuell is None or not zeile.strip():
            continue
        if zeile.strip().startswith(("~~", "◆")):
            aktuell = None
            continue
        link = re.search(r"https?://\S+", zeile)
        if link and not aktuell["link"] and zeile.strip().startswith(">"):
            aktuell["link"] = link.group(0)
        elif len(aktuell["beschreibung"]) < 400:
            aktuell["beschreibung"] = kurz(aktuell["beschreibung"] + " " + zeile)
    return termine


LESER = {"ical": aus_ical, "feed": aus_feed, "pad": aus_pad}


# Feste eigene Termine

def lies_belegt(pfad):
    belegt = []
    if not pfad.exists():
        return belegt
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#"):
            continue
        zeit = re.search(r"(\d{1,2})[:.](\d{2})\s*-\s*(\d{1,2})[:.](\d{2})", zeile)
        tage = [KURZTAGE.index(w[:2].lower()) for w in re.findall(r"\b(?:Mo|Di|Mi|Do|Fr|Sa|So)\b", zeile[:zeit.start()]
                                                                   if zeit else "", re.I)]
        if not zeit or not tage:
            print(f"  belegt.txt: Zeile nicht verstanden: {zeile}")
            continue
        ab = re.search(r"\bab\s+(\d{4}-\d{2}-\d{2})", zeile)
        bis = re.search(r"\bbis\s+(\d{4}-\d{2}-\d{2})", zeile)
        name = re.sub(r"\b(?:ab|bis)\s+\d{4}-\d{2}-\d{2}", "", zeile[zeit.end():]).strip() or "fester Termin"
        belegt.append({"tage": tage, "von": time(int(zeit.group(1)), int(zeit.group(2))),
                       "bis": time(int(zeit.group(3)), int(zeit.group(4))), "name": name,
                       "ab": date.fromisoformat(ab.group(1)) if ab else None,
                       "bis_datum": date.fromisoformat(bis.group(1)) if bis else None})
    return belegt


def kollision(t, belegt):
    """Name des festen Termins, mit dem sich t überschneidet, sonst None."""
    if t["ganztags"]:
        return None
    tag = t["beginn"].date()
    ende = t["ende"] or t["beginn"] + timedelta(hours=2)
    for b in belegt:
        if tag.weekday() not in b["tage"] or (b["ab"] and tag < b["ab"]) or (b["bis_datum"] and tag > b["bis_datum"]):
            continue
        von = datetime.combine(tag, b["von"], tzinfo=BERLIN)
        bis = datetime.combine(tag, b["bis"], tzinfo=BERLIN)
        mindestens = timedelta(minutes=FREI_MINDESTENS)
        if t["beginn"] < bis and ende > von and von - t["beginn"] < mindestens and ende - bis < mindestens:
            return b["name"]
    return None


# Auswahl

def waehle_aus(konfiguration, profil, reihen):
    """reihen: [(schluessel, quelle, titel, beschreibung)] → {schluessel: (relevant, grund)}"""
    ergebnis = {}
    for start in range(0, len(reihen), PRO_ANFRAGE):
        teil = reihen[start:start + PRO_ANFRAGE]
        zeilen = [f"[{nr}] {quelle}: {titel}" + (f" ({beschreibung[:250]})" if beschreibung else "")
                  for nr, (_, quelle, titel, beschreibung) in enumerate(teil, 1)]
        inhalt = (
            "Termine aus Berliner Hackspaces, Vereinen und Initiativen:\n\n" + "\n".join(zeilen)
            + f"\n\n---\n\nInteressenprofil:\n\n{profil}\n\n---\n\n"
            "Aufgabe: Entscheide für jeden Termin, ob er für das Interessenprofil relevant ist. Relevant sind "
            "Vorträge, Workshops, offene Abende und Treffen, bei denen sich etwas lernen lässt oder bei denen "
            "Andockpunkte entstehen: Vereinsarbeit, Netzwerk, Finanzierung, digitale Zivilgesellschaft, "
            "Technik, Theorie. Nicht relevant sind Sport, Yoga, Spieleabende, Partys, Kinderangebote und interne "
            "Treffen fremder Gruppen, außer das Profil nennt das Thema ausdrücklich.\n"
            "Gib für jeden Termin zurück: nr, relevant (true oder false), grund (höchstens acht Wörter)."
        )
        schema = {
            "type": "object",
            "properties": {"termine": {"type": "array", "items": {
                "type": "object",
                "properties": {"nr": {"type": "integer"}, "relevant": {"type": "boolean"},
                               "grund": {"type": "string"}},
                "required": ["nr", "relevant", "grund"],
            }}},
            "required": ["termine"],
        }
        roh, statistik = frage_modell(konfiguration, inhalt, schema)
        try:
            antworten = json.loads(roh).get("termine", [])
        except json.JSONDecodeError:
            antworten = []
        for a in antworten:
            if 1 <= a.get("nr", 0) <= len(teil):
                ergebnis[teil[a["nr"] - 1][0]] = (bool(a.get("relevant")), a.get("grund", ""))
        print(f"  Reihen {start + 1} bis {start + len(teil)} bewertet: "
              f"{sum(1 for s, *_ in teil if ergebnis.get(s, (False,))[0])} relevant; "
              f"{statistik['eingabe_token']}+{statistik['ausgabe_token']} Token, {statistik['sekunden']:.0f} s")
    return ergebnis


# Ausgabe

def kennung(t):
    return hashlib.sha1(f"{t['quelle']}|{t['titel']}|{t['beginn'].isoformat()}".encode()).hexdigest()[:16]


def uhrzeit(t):
    if t["ganztags"]:
        return "ganztags"
    return f"{t['beginn']:%H:%M}" + (f" bis {t['ende']:%H:%M}" if t["ende"] and t["ende"].date() == t["beginn"].date()
                                     else "")


def schreibe_seite(pfad, termine, tage):
    frei = [t for t in termine if not t["kollision"]]
    belegt = [t for t in termine if t["kollision"]]
    termine = frei
    zeilen = []
    tag_vorher = None
    for t in termine:
        tag = t["beginn"].date()
        if tag != tag_vorher:
            zeilen += ["", f"## {WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.}", ""]
            tag_vorher = tag
        titel = t["titel"].replace("[", "(").replace("]", ")")
        titel = f"[{titel}]({t['link']})" if t["link"] else titel
        ort = t["ort"] or t["quelle_ort"]
        zusatz = ort if ort.lower().startswith(t["quelle"].lower()) else ", ".join(x for x in (t["quelle"], ort) if x)
        zeilen.append(f"- {uhrzeit(t)} **{titel}** ({zusatz})" + (" · neu" if t["neu"] else ""))
    if belegt:
        zeilen += ["", "## Überschneidet sich mit festen Terminen", ""]
        zeilen += [f"- {WOCHENTAGE[t['beginn'].weekday()][:2]} {t['beginn']:%d.%m.} {uhrzeit(t)} {t['titel']} "
                   f"({t['quelle']}; {t['kollision']})" for t in belegt]
    koerper = (f"Ausgewählte Termine der nächsten {tage} Tage aus Hackspaces, Vereinen und Initiativen. "
               f"Als Kalender abonnieren: [termine.ics](termine.ics)\n" + "\n".join(zeilen) + "\n")
    alt = pfad.read_text(encoding="utf-8").split("---\n", 2)[-1] if pfad.exists() else ""
    if alt.strip() == koerper.strip():
        return False
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(f'---\ntitle: "Termine"\ndate: {datetime.now(BERLIN).isoformat(timespec="seconds")}\n---\n'
                    + koerper, encoding="utf-8")
    return True


def schreibe_kalender(pfad, termine):
    kalender = icalendar.Calendar()
    kalender.add("prodid", "-//Lesesaal//Termine//DE")
    kalender.add("version", "2.0")
    kalender.add("x-wr-calname", "Lesesaal Termine")
    for t in termine:
        e = icalendar.Event()
        e.add("uid", f"{kennung(t)}@lesesaal")
        e.add("summary", f"{t['titel']} ({t['quelle']})")
        if t["ganztags"]:
            e.add("dtstart", t["beginn"].date())
        else:
            e.add("dtstart", t["beginn"])
            e.add("dtend", t["ende"] or t["beginn"] + timedelta(hours=2))
        if t["ort"] or t["quelle_ort"]:
            e.add("location", t["ort"] or t["quelle_ort"])
        if t["link"]:
            e.add("url", t["link"])
        if t["beschreibung"]:
            e.add("description", t["beschreibung"])
        e.add("dtstamp", datetime.now(BERLIN))
        kalender.add_component(e)
    pfad.write_bytes(kalender.to_ical())


def main():
    parser = argparse.ArgumentParser(description="Termine sammeln und als Seite und Kalender schreiben")
    parser.add_argument("--nur-zeigen", action="store_true", help="nur anzeigen, nichts schreiben")
    parser.add_argument("--ohne-modell", action="store_true", help="alles übernehmen, keine Auswahl")
    parser.add_argument("--an", metavar="MUSTER", help="passende Reihen künftig aufnehmen")
    parser.add_argument("--aus", metavar="MUSTER", help="passende Reihen künftig weglassen")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)
    einst = {"tage": 28, **konfiguration.get("termine", {})}
    notizen = Path(konfiguration["pfade"]["notizen"])
    ordner = Path(konfiguration["pfade"]["daten"]) / "termine"
    ordner.mkdir(parents=True, exist_ok=True)
    auswahl_pfad, bekannt_pfad = ordner / "auswahl.json", ordner / "bekannt.json"
    auswahl = json.loads(auswahl_pfad.read_text(encoding="utf-8")) if auswahl_pfad.exists() else {}
    bekannt = set(json.loads(bekannt_pfad.read_text(encoding="utf-8"))) if bekannt_pfad.exists() else set()

    if args.an or args.aus:
        muster, relevant = (args.an, True) if args.an else (args.aus, False)
        treffer = [k for k, v in auswahl.items() if muster.lower() in v.get("titel", k).lower()]
        for k in treffer:
            auswahl[k].update(relevant=relevant, grund="von Hand")
            print(f"  {'an' if relevant else 'aus'}: {k}")
        auswahl_pfad.write_text(json.dumps(auswahl, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        print(f"{len(treffer)} Reihen geändert" if treffer else f"Keine Reihe mit „{muster}“ im Titel.")
        return

    jetzt = datetime.now(BERLIN)
    von = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    bis = von + timedelta(days=einst["tage"])
    alle, intern = [], []
    for quelle in lies_quellen(notizen / "termine.txt"):
        try:
            gefunden = LESER[quelle["typ"]](quelle, von, bis)
        except Exception as fehler:  # noqa: BLE001
            print(f"  FEHLER bei {quelle['name']}: {fehler}")
            continue
        weg = [t for t in gefunden if AUSSCHLUSS.search(t["titel"])]
        gefunden = [t for t in gefunden if t not in weg]
        for t in gefunden:
            t.update(quelle=quelle["name"], quelle_ort=quelle["ort"],
                     schluessel=f"{quelle['name']}|{t['reihe']}")
        print(f"  {quelle['name']}: {len(gefunden)} übernommen" + (f", {len(weg)} als intern ausgelassen" if weg else ""))
        alle += gefunden
        intern += [f"{quelle['name']}: {t['titel']}" for t in weg]

    if not args.ohne_modell:
        offen = {}
        for t in alle:
            if t["schluessel"] not in auswahl and t["schluessel"] not in offen:
                offen[t["schluessel"]] = (t["schluessel"], t["quelle"], t["titel"], t["beschreibung"])
        if offen:
            print(f"  {len(offen)} neue Reihen zu bewerten")
            profil = profil_auszug((notizen / "interessenprofil.md").read_text(encoding="utf-8"), PROFIL)
            for schluessel, (relevant, grund) in waehle_aus(konfiguration, profil, list(offen.values())).items():
                auswahl[schluessel] = {"relevant": relevant, "grund": grund, "titel": offen[schluessel][2],
                                       "bewertet": f"{jetzt:%Y-%m-%d}"}
        gewaehlt = [t for t in alle if auswahl.get(t["schluessel"], {}).get("relevant")]
    else:
        gewaehlt = alle

    gewaehlt.sort(key=lambda t: (t["beginn"], t["titel"]))
    belegt = lies_belegt(notizen / "belegt.txt")
    for t in gewaehlt:
        t["neu"] = bool(bekannt) and kennung(t) not in bekannt
        t["kollision"] = kollision(t, belegt)
    print(f"  {len(gewaehlt)} von {len(alle)} Terminen ausgewählt, {sum(t['neu'] for t in gewaehlt)} neu, "
          f"{sum(1 for t in gewaehlt if t['kollision'])} überschneiden sich mit festen Terminen")

    auswahl_pfad.write_text(json.dumps(auswahl, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    if args.nur_zeigen:
        for t in gewaehlt:
            print(f"- {WOCHENTAGE[t['beginn'].weekday()][:2]} {t['beginn']:%d.%m.} "
                  f"{'ganztags' if t['ganztags'] else format(t['beginn'], '%H:%M')}  {t['titel'][:60]}  "
                  f"({t['quelle']})" + (" NEU" if t["neu"] else "")
                  + (f"  ✗ {t['kollision']}" if t["kollision"] else ""))
        weg = sorted({t["titel"] for t in alle if t not in gewaehlt})
        print(f"\nNicht ausgewählt ({len(weg)}): " + "; ".join(x[:40] for x in weg[:40]))
        print(f"\nAls intern ausgelassen ({len(set(intern))}): " + "; ".join(sorted(set(intern))))
        return

    seite = notizen / "inhalt" / "termine" / "index.md"
    geaendert = schreibe_seite(seite, gewaehlt, einst["tage"])
    schreibe_kalender(seite.with_name("termine.ics"), [t for t in gewaehlt if not t["kollision"]])
    bald = [{"zeit": f"{WOCHENTAGE[t['beginn'].weekday()][:2]} {t['beginn']:%d.%m.}"
             + ("" if t["ganztags"] else f" {t['beginn']:%H:%M}"),
             "titel": t["titel"], "quelle": t["quelle"]}
            for t in gewaehlt if not t["kollision"] and t["beginn"] < jetzt + timedelta(hours=48) and t["beginn"] >= jetzt - timedelta(hours=2)]
    (ordner / "bald.json").write_text(json.dumps(bald, ensure_ascii=False, indent=1), encoding="utf-8")
    bekannt = sorted(bekannt | {kennung(t) for t in gewaehlt})[-5000:]
    bekannt_pfad.write_text(json.dumps(bekannt, indent=0), encoding="utf-8")
    print(seite if geaendert else "unverändert")


if __name__ == "__main__":
    main()
