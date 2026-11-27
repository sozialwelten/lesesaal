"""Einen Text als Direktnachricht über das Konto des Lesesaals schicken.

Aufruf:
  echo "Text" | .venv/bin/python nachricht.py [--kennung KENNUNG]

Empfangskonto ist [betrieb] bericht_an aus config.toml, der Zugang kommt aus der .env wie bei posten.py
(MASTODON_ADRESSE, MASTODON_TOKEN). Darüber schicken andere Programme auf demselben Rechner ihre Berichte,
etwa der Dirigent am Ende jedes Laufs. Der Text wird auf 490 Zeichen gekürzt. Mit --kennung schickt ein
zweiter Aufruf mit derselben Kennung nichts doppelt. Netz- und Serverfehler wiederholt mastodon_api.py,
danach endet das Skript mit Exit-Code 4.
"""

import argparse
import sys
import tomllib

from bauen import PROJEKT, lies_env
from mastodon_api import VORUEBERGEHEND, Voruebergehend, anfrage


def main():
    parser = argparse.ArgumentParser(description="Text von der Standardeingabe als Direktnachricht schicken")
    parser.add_argument("--kennung", help="Kennung gegen doppelte Nachrichten (Idempotency-Key)")
    args = parser.parse_args()

    text = sys.stdin.read().strip()
    if not text:
        raise SystemExit("Kein Text auf der Standardeingabe.")
    with open(PROJEKT / "config.toml", "rb") as datei:
        an = tomllib.load(datei).get("betrieb", {}).get("bericht_an", "sozialwelten")
    text = f"@{an} {text}"
    if len(text) > 490:
        text = text[:488].rstrip() + " …"

    env = lies_env()
    try:
        antwort = anfrage("POST", f"{env['MASTODON_ADRESSE'].rstrip('/')}/api/v1/statuses", env["MASTODON_TOKEN"],
                          idempotenz=args.kennung, data={"status": text, "visibility": "direct", "language": "de"})
    except Voruebergehend as fehler:
        print(f"Mastodon nicht erreichbar: {fehler}")
        raise SystemExit(VORUEBERGEHEND) from None
    antwort.raise_for_status()
    print("Direktnachricht verschickt.")


if __name__ == "__main__":
    main()
