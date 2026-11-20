"""Feeds abfragen und Folgen herunterladen.

Aufruf:
  .venv/bin/python holen.py QUELLE            zeigt die neuesten Folgen
  .venv/bin/python holen.py QUELLE --laden 3  lädt Folge Nr. 3 der Liste

Neben jede Audiodatei kommt <name>.meta.json mit Titel, Datum, Folgenseite und Quelle.

Quellen mit typ = "youtube" und kanal_id lesen den RSS-Feed des Kanals. Shorts werden
übersprungen. Geladen wird nur die Tonspur, mit yt-dlp. Vorher prüft yt-dlp, ob das Video
gerade live oder angekündigt ist (dann später erneut versuchen), ob es ein aufgezeichneter
Livestream ist (überspringen bei ohne_livestreams = true) und ob es länger ist als
hoechstdauer_minuten (überspringen).

Quellen mit typ = "tiktok" und konto listen die neuesten Videos über yt-dlp, weil TikTok keinen
Feed hat. Geladen wird wie bei YouTube. Bei Videos bleibt die Angabe url
leer, damit die Notiz keinen Audio-Player bekommt. Die Zeitmarken verlinken dann über
sprunglink auf das Video.
"""

import argparse
import json
import re
import subprocess
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


class NochNichtFertig(Exception):
    """Das Video läuft gerade live oder ist angekündigt. Später erneut versuchen."""


class Uebersprungen(Exception):
    """Das Video passt nach den Regeln der Quelle nicht und wird dauerhaft übersprungen."""


def feed_adresse(quelle):
    if quelle.get("typ") == "youtube":
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={quelle['kanal_id']}"
    return quelle["feed"]


def lies_feed(quelle):
    if quelle.get("typ") == "youtube":
        return lies_youtube(quelle)
    if quelle.get("typ") == "tiktok":
        return lies_tiktok(quelle)
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


def lies_youtube(quelle):
    adresse = feed_adresse(quelle)
    feed = feedparser.parse(adresse, agent=KENNUNG_PROGRAMM)
    if not feed.entries:
        raise SystemExit(f"Feed leer oder nicht lesbar: {adresse}")
    folgen = []
    for eintrag in feed.entries:
        seite = eintrag.get("link", "")
        if "/shorts/" in seite:
            continue
        datum = eintrag.get("published_parsed")
        folgen.append({
            "titel": eintrag.get("title", "ohne Titel"),
            "datum": time.strftime("%Y-%m-%d", datum) if datum else "",
            "dauer": "",
            "bytes": 0,
            "url": "",
            "seite": seite,
            "video": True,
            "endung": ".m4a",
        })
    return folgen


def lies_tiktok(quelle, anzahl=30):
    adresse = f"https://www.tiktok.com/@{quelle['konto']}"
    ergebnis = subprocess.run(
        ["yt-dlp", "--quiet", "--no-warnings", "--flat-playlist", "--playlist-items", f"1:{anzahl}",
         "--print", "%(timestamp)s\t%(duration)s\t%(webpage_url)s\t%(title)s", adresse],
        capture_output=True, text=True, timeout=300,
    )
    folgen = []
    for zeile in ergebnis.stdout.splitlines():
        teile = zeile.split("\t", 3)
        if len(teile) < 4 or not teile[0].isdigit():
            continue
        stempel, dauer, seite, titel = teile
        folgen.append({
            "titel": " ".join(titel.split())[:150] or "ohne Titel",
            "datum": time.strftime("%Y-%m-%d", time.gmtime(int(stempel))),
            "dauer": dauer if dauer != "NA" else "",
            "bytes": 0,
            "url": "",
            "seite": seite,
            "video": True,
            "endung": ".mp4",
        })
    if not folgen:
        fehler = (ergebnis.stderr or "keine Videos gefunden").strip()[-300:]
        raise SystemExit(f"TikTok {quelle['konto']}: {fehler}")
    return folgen


def dateiname(folge):
    titel = folge["titel"].lower()
    for alt, neu in {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}.items():
        titel = titel.replace(alt, neu)
    kurz = re.sub(r"[^a-z0-9]+", "-", titel).strip("-")[:60]
    endung = folge.get("endung") or Path(folge["url"].split("?")[0]).suffix or ".mp3"
    return f"{folge['datum']}_{kurz}{endung}"


def lade(folge, ziel, quelle=None):
    if folge.get("video"):
        return lade_video(folge, ziel, quelle or {})
    ziel.parent.mkdir(parents=True, exist_ok=True)
    teil = ziel.with_suffix(ziel.suffix + ".teil")
    kopf = {"User-Agent": KENNUNG_PROGRAMM}
    with requests.get(folge["url"], stream=True, timeout=60, headers=kopf) as antwort:
        antwort.raise_for_status()
        with open(teil, "wb") as datei:
            for stueck in antwort.iter_content(chunk_size=1 << 20):
                datei.write(stueck)
    teil.rename(ziel)


def pruefe_video(folge, quelle):
    """Fragt bei YouTube Status und Dauer ab, ohne zu laden. Wirft NochNichtFertig oder Uebersprungen."""
    ergebnis = subprocess.run(
        ["yt-dlp", "--quiet", "--no-warnings", "--no-playlist", "--skip-download",
         "--print", "%(live_status)s|%(duration)s", folge["seite"]],
        capture_output=True, text=True, timeout=120,
    )
    if ergebnis.returncode != 0:
        fehler = (ergebnis.stderr or ergebnis.stdout).strip()
        if "live event" in fehler.lower() or "premiere" in fehler.lower():
            raise NochNichtFertig("noch nicht erschienen")
        raise RuntimeError(f"yt-dlp: {fehler[-300:]}")
    status, _, dauer = ergebnis.stdout.strip().splitlines()[-1].partition("|")
    if status in ("is_live", "is_upcoming", "post_live"):
        raise NochNichtFertig(f"Status {status}")
    if status == "was_live" and quelle.get("ohne_livestreams"):
        raise Uebersprungen("aufgezeichneter Livestream")
    grenze = quelle.get("hoechstdauer_minuten")
    if grenze and dauer.replace(".", "", 1).isdigit() and float(dauer) > grenze * 60:
        raise Uebersprungen(f"{float(dauer) / 60:.0f} Minuten, länger als {grenze}")


def lade_video(folge, ziel, quelle):
    """Lädt nur die Tonspur eines Videos mit yt-dlp, nach der Prüfung."""
    pruefe_video(folge, quelle)
    ziel.parent.mkdir(parents=True, exist_ok=True)
    teil = ziel.with_suffix(ziel.suffix + ".teil")
    ergebnis = subprocess.run(
        ["yt-dlp", "--quiet", "--no-warnings", "--no-playlist", "--match-filter", "!is_live",
         "-f", "bestaudio[ext=m4a]/bestaudio/best",
         "--force-overwrites", "-o", str(teil), folge["seite"]],
        capture_output=True, text=True,
    )
    if ergebnis.returncode != 0 or not teil.exists():
        raise RuntimeError(f"yt-dlp: {(ergebnis.stderr or ergebnis.stdout).strip()[-300:]}")
    teil.rename(ziel)


def schreibe_meta(folge, quelle, ziel):
    meta = dict(folge)
    meta.update({
        "quelle": quelle["kennung"],
        "quelle_name": quelle["name"],
        "zweck": quelle["zweck"],
        "sprache": quelle.get("sprache", ""),
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
        try:
            lade(folge, ziel, quelle)
        except (NochNichtFertig, Uebersprungen) as grund:
            raise SystemExit(f"Nicht geladen: {grund}") from None
        print(f"Gespeichert: {ziel} ({ziel.stat().st_size / 1_000_000:.1f} MB)")
    print(f"Angaben: {schreibe_meta(folge, quelle, ziel)}")


if __name__ == "__main__":
    main()
