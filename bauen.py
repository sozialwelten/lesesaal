"""Seite bauen, Suche erzeugen und auf den Webspace laden.

Aufruf:
  .venv/bin/python bauen.py              baut nach DATEN/seite/
  .venv/bin/python bauen.py --vorschau   lokale Vorschau unter http://localhost:1313/ (ohne Suche)
  .venv/bin/python bauen.py --hochladen  baut und lädt per FTPS hoch

Aus der .env im Projektordner:
  FTP_HOST, FTP_BENUTZER, FTP_PASSWORT, GEHEIMER_PFAD

Auf dem Webspace entsteht:
  /index.html          leere Seite
  /robots.txt          sperrt Suchmaschinen aus
  /GEHEIMER_PFAD/      die eigentliche Seite
"""

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path

PROJEKT = Path(__file__).resolve().parent


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
        subprocess.run(
            ["hugo", "server", "--source", str(quelle), "--contentDir", str(inhalt),
             "--baseURL", "http://localhost:1313/"],
            check=False,
        )
        return

    env = lies_env()
    geheim = env["GEHEIMER_PFAD"]
    adresse = f"{konfiguration['seite']['adresse'].rstrip('/')}/{geheim}/"
    wurzel = Path(konfiguration["pfade"]["daten"]) / "seite"
    ziel = wurzel / geheim

    subprocess.run(
        ["hugo", "--source", str(quelle), "--contentDir", str(inhalt), "--destination", str(ziel),
         "--baseURL", adresse, "--cleanDestinationDir", "--minify", "--quiet"],
        check=True,
    )
    subprocess.run(["pagefind", "--site", str(ziel), "--silent"], check=True)
    (wurzel / "index.html").write_text("<!doctype html><title></title>\n", encoding="utf-8")
    (wurzel / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    seiten = len(list(ziel.rglob("*.html")))
    print(f"Gebaut: {ziel} ({seiten} HTML-Seiten)")

    if not args.hochladen:
        return

    befehle = (
        "set ftp:ssl-force true; set ftp:ssl-protect-data true; set ssl:verify-certificate true; "
        f"mirror --reverse --delete --verbose=1 '{wurzel}/' /; quit"
    )
    umgebung = dict(os.environ, LFTP_PASSWORD=env["FTP_PASSWORT"])
    subprocess.run(
        ["lftp", "--env-password", "-u", env["FTP_BENUTZER"], env["FTP_HOST"], "-e", befehle],
        check=True, env=umgebung,
    )
    print(f"Hochgeladen: {adresse}")


if __name__ == "__main__":
    main()
