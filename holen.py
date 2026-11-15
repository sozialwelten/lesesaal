"""Feeds abfragen und Folgen herunterladen.

Aufruf:
  .venv/bin/python holen.py QUELLE            zeigt die neuesten Folgen
  .venv/bin/python holen.py QUELLE --laden 3  lädt Folge Nr. 3 der Liste

Neben jede Audiodatei kommt <name>.meta.json mit Titel, Datum, Folgenseite und Quelle.
"""

import argparse
import json
import re
import time
import tomllib
from pathlib import Path

import feedparser
import requests

PROJEKT = Path(__file__).resolve().parent
KENNUNG_PROGRAMM = "lesesaal/0.1"


def lade_konfiguration():
    with open(PROJEKT / "config.toml", "rb") as datei:
        return tomllib.load(datei)


def finde_quelle(konfiguration, kennung):
    for quelle in konfiguration["quelle"]:
        if quelle["kennung"] == kennung:
            return quelle
    bekannte = ", ".join(q["kennung"] for q in konfiguration["quelle"])
    raise SystemExit(f"Quelle '{kennung}' fehlt in config.toml. Bekannt: {bekannte}")


def lies_feed(quelle):
    feed = feedparser.parse(quelle["feed"], agent=KENNUNG_PROGRAMM)
    if not feed.entries:
        raise SystemExit(f"Feed leer oder nicht lesbar: {quelle['feed']}")
    folgen = []
    for eintrag in feed.entries:
        audio = next((l for l in eintrag.get("links", []) if l.get("rel") == "enclosure"), None)
        if audio is None:
            continue
        datum = eintrag.get("published_parsed")
        folgen.append({
            "titel": eintrag.get("title", "ohne Titel"),
            "datum": time.strftime("%Y-%m-%d", datum) if datum else "",
            "dauer": eintrag.get("itunes_duration", ""),
            "bytes": int(audio.get("length") or 0),
            "url": audio["href"],
            "seite": eintrag.get("link", ""),
        })
    return folgen


def dateiname(folge):
    titel = folge["titel"].lower()
    for alt, neu in {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}.items():
        titel = titel.replace(alt, neu)
    kurz = re.sub(r"[^a-z0-9]+", "-", titel).strip("-")[:60]
    endung = Path(folge["url"].split("?")[0]).suffix or ".mp3"
    return f"{folge['datum']}_{kurz}{endung}"


def lade(folge, ziel):
    ziel.parent.mkdir(parents=True, exist_ok=True)
    teil = ziel.with_suffix(ziel.suffix + ".teil")
    kopf = {"User-Agent": KENNUNG_PROGRAMM}
    with requests.get(folge["url"], stream=True, timeout=60, headers=kopf) as antwort:
        antwort.raise_for_status()
        with open(teil, "wb") as datei:
            for stueck in antwort.iter_content(chunk_size=1 << 20):
                datei.write(stueck)
    teil.rename(ziel)


def schreibe_meta(folge, quelle, ziel):
    meta = dict(folge)
    meta.update({
        "quelle": quelle["kennung"],
        "quelle_name": quelle["name"],
        "zweck": quelle["zweck"],
        "sprache": quelle["sprache"],
    })
    pfad = ziel.with_name(ziel.stem + ".meta.json")
    pfad.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return pfad


def main():
    parser = argparse.ArgumentParser(description="Feed abfragen und Folgen laden")
    parser.add_argument("quelle", help="Kennung der Quelle aus config.toml")
    parser.add_argument("--anzahl", type=int, default=10, help="wie viele Folgen anzeigen")
    parser.add_argument("--laden", type=int, metavar="NR", help="Folge mit dieser Nummer laden")
    args = parser.parse_args()

    konfiguration = lade_konfiguration()
    quelle = finde_quelle(konfiguration, args.quelle)
    folgen = lies_feed(quelle)

    if args.laden is None:
        for nr, folge in enumerate(folgen[: args.anzahl], start=1):
            mb = folge["bytes"] / 1_000_000
            print(f"{nr:>3}  {folge['datum']}  {folge['dauer']:>8}  {mb:6.1f} MB  {folge['titel']}")
        return

    if not 1 <= args.laden <= len(folgen):
        raise SystemExit(f"Nummer {args.laden} gibt es nicht, der Feed hat {len(folgen)} Folgen.")
    folge = folgen[args.laden - 1]
    ziel = Path(konfiguration["pfade"]["daten"]) / "audio" / quelle["kennung"] / dateiname(folge)
    if ziel.exists():
        print(f"Schon vorhanden: {ziel}")
    else:
        print(f"Lade {folge['titel']} ...")
        lade(folge, ziel)
        print(f"Gespeichert: {ziel} ({ziel.stat().st_size / 1_000_000:.1f} MB)")
    print(f"Angaben: {schreibe_meta(folge, quelle, ziel)}")


if __name__ == "__main__":
    main()
