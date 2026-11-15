"""Hinweis auf eine Notiz bei Mastodon posten.

Aufruf:
  .venv/bin/python posten.py NOTIZ.md            zeigt die Beiträge nur an
  .venv/bin/python posten.py NOTIZ.md --senden   postet sie als Thread
  .venv/bin/python posten.py NOTIZ.md --senden --neu
                                                 postet auch, wenn die Notiz schon gepostet wurde

Der erste Beitrag nennt Format, Titel, Quelle und Datum, verlinkt die Notiz
und trägt die Hashtags. Die Einträge der Notiz folgen als Antworten, je Teil
der Notiz ein Beitrag, bei Bedarf mehrere. Sichtbarkeit: nur Follower.

Aus der .env: MASTODON_ADRESSE, MASTODON_TOKEN, GEHEIMER_PFAD
Bereits gepostete Notizen stehen in DATEN/gepostet.json.
"""

import argparse
import json
import re
import time
import tomllib
from pathlib import Path

import requests

from bauen import PROJEKT, lies_env

GRENZE = 500
URL_LAENGE = 23  # Mastodon zählt jeden Link mit 23 Zeichen
SICHTBARKEIT = "private"


def laenge(text):
    return len(text) + sum(URL_LAENGE - len(u) for u in re.findall(r"https?://\S+", text))


def lies_notiz(pfad):
    text = pfad.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise SystemExit("Die Notiz hat keinen Kopfbereich.")
    ende = text.index("\n---\n", 4)
    kopf = {}
    for zeile in text[4:ende].splitlines():
        if ":" not in zeile:
            continue
        schluessel, wert = zeile.split(":", 1)
        try:
            kopf[schluessel.strip()] = json.loads(wert)
        except json.JSONDecodeError:
            kopf[schluessel.strip()] = wert.strip()
    teile, aktuell = {}, "Fundstücke"
    for zeile in text[ende + 5:].splitlines():
        if zeile.startswith("## "):
            aktuell = zeile[3:].strip()
            continue
        treffer = re.match(r"- \*\*(.+?)\*\* \(([^,()]+),", zeile)
        if treffer:
            teile.setdefault(aktuell, []).append(f"{treffer[1]} ({treffer[2]})"[:400])
    return kopf, teile


def erster_beitrag(kopf, formatname, link):
    j, m, t = str(kopf["date"]).split("-")
    titel = str(kopf["title"])

    def basis(titel):
        return f"Neu im Lesesaal: {formatname}\n\n{titel}\n{kopf.get('quelle', '')}, {t}.{m}.{j}\n\n{link}"

    zu_lang = laenge(basis(titel)) - GRENZE
    if zu_lang > 0:
        titel = titel[: len(titel) - zu_lang - 1] + "…"
    text = basis(titel)

    tags = []
    for tag in kopf.get("tags", []):
        tag = re.sub(r"\W", "", str(tag))
        if not tag or tag.isdigit() or tag.lower() in (x.lower() for x in tags):
            continue
        if laenge(text + "\n\n" + " ".join(f"#{x}" for x in tags + [tag])) <= GRENZE:
            tags.append(tag)
    if tags:
        text += "\n\n" + " ".join(f"#{x}" for x in tags)
    return text


def antworten(teile):
    beitraege = []
    for teil, eintraege in teile.items():
        aktuell = f"{teil}:"
        for eintrag in eintraege:
            zeile = f"\n- {eintrag}"
            if laenge(aktuell + zeile) > GRENZE:
                beitraege.append(aktuell)
                aktuell = f"{teil}, Fortsetzung:"
            aktuell += zeile
        beitraege.append(aktuell)
    return beitraege


def poste(adresse, token, text, antwort_auf, schluessel):
    daten = {"status": text, "visibility": SICHTBARKEIT, "language": "de"}
    if antwort_auf:
        daten["in_reply_to_id"] = antwort_auf
    antwort = requests.post(
        f"{adresse}/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": schluessel},
        data=daten,
        timeout=60,
    )
    if not antwort.ok:
        raise SystemExit(f"Mastodon meldet {antwort.status_code}: {antwort.text[:300]}")
    return antwort.json()


def main():
    parser = argparse.ArgumentParser(description="Hinweis auf eine Notiz bei Mastodon posten")
    parser.add_argument("notiz", type=Path, help="Markdown-Datei unter NOTIZEN/inhalt/notizen/")
    parser.add_argument("--senden", action="store_true", help="wirklich posten")
    parser.add_argument("--neu", action="store_true", help="auch posten, wenn schon gepostet")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)
    with open(PROJEKT / "seite" / "hugo.toml", "rb") as datei:
        formate = tomllib.load(datei)["params"]["formate"]
    env = lies_env()

    kopf, teile = lies_notiz(args.notiz)
    link = f"{konfiguration['seite']['adresse'].rstrip('/')}/{env['GEHEIMER_PFAD']}/notizen/{args.notiz.stem.lower()}/"
    beitraege = [erster_beitrag(kopf, formate.get(kopf.get("format"), "Notiz"), link)] + antworten(teile)

    for nr, text in enumerate(beitraege, 1):
        print(f"--- Beitrag {nr} von {len(beitraege)}, {laenge(text)} Zeichen ---\n{text}\n")
    online = requests.head(link, timeout=30).status_code == 200
    print(f"Notiz online: {'ja' if online else 'nein'}")

    if not args.senden:
        print("Nur angezeigt. Zum Posten mit --senden aufrufen.")
        return
    if not online:
        raise SystemExit("Die Notiz ist noch nicht online. Erst bauen.py --hochladen.")

    stand_pfad = Path(konfiguration["pfade"]["daten"]) / "gepostet.json"
    stand = json.loads(stand_pfad.read_text(encoding="utf-8")) if stand_pfad.exists() else {}
    if args.notiz.stem in stand and not args.neu:
        raise SystemExit(f"Schon gepostet: {stand[args.notiz.stem]}. Mit --neu erneut posten.")

    adresse, token = env["MASTODON_ADRESSE"].rstrip("/"), env["MASTODON_TOKEN"]
    lauf, vorher = int(time.time()), None
    for nr, text in enumerate(beitraege, 1):
        status = poste(adresse, token, text, vorher, f"lesesaal-{args.notiz.stem}-{lauf}-{nr}")
        vorher = status["id"]
        if nr == 1:
            stand[args.notiz.stem] = status["url"]
            stand_pfad.write_text(json.dumps(stand, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Beitrag {nr} gepostet")
    print(f"Thread: {stand[args.notiz.stem]}")


if __name__ == "__main__":
    main()
