"""Anfragen an die Mastodon-Schnittstelle, mit neuen Versuchen bei Netz- und Serverfehlern.

Gemeinsam genutzt von posten.py, nachricht.py, profil.py und dem Tagesbetrieb. Reißt die
Verbindung ab, scheitert TLS, läuft die Zeit ab, antwortet der Server mit 5xx oder mit 429
(zu viele Anfragen), wird die Anfrage nach einer Pause wiederholt: 10, 30, 60 und 120
Sekunden, bei 429 so lange, wie der Server in Retry-After verlangt (höchstens 5 Minuten).
Klappt es auch beim fünften Versuch nicht, kommt Voruebergehend. Die Programme enden dann
mit dem Exit-Code VORUEBERGEHEND, und der Tagesbetrieb zählt das nicht als Fehlversuch.

Beiträge tragen einen Idempotency-Key. Kommt eine Antwort nicht an, obwohl der Beitrag
angelegt wurde, nimmt Mastodon den Wiederholungsversuch mit demselben Schlüssel nicht
noch einmal an, sondern gibt den schon angelegten Beitrag zurück.
"""

import time

import requests

VORUEBERGEHEND = 4
PAUSEN = (10, 30, 60, 120)
NOCHMAL = {429, 500, 502, 503, 504}


class Voruebergehend(Exception):
    """Netz oder Server waren auch nach allen Versuchen nicht erreichbar."""


def anfrage(methode, url, token=None, idempotenz=None, pausen=None, **weiter):
    pausen = PAUSEN if pausen is None else pausen
    kopf = dict(weiter.pop("headers", {}))
    if token:
        kopf["Authorization"] = f"Bearer {token}"
    if idempotenz:
        kopf["Idempotency-Key"] = idempotenz
    weiter.setdefault("timeout", 60)
    letzter = ""
    for nr in range(len(pausen) + 1):
        try:
            antwort = requests.request(methode, url, headers=kopf, **weiter)
        except (requests.ConnectionError, requests.Timeout) as fehler:
            letzter = f"{type(fehler).__name__}: {str(fehler)[:200]}"
        else:
            if antwort.status_code not in NOCHMAL:
                return antwort
            letzter = f"HTTP {antwort.status_code}"
            warten = antwort.headers.get("Retry-After", "")
            if antwort.status_code == 429 and warten.isdigit() and nr < len(pausen):
                print(f"  {letzter}, warte {min(int(warten), 300)} s", flush=True)
                time.sleep(min(int(warten), 300))
                continue
        if nr < len(pausen):
            print(f"  {letzter}, neuer Versuch in {pausen[nr]} s", flush=True)
            time.sleep(pausen[nr])
    raise Voruebergehend(f"{methode} {url.split('?')[0]}: {letzter}")
