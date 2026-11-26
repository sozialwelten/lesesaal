"""Seite bauen, Suche erzeugen und auf den Webspace laden.

Aufruf:
  .venv/bin/python bauen.py              baut nach DATEN/seite/
  .venv/bin/python bauen.py --vorschau   lokale Vorschau unter http://localhost:1313/ (ohne Suche)
  .venv/bin/python bauen.py --hochladen  baut und lädt per FTPS hoch

Exit-Codes: 0 fertig, 3 gebaut, aber das Hochladen ist gescheitert (Netz oder Webspace),
sonst ist das Bauen gescheitert.

Aus der .env im Projektordner:
  FTP_HOST, FTP_BENUTZER, FTP_PASSWORT, GEHEIMER_PFAD

Auf dem Webspace entsteht:
  /index.html          leere Seite
  /robots.txt          sperrt Suchmaschinen aus
  /GEHEIMER_PFAD/      die eigentliche Seite

Hugo baut jedes Mal komplett neu nach DATEN/seite_bau/GEHEIMER_PFAD/. Hugos
--cleanDestinationDir räumt ohne Ordner für statische Dateien nicht auf, deshalb wird
dieser Ordner vorher gelöscht. Danach wird DATEN/seite/ mit dem frischen Bau abgeglichen:
Nur Dateien mit anderem Inhalt werden neu geschrieben, Dateien ohne Gegenstück gelöscht.
Unveränderte Dateien behalten so ihre alte Änderungszeit, und lftp lädt mit --only-newer
nur hoch, was sich geändert hat. Seiten, die es nicht mehr gibt, verschwinden trotzdem
aus Suchindex und Webspace.

Das Hochladen läuft mit vier Übertragungen gleichzeitig und endet spätestens nach
[seite] hochladen_hoechstens_minuten (Vorgabe 20) mit Exit-Code 3.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

PROJEKT = Path(__file__).resolve().parent
HOCHLADEN_GESCHEITERT = 3
HOCHLADEN_HOECHSTENS_MINUTEN = 20


def lies_env():
    werte = {}
    pfad = PROJEKT / ".env"
    if not pfad.exists():
        raise SystemExit("Keine .env im Projektordner.")
    for zeile in pfad.read_text(encoding="utf-8").splitlines():
        zeile = zeile.strip()
        if zeile and not zeile.startswith("#") and "=" in zeile:
            schluessel, wert = zeile.split("=", 1)
            werte[schluessel.strip()] = wert.strip()
    return werte


def abgleichen(neu, ziel):
    """Bringt ziel auf den Inhalt von neu. Schreibt nur Dateien mit anderem Inhalt neu.

    Gibt (geschrieben, geloescht) zurück."""
    geschrieben = geloescht = 0
    ziel.mkdir(parents=True, exist_ok=True)
    vorhanden = set()
    for datei in sorted(neu.rglob("*")):
        relativ = datei.relative_to(neu)
        vorhanden.add(relativ)
        dort = ziel / relativ
        if datei.is_dir():
            if dort.exists() and not dort.is_dir():
                dort.unlink()
            dort.mkdir(exist_ok=True)
            continue
        if dort.is_dir():
            shutil.rmtree(dort)
        if dort.exists() and dort.stat().st_size == datei.stat().st_size and dort.read_bytes() == datei.read_bytes():
            continue
        shutil.copyfile(datei, dort)
        geschrieben += 1
    for datei in sorted(ziel.rglob("*"), reverse=True):
        if datei.relative_to(ziel) in vorhanden:
            continue
        if datei.is_dir():
            shutil.rmtree(datei)
        else:
            datei.unlink()
            geloescht += 1
    return geschrieben, geloescht


def schreibe_wenn_anders(pfad, text):
    if not pfad.exists() or pfad.read_text(encoding="utf-8") != text:
        pfad.write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Lesesaal-Seite bauen und hochladen")
    parser.add_argument("--vorschau", action="store_true", help="lokale Vorschau starten")
    parser.add_argument("--hochladen", action="store_true", help="nach dem Bauen hochladen")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)
    inhalt = Path(konfiguration["pfade"]["notizen"]) / "inhalt"
    quelle = PROJEKT / "seite"

    if args.vorschau:
        try:
            subprocess.run(
                ["hugo", "server", "--source", str(quelle), "--contentDir", str(inhalt),
                 "--baseURL", "http://localhost:1313/"],
                check=False,
            )
        except KeyboardInterrupt:
            print("\nVorschau beendet.")
        return

    env = lies_env()
    geheim = env["GEHEIMER_PFAD"]
    adresse = f"{konfiguration['seite']['adresse'].rstrip('/')}/{geheim}/"
    daten = Path(konfiguration["pfade"]["daten"])
    wurzel = daten / "seite"
    ziel = wurzel / geheim
    neu = daten / "seite_bau" / geheim
    if not geheim.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("GEHEIMER_PFAD in der .env fehlt oder ist kein einfacher Ordnername.")
    if neu.exists():
        shutil.rmtree(neu)

    subprocess.run(
        ["hugo", "--source", str(quelle), "--contentDir", str(inhalt), "--destination", str(neu),
         "--baseURL", adresse, "--minify", "--quiet"],
        check=True,
    )
    subprocess.run(["pagefind", "--site", str(neu), "--silent"], check=True)
    geschrieben, geloescht = abgleichen(neu, ziel)
    schreibe_wenn_anders(wurzel / "index.html", "<!doctype html><title></title>\n")
    schreibe_wenn_anders(wurzel / "robots.txt", "User-agent: *\nDisallow: /\n")
    seiten = len(list(ziel.rglob("*.html")))
    print(f"Gebaut: {ziel} ({seiten} HTML-Seiten, geändert {geschrieben}, gelöscht {geloescht})")

    if not args.hochladen:
        return

    befehle = (
        "set ftp:ssl-force true; set ftp:ssl-protect-data true; set ssl:verify-certificate true; set net:timeout 30; set net:max-retries 3; set net:reconnect-interval-base 10; set net:reconnect-interval-max 60; "
        f"mirror --reverse --delete --only-newer --parallel=4 --verbose=1 '{wurzel}/' /; quit"
    )
    umgebung = dict(os.environ, LFTP_PASSWORD=env["FTP_PASSWORT"])
    minuten = float(konfiguration.get("seite", {}).get("hochladen_hoechstens_minuten", HOCHLADEN_HOECHSTENS_MINUTEN))
    try:
        subprocess.run(
            ["lftp", "--env-password", "-u", env["FTP_BENUTZER"], env["FTP_HOST"], "-e", befehle],
            check=True, env=umgebung, timeout=minuten * 60,
        )
    except subprocess.TimeoutExpired:
        print(f"Hochladen gescheitert: nach {minuten:g} Minuten abgebrochen", file=sys.stderr)
        sys.exit(HOCHLADEN_GESCHEITERT)
    except (OSError, subprocess.CalledProcessError) as fehler:
        print(f"Hochladen gescheitert: {fehler}", file=sys.stderr)
        sys.exit(HOCHLADEN_GESCHEITERT)
    print(f"Hochgeladen: {adresse}")


if __name__ == "__main__":
    main()
