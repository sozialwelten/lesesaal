"""Profilfelder des Lesesaal-Kontos setzen, etwa für Fortschrittsbalken.

Aufruf:
  printf 'Name|Wert\\nName|Wert\\n' | .venv/bin/python profil.py [--probe]

Je Zeile ein Feld „Name|Wert“, höchstens vier. Ein vorhandenes Feld mit demselben Namen wird
ersetzt, andere vorhandene Felder bleiben stehen. Ergäben sich mehr als vier Felder, ändert das
Skript nichts und endet mit einem Fehler. Mit --probe zeigt es nur, was es schreiben würde.

Der Zugang kommt aus der .env wie bei posten.py (MASTODON_ADRESSE, MASTODON_TOKEN). Das Token
braucht dafür zusätzlich die Berechtigung write:accounts. Darüber setzt etwa der Dirigent am
Ende jedes Laufs den Stand der großen Arbeiten ins Profil.
"""

import argparse
import sys

import requests

from bauen import lies_env

HOECHSTENS = 4
LAENGE = 255


def lies_felder(text):
    felder = []
    for zeile in text.splitlines():
        if not zeile.strip():
            continue
        name, trenner, wert = zeile.partition("|")
        name, wert = name.strip(), wert.strip()
        if not trenner or not name or not wert:
            raise SystemExit(f"Zeile ohne „Name|Wert“: {zeile}")
        if len(name) > LAENGE or len(wert) > LAENGE:
            raise SystemExit(f"Feld länger als {LAENGE} Zeichen: {name}")
        felder.append((name, wert))
    if not felder:
        raise SystemExit("Keine Felder auf der Standardeingabe.")
    return felder


def zusammenfuehren(vorhanden, neu):
    """Neue Felder ersetzen gleichnamige an ihrer Stelle, die übrigen kommen hinten dazu."""
    ergebnis = list(vorhanden)
    for name, wert in neu:
        for nr, (alt_name, _) in enumerate(ergebnis):
            if alt_name == name:
                ergebnis[nr] = (name, wert)
                break
        else:
            ergebnis.append((name, wert))
    return ergebnis


def main():
    parser = argparse.ArgumentParser(description="Profilfelder des Lesesaal-Kontos setzen")
    parser.add_argument("--probe", action="store_true", help="nur anzeigen, nichts ändern")
    args = parser.parse_args()

    neu = lies_felder(sys.stdin.read())
    env = lies_env()
    adresse = env["MASTODON_ADRESSE"].rstrip("/")
    kopf = {"Authorization": f"Bearer {env['MASTODON_TOKEN']}"}
    antwort = requests.get(f"{adresse}/api/v1/accounts/verify_credentials", headers=kopf, timeout=60)
    antwort.raise_for_status()
    konto = antwort.json()
    vorhanden = [(f["name"], f["value"]) for f in konto.get("source", {}).get("fields", [])]
    felder = zusammenfuehren(vorhanden, neu)
    if len(felder) > HOECHSTENS:
        namen = ", ".join(n for n, _ in vorhanden)
        raise SystemExit(f"Es wären {len(felder)} Felder, erlaubt sind {HOECHSTENS}. Vorhanden: {namen}")

    print(f"Konto @{konto.get('acct', '?')}, {len(felder)} Felder:")
    for name, wert in felder:
        print(f"  {name}: {wert}")
    if args.probe:
        print("Probe, nichts geändert.")
        return

    daten = {}
    for nr, (name, wert) in enumerate(felder):
        daten[f"fields_attributes[{nr}][name]"] = name
        daten[f"fields_attributes[{nr}][value]"] = wert
    antwort = requests.patch(f"{adresse}/api/v1/accounts/update_credentials", headers=kopf, data=daten, timeout=60)
    if antwort.status_code == 403:
        raise SystemExit("Abgelehnt (403). Hat das Token die Berechtigung write:accounts?")
    antwort.raise_for_status()
    print("Profil aktualisiert.")


if __name__ == "__main__":
    main()
