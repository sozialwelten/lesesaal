"""Hinweis auf eine Notiz bei Mastodon posten.

Aufruf:
  .venv/bin/python posten.py NOTIZ.md            zeigt die Beiträge nur an
  .venv/bin/python posten.py NOTIZ.md --senden   postet sie als Thread
  .venv/bin/python posten.py NOTIZ.md --senden --neu
                                                 postet auch, wenn die Notiz schon gepostet wurde

  .venv/bin/python posten.py NOTIZ.md --senden --fortsetzen
                                                 einen schon begonnenen Thread vervollständigen, auch wenn
                                                 er nur in gepostet.json steht (aus der Zeit vor threads.json)

Der erste Beitrag nennt Format, Titel, Quelle und Datum, verlinkt die Notiz
und trägt die Hashtags. Die Einträge der Notiz folgen als Antworten, je Teil
der Notiz ein Beitrag, bei Bedarf mehrere. Sichtbarkeit: nur Follower.

Ein abgebrochener Thread wird beim nächsten Aufruf fortgesetzt, nicht neu begonnen:
DATEN/threads.json hält je Notiz die Kennungen der schon geposteten Beiträge und
ihre Gesamtzahl. Fehlt dort ein Eintrag, sucht das Skript den ersten Beitrag über
gepostet.json oder unter den letzten Beiträgen des Kontos (am Link zur Notiz) und
liest die Antworten darunter bei Mastodon nach. Jeder Beitrag trägt einen festen
Idempotency-Key aus Notiz und Nummer.

Netz- und Serverfehler wiederholt mastodon_api.py. Scheitert es trotzdem, endet das
Skript mit Exit-Code 4 (VORUEBERGEHEND), und der Tagesbetrieb versucht es beim
nächsten Lauf erneut, ohne das als Fehlversuch zu zählen.

Aus der .env: MASTODON_ADRESSE, MASTODON_TOKEN, GEHEIMER_PFAD
Bereits gepostete Notizen stehen in DATEN/gepostet.json (Adresse des ersten Beitrags).
"""

import argparse
import json
import re
import tomllib
from pathlib import Path

from bauen import PROJEKT, lies_env
from mastodon_api import VORUEBERGEHEND, Voruebergehend, anfrage

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
        return f"Neu im Lesesaal: {formatname}\n\n{titel}\n{kopf.get('quelle_name') or kopf.get('quelle', '')}, {t}.{m}.{j}\n\n{link}"

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
    antwort = anfrage("POST", f"{adresse}/api/v1/statuses", token, idempotenz=schluessel, data=daten)
    if not antwort.ok:
        raise SystemExit(f"Mastodon meldet {antwort.status_code}: {antwort.text[:300]}")
    return antwort.json()


def lies_json(pfad):
    return json.loads(pfad.read_text(encoding="utf-8")) if pfad.exists() else {}


def schreibe_json(pfad, daten):
    tmp = pfad.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(pfad)


def hole(adresse, token, pfad, **params):
    antwort = anfrage("GET", f"{adresse}{pfad}", token, params=params)
    if not antwort.ok:
        raise SystemExit(f"Mastodon meldet {antwort.status_code}: {antwort.text[:300]}")
    return antwort.json()


def erster_suchen(adresse, token, url_alt, notizpfad):
    """Kennung des ersten Beitrags: aus der Adresse in gepostet.json oder unter den letzten 40 Beiträgen."""
    if url_alt:
        kennung = str(url_alt).rstrip("/").rsplit("/", 1)[-1]
        if kennung.isdigit():
            return kennung
    konto = hole(adresse, token, "/api/v1/accounts/verify_credentials")
    for status in hole(adresse, token, f"/api/v1/accounts/{konto['id']}/statuses", limit=40, exclude_replies="true"):
        if notizpfad in status.get("content", ""):
            return status["id"]
    return None


def kette(adresse, token, erster):
    """Kennungen des Threads: der erste Beitrag und die Antworten des Kontos, jede auf die vorige."""
    kennungen = [erster]
    nachfolger = {}
    konto = hole(adresse, token, "/api/v1/accounts/verify_credentials")["id"]
    for status in hole(adresse, token, f"/api/v1/statuses/{erster}/context").get("descendants", []):
        if status.get("account", {}).get("id") == konto:
            nachfolger.setdefault(status.get("in_reply_to_id"), status["id"])
    while kennungen[-1] in nachfolger:
        kennungen.append(nachfolger[kennungen[-1]])
    return kennungen


def main():
    parser = argparse.ArgumentParser(description="Hinweis auf eine Notiz bei Mastodon posten")
    parser.add_argument("notiz", type=Path, help="Markdown-Datei unter NOTIZEN/inhalt/notizen/")
    parser.add_argument("--senden", action="store_true", help="wirklich posten")
    parser.add_argument("--neu", action="store_true", help="auch posten, wenn schon gepostet")
    parser.add_argument("--fortsetzen", action="store_true",
                        help="schon begonnenen Thread vervollständigen, auch ohne Eintrag in threads.json")
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
    online = anfrage("HEAD", link, pausen=(5, 15), timeout=30).status_code == 200
    print(f"Notiz online: {'ja' if online else 'nein'}")

    if not args.senden:
        print("Nur angezeigt. Zum Posten mit --senden aufrufen.")
        return
    if not online:
        raise SystemExit("Die Notiz ist noch nicht online. Erst bauen.py --hochladen.")

    daten = Path(konfiguration["pfade"]["daten"])
    stand_pfad, threads_pfad = daten / "gepostet.json", daten / "threads.json"
    stand, threads = lies_json(stand_pfad), lies_json(threads_pfad)
    name = args.notiz.stem
    adresse, token = env["MASTODON_ADRESSE"].rstrip("/"), env["MASTODON_TOKEN"]

    if args.neu:
        kennungen = []
    elif name in threads:
        kennungen = list(threads[name]["ids"])
    elif name in stand and not args.fortsetzen:
        raise SystemExit(f"Schon gepostet: {stand[name]}. Mit --fortsetzen vervollständigen, mit --neu erneut posten.")
    else:
        erster = erster_suchen(adresse, token, stand.get(name), f"/notizen/{name.lower()}/")
        kennungen = kette(adresse, token, erster) if erster else []
        if kennungen:
            print(f"Thread gefunden mit {len(kennungen)} von {len(beitraege)} Beiträgen")

    if len(kennungen) >= len(beitraege):
        threads[name] = {"ids": kennungen, "gesamt": len(beitraege)}
        schreibe_json(threads_pfad, threads)
        print(f"Schon vollständig gepostet ({len(kennungen)} Beiträge).")
        return
    for nr in range(len(kennungen) + 1, len(beitraege) + 1):
        status = poste(adresse, token, beitraege[nr - 1], kennungen[-1] if kennungen else None,
                       f"lesesaal-{name}-{nr}" + ("-neu" if args.neu else ""))
        kennungen.append(status["id"])
        threads[name] = {"ids": kennungen, "gesamt": len(beitraege)}
        schreibe_json(threads_pfad, threads)
        if nr == 1:
            stand[name] = status["url"]
            schreibe_json(stand_pfad, stand)
        print(f"Beitrag {nr} gepostet")
    print(f"Thread: {stand.get(name, '?')}")


if __name__ == "__main__":
    try:
        main()
    except Voruebergehend as fehler:
        print(f"Mastodon nicht erreichbar: {fehler}", flush=True)
        raise SystemExit(VORUEBERGEHEND) from None
