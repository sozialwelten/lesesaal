"""Alle Threads des Lesesaal-Kontos bei Mastodon löschen, etwa bevor der Stand neu aufgebaut wird.

Aufruf:
  .venv/bin/python threads_loeschen.py             zeigt nur an, was gelöscht würde
  .venv/bin/python threads_loeschen.py --loeschen  löscht wirklich

Gelöscht werden alle eigenen Beiträge außer Direktnachrichten. Die Tagesberichte bleiben also stehen.
Mastodon erlaubt nur etwa 30 Löschungen pro halbe Stunde. Ist die Grenze erreicht, wartet das
Programm, bis wieder gelöscht werden darf, und macht dann weiter. Bei vielen Beiträgen dauert das
entsprechend; abbrechen mit Strg+C ist jederzeit möglich, ein neuer Aufruf macht weiter.

Aus der .env: MASTODON_ADRESSE, MASTODON_TOKEN
"""

import argparse
import time
from datetime import datetime, timezone

import requests

from bauen import lies_env


def eigene_beitraege(adresse, kopf):
    konto = requests.get(f"{adresse}/api/v1/accounts/verify_credentials", headers=kopf, timeout=60)
    konto.raise_for_status()
    konto = konto.json()
    beitraege, max_id = [], None
    while True:
        parameter = {"limit": 40, **({"max_id": max_id} if max_id else {})}
        antwort = requests.get(f"{adresse}/api/v1/accounts/{konto['id']}/statuses", headers=kopf,
                               params=parameter, timeout=60)
        antwort.raise_for_status()
        seite = antwort.json()
        if not seite:
            break
        beitraege += seite
        max_id = seite[-1]["id"]
    return konto, [b for b in beitraege if b["visibility"] != "direct"]


def warte_bis(antwort):
    zurueck = antwort.headers.get("X-RateLimit-Reset")
    try:
        ende = datetime.fromisoformat(zurueck.replace("Z", "+00:00"))
        sekunden = max(30, (ende - datetime.now(timezone.utc)).total_seconds() + 5)
    except (AttributeError, ValueError):
        sekunden = 300
    print(f"  Löschgrenze erreicht, warte {sekunden / 60:.0f} Minuten …", flush=True)
    time.sleep(sekunden)


def main():
    parser = argparse.ArgumentParser(description="Alle Threads des Lesesaal-Kontos löschen")
    parser.add_argument("--loeschen", action="store_true", help="wirklich löschen")
    args = parser.parse_args()

    env = lies_env()
    adresse = env["MASTODON_ADRESSE"].rstrip("/")
    kopf = {"Authorization": f"Bearer {env['MASTODON_TOKEN']}"}
    konto, beitraege = eigene_beitraege(adresse, kopf)
    anfaenge = sum(1 for b in beitraege if not b.get("in_reply_to_id"))
    print(f"@{konto['acct']}: {len(beitraege)} Beiträge in {anfaenge} Threads, Direktnachrichten nicht mitgezählt")
    if not args.loeschen:
        for b in beitraege[:5]:
            print(f"  {b['created_at'][:10]} {b['url']}")
        print("Nur angezeigt. Zum Löschen mit --loeschen aufrufen.")
        return

    # Antworten zuerst, die Anfänge der Threads zuletzt: bricht der Lauf ab, bleiben ganze Threads übrig.
    beitraege.sort(key=lambda b: not b.get("in_reply_to_id"))
    for nr, b in enumerate(beitraege, 1):
        while True:
            antwort = requests.delete(f"{adresse}/api/v1/statuses/{b['id']}", headers=kopf, timeout=60)
            if antwort.status_code == 429:
                warte_bis(antwort)
                continue
            if antwort.status_code not in (200, 404):
                raise SystemExit(f"Mastodon meldet {antwort.status_code}: {antwort.text[:300]}")
            break
        print(f"  {nr}/{len(beitraege)} gelöscht", flush=True)
    print("Fertig.")


if __name__ == "__main__":
    main()
