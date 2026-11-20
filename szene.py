"""Fediverse-Konten der Szene über ihre RSS-Feeds lesen und als Tagesnotiz auswerten.

Aufruf:
  .venv/bin/python szene.py                 neue Beiträge auswerten und Notiz schreiben
  .venv/bin/python szene.py --nur-zeigen    nur anzeigen, welche Beiträge neu sind
  .venv/bin/python szene.py --tage 3        wie weit zurück beim ersten Mal (Vorgabe 2)

Konten stehen in NOTIZEN/szene.txt, eine Adresse pro Zeile, etwa @CCC@social.bau-ha.us.
Zeilen mit # sind Kommentare. Gelesen wird der öffentliche RSS-Feed jedes Kontos, ohne Konto,
ohne Token, ohne Folgen. Konten mit #nobot in der Profilbeschreibung werden ausgelassen.
Zuerst wird die Mastodon-Adresse instanz/@name.rss versucht. Klappt das nicht (etwa bei Friendica
oder wenn die Domain der Adresse nicht der Server ist), sucht das Programm den Feed über WebFinger.
Gefundene Feed-Adressen merkt es sich in DATEN/szene/feeds.json.

Das Modell bekommt die neuen Beiträge nummeriert, bewertet jeden gegen das Interessenprofil und
fasst die relevanten in ein bis zwei Sätzen zusammen. Übernommen wird ab Relevanz 3. Die Notiz
heißt „Szene am TT.MM.JJJJ“ und verlinkt jeden Eintrag auf den Beitrag und auf die Links darin.
Bereits gesehene Beiträge stehen in DATEN/szene/gesehen.json.

Ausgabe: der Pfad der Notiz in der letzten Zeile, oder „keine Notiz“.
"""

import argparse
import html
import itertools
import json
import re
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser

from auswerten import (PROJEKT, frage_modell, gleiche_an, profil_auszug, sauberer_hashtag, vergib_hashtags,
                       vergleichsform, vorhandene_hashtags)

KENNUNG_PROGRAMM = "lesesaal/0.1"
MIN_RELEVANZ = 3
PRO_ANFRAGE = 30
ZEICHEN_PRO_BEITRAG = 700
ARTEN = ["Meldung", "Termin", "Veranstaltung", "Projekt", "Werkzeug", "Organisation", "Initiative", "Literatur"]
PROFIL = ["Tech", "Theorie", "Finanzen", "Netzwerk und Andocken"]


def lies_konten(pfad):
    if not pfad.exists():
        raise SystemExit(f"Keine Kontenliste: {pfad}")
    konten = []
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#"):
            continue
        adresse = zeile.split()[0].lstrip("@")
        if re.fullmatch(r"[\w.]+@[\w.-]+\.\w+", adresse) and adresse not in konten:
            konten.append(adresse)
    return konten


def hole_json(adresse):
    anfrage = urllib.request.Request(adresse, headers={"User-Agent": KENNUNG_PROGRAMM,
                                                       "Accept": "application/jrd+json, application/json"})
    with urllib.request.urlopen(anfrage, timeout=20) as antwort:
        return json.load(antwort)


def feed_kandidaten(konto):
    """Mögliche Feed-Adressen eines Kontos: erst das Mastodon-Muster, dann was WebFinger verrät."""
    name, instanz = konto.split("@", 1)
    yield f"https://{instanz}/@{name}.rss"
    try:
        finger = hole_json(f"https://{instanz}/.well-known/webfinger?resource="
                           + urllib.parse.quote(f"acct:{konto}"))
    except Exception:  # noqa: BLE001
        return
    links = finger.get("links", [])
    for link in links:
        if link.get("type") in ("application/atom+xml", "application/rss+xml") and link.get("href"):
            yield link["href"]
    for link in links:
        if link.get("rel") == "http://webfinger.net/rel/profile-page" and link.get("href"):
            seite = link["href"].rstrip("/")
            yield seite + ".rss"
            friendica = re.match(r"(https://[^/]+)/profile/([^/?#]+)$", seite)
            if friendica:
                yield f"{friendica.group(1)}/feed/{friendica.group(2)}"


def brauchbar(feed):
    return getattr(feed, "status", 200) < 400 and (feed.entries or not feed.get("bozo"))


def lies_feed(konto, feeds):
    """Den Feed eines Kontos lesen; die funktionierende Adresse kommt nach feeds[konto]."""
    versucht = []
    kandidaten = itertools.chain([feeds[konto]] if konto in feeds else [], feed_kandidaten(konto))
    for adresse in kandidaten:
        if adresse in versucht:
            continue
        feed = feedparser.parse(adresse, agent=KENNUNG_PROGRAMM)
        if brauchbar(feed):
            feeds[konto] = adresse
            return feed
        versucht.append(adresse)
    raise RuntimeError("kein lesbarer Feed, versucht: " + ", ".join(versucht))


def text_aus_html(roh):
    roh = re.sub(r"<br\s*/?>|</p>\s*<p>", "\n", roh or "")
    return html.unescape(re.sub(r"<[^>]+>", "", roh)).strip()


def links_aus_html(roh):
    """Links aus dem Beitrag, ohne Erwähnungen und Hashtag-Seiten."""
    links = []
    for tag in re.findall(r"<a\s[^>]*>", roh or ""):
        href = re.search(r'href="([^"]+)"', tag)
        if not href:
            continue
        href = html.unescape(href.group(1))
        klasse = (re.search(r'class="([^"]*)"', tag) or [None, ""])[1]
        if "mention" in klasse or "hashtag" in klasse or "/tags/" in href or re.search(r"/@[\w.]+/?$", href):
            continue
        if href not in links:
            links.append(href)
    return links


def lies_beitraege(konto, seit, feeds):
    feed = lies_feed(konto, feeds)
    profil = (feed.feed.get("subtitle", "") + " " + feed.feed.get("description", "")).lower()
    if "#nobot" in profil:
        return None
    beitraege = []
    for e in feed.entries:
        zeit = e.get("published_parsed")
        if not zeit:
            continue
        datum = datetime.fromtimestamp(time.mktime(zeit), tz=timezone.utc)
        if datum < seit:
            continue
        roh = e.get("description", "") or e.get("summary", "") or (e.get("content") or [{}])[0].get("value", "")
        text = text_aus_html(roh)
        if not text:
            continue
        beitraege.append({
            "id": e.get("id") or e.get("link"),
            "konto": konto,
            "link": e.get("link", ""),
            "datum": datum.strftime("%d.%m."),
            "text": text[:ZEICHEN_PRO_BEITRAG],
            "links": links_aus_html(roh),
        })
    return beitraege


def bewerte(konfiguration, profil, beitraege):
    zeilen = []
    for nr, b in enumerate(beitraege, 1):
        links = f" | Links: {', '.join(b['links'][:3])}" if b["links"] else ""
        zeilen.append(f"[{nr}] @{b['konto']} ({b['datum']}): {' '.join(b['text'].split())}{links}")
    inhalt = (
        "Neue öffentliche Beiträge aus der Szene:\n\n" + "\n".join(zeilen)
        + f"\n\n---\n\nInteressenprofil:\n\n{profil}\n\n---\n\n"
        "Aufgabe: Bewerte jeden Beitrag, wie relevant er für das Interessenprofil ist, von 1 (gar nicht) "
        "bis 5 (sehr). Gib für jeden Beitrag ab Relevanz 3 einen Eintrag zurück:\n"
        "- nr: die Nummer des Beitrags in eckigen Klammern.\n"
        "- titel: worum es geht, höchstens acht Wörter, auf Deutsch.\n"
        f"- art: eine von {', '.join(ARTEN)}.\n"
        "- beschreibung: ein bis zwei Sätze auf Deutsch, nur was im Beitrag steht. Bei Terminen Datum und Ort.\n"
        "- relevanz: 3 bis 5.\n"
        "Beiträge ohne Inhalt für das Profil, reine Unterhaltung, Werbung und Antworten ohne Zusammenhang weglassen."
    )
    schema = {
        "type": "object",
        "properties": {"eintraege": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "nr": {"type": "integer"},
                "titel": {"type": "string"},
                "art": {"type": "string", "enum": ARTEN},
                "beschreibung": {"type": "string"},
                "relevanz": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["nr", "titel", "art", "beschreibung", "relevanz"],
        }}},
        "required": ["eintraege"],
    }
    roh, statistik = frage_modell(konfiguration, inhalt, schema)
    try:
        eintraege = json.loads(roh).get("eintraege", [])
    except json.JSONDecodeError:
        eintraege = []
    ergebnis, gesehen = [], set()
    for e in eintraege:
        nr = e.get("nr")
        if not isinstance(nr, int) or not 1 <= nr <= len(beitraege) or nr in gesehen:
            continue
        if e.get("relevanz", 0) < MIN_RELEVANZ:
            continue
        gesehen.add(nr)
        ergebnis.append({**e, "beitrag": beitraege[nr - 1]})
    return ergebnis, statistik


def domain(adresse):
    return re.sub(r"^https?://(www\.)?", "", adresse).split("/")[0]


def schreibe_notiz(pfad, titel, datum, eintraege, tags, anzahl_beitraege, anzahl_konten):
    zeilen = [
        "---",
        f"title: {json.dumps(titel, ensure_ascii=False)}",
        f"date: {datum}",
        'quelle: "szene"',
        'quelle_name: "Fediverse-Szene"',
        'zweck: "tech"',
        'format: "meldungen"',
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        'status: "ungeprüft"',
        'folgenseite: ""',
        'audio: ""',
        "---",
        "",
        f"Meldungsliste aus {anzahl_beitraege} neuen Beiträgen von {anzahl_konten} Konten.",
        "",
        "## Meldungen",
        "",
    ]
    for e in sorted(eintraege, key=lambda e: -e["relevanz"]):
        b = e["beitrag"]
        quelle = f"[@{b['konto'].split('@')[0]}]({b['link']})" if b["link"] else f"@{b['konto']}"
        weiter = " ".join(f"[{domain(l)}]({l})" for l in b["links"][:3])
        zeilen.append(f"- **{e['titel']}** ({e['art']}, {quelle}): {e['beschreibung']}" + (f" Links: {weiter}" if weiter else ""))
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("\n".join(zeilen) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Fediverse-Konten der Szene auswerten")
    parser.add_argument("--nur-zeigen", action="store_true", help="nur neue Beiträge anzeigen")
    parser.add_argument("--tage", type=int, default=2, help="wie weit zurück beim ersten Mal")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)
    notizen = Path(konfiguration["pfade"]["notizen"])
    ordner = Path(konfiguration["pfade"]["daten"]) / "szene"
    ordner.mkdir(parents=True, exist_ok=True)
    stand_pfad = ordner / "gesehen.json"
    stand = json.loads(stand_pfad.read_text(encoding="utf-8")) if stand_pfad.exists() else {}
    feeds_pfad = ordner / "feeds.json"
    feeds = json.loads(feeds_pfad.read_text(encoding="utf-8")) if feeds_pfad.exists() else {}

    konten = lies_konten(notizen / "szene.txt")
    seit = datetime.now(timezone.utc) - timedelta(days=args.tage)
    neu, nobot = [], []
    for konto in konten:
        try:
            beitraege = lies_beitraege(konto, seit, feeds)
        except Exception as fehler:  # noqa: BLE001
            print(f"  FEHLER bei @{konto}: {fehler}")
            continue
        if beitraege is None:
            nobot.append(konto)
            continue
        bekannt = set(stand.get(konto, []))
        frisch = [b for b in beitraege if b["id"] not in bekannt]
        print(f"  @{konto}: {len(frisch)} neu")
        neu += frisch
    for konto in nobot:
        print(f"  @{konto}: #nobot im Profil, ausgelassen")
    feeds_pfad.write_text(json.dumps(feeds, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.nur_zeigen:
        for b in neu:
            print(f"- @{b['konto']} {b['datum']}: {' '.join(b['text'].split())[:120]}")
        return
    if not neu:
        print("keine Notiz")
        return

    profil = profil_auszug((notizen / "interessenprofil.md").read_text(encoding="utf-8"), PROFIL)
    eintraege = []
    for start in range(0, len(neu), PRO_ANFRAGE):
        teil = neu[start:start + PRO_ANFRAGE]
        gefunden, statistik = bewerte(konfiguration, profil, teil)
        print(f"  Beiträge {start + 1} bis {start + len(teil)}: {len(gefunden)} relevant; "
              f"{statistik['eingabe_token']}+{statistik['ausgabe_token']} Token, {statistik['sekunden']:.0f} s")
        eintraege += gefunden

    for b in neu:
        stand.setdefault(b["konto"], []).append(b["id"])
        stand[b["konto"]] = stand[b["konto"]][-300:]
    if not eintraege:
        stand_pfad.write_text(json.dumps(stand, ensure_ascii=False, indent=1), encoding="utf-8")
        print("keine Notiz")
        return

    heute = datetime.now()
    stem = f"{heute:%Y-%m-%d}_szene"
    notizordner = notizen / "inhalt" / "notizen"
    nr = 2
    while (notizordner / f"{stem}.md").exists():
        stem = f"{heute:%Y-%m-%d}_szene-{nr}"
        nr += 1

    funde = [{"titel": e["titel"], "art": e["art"], "beschreibung": e["beschreibung"]} for e in eintraege]
    hashtags, _ = vergib_hashtags(konfiguration, {"titel": f"Szene am {heute:%d.%m.%Y}"}, funde)
    bekannt = vorhandene_hashtags(notizordner, stem)
    schreibweise = {vergleichsform(t): t for t in reversed(bekannt)}
    tags = ["Tech"]
    kandidaten = []
    for h in hashtags:
        h = schreibweise.get(vergleichsform(sauberer_hashtag(h)), sauberer_hashtag(h))
        if h and vergleichsform(h) not in {vergleichsform(t) for t in tags}:
            tags.append(h)
            if vergleichsform(h) not in schreibweise:
                kandidaten.append(h)
    zuordnung, _ = gleiche_an(konfiguration, kandidaten, [t for t in bekannt if vergleichsform(t) != "tech"][:300])
    tags = list(dict.fromkeys(zuordnung.get(t, t) for t in tags))

    pfad = notizordner / f"{stem}.md"
    schreibe_notiz(pfad, f"Szene am {heute:%d.%m.%Y}", f"{heute:%Y-%m-%d}", eintraege, tags,
                   len(neu), len({b["konto"] for b in neu}))
    stand_pfad.write_text(json.dumps(stand, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(eintraege)} Einträge aus {len(neu)} Beiträgen")
    print(pfad)


if __name__ == "__main__":
    main()
