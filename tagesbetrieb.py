"""Tagesbetrieb: Feeds abfragen, laden, transkribieren, auswerten, veröffentlichen.

Aufruf:
  .venv/bin/python tagesbetrieb.py              ein Lauf über alle Quellen
  .venv/bin/python tagesbetrieb.py --danach-aus medienlabor nach dem Lauf herunterfahren
  .venv/bin/python tagesbetrieb.py --stand      nur anzeigen, welche Folgen auf welcher Stufe stehen
  .venv/bin/python tagesbetrieb.py --ohne-archiv  ohne Aufarbeitung älterer Folgen
  .venv/bin/python tagesbetrieb.py --archiv-bis 15:00
                                                Aufarbeitung heute nur bis zu dieser Uhrzeit
  .venv/bin/python tagesbetrieb.py --nur-archiv --archiv-bis 11:30
                                                nur Aufarbeitung, ohne Feeds, Posten und Direktnachricht;
                                                so ruft der Dirigent sie blockweise auf
  .venv/bin/python tagesbetrieb.py --neu-auswerten alle
                                                fertige Folgen mit vorhandenem Transkript noch einmal
                                                auswerten und ihre Notizen überschreiben; statt „alle“
                                                geht auch eine Quelle oder ein Teil des Dateinamens.
                                                Threads werden nicht neu gepostet.

Gedacht für den Start per Alias im Hintergrund (lesesaal). Ablauf:
  1. Feeds abfragen. Beim ersten Lauf einer Quelle kommt die neueste Folge dazu, danach nur
     Folgen, die neuer sind als die bekannten. Ältere Folgen im Rückstand der Quelle werden
     zur Aufarbeitung eingetragen (siehe unten).
  2. Laden und transkribieren, neue Folgen zuerst. Die Audiodatei wird danach gelöscht.
  3. Auswerten, erst wenn alles transkribiert ist. Whisper und qwen3:32b passen nicht
     gleichzeitig in den Grafikspeicher.
  4. Szene: Ist [szene] aktiv, liest szene.py die Fediverse-Konten aus NOTIZEN/szene.txt und
     schreibt eine Tagesnotiz, die mit veröffentlicht wird.
  5. Termine: Ist [termine] aktiv, sammelt termine.py die Termine aus NOTIZEN/termine.txt und
     schreibt die Seite „Termine“ samt Kalender termine.ics.
  6. Seite bauen, hochladen, je neue Notiz einen Thread posten, Notizen committen. Gebaut wird
     auch, wenn sich nur die Termine geändert haben.
  7. Tagesbericht als Datei und als Direktnachricht, mit den Terminen der nächsten 48 Stunden.
  8. Aufarbeitung bis `archiv_bis` (siehe unten).

Aufarbeitung: Je Quelle legt `rueckstand` fest, wie weit es zurückgeht. Eine Zahl heißt so viele
Folgen vor der neuesten, „alles“ heißt der ganze Feed, sonst gilt `rueckstand_wochen`
(Vorgabe 4 Wochen, 0 schaltet ab). Bei YouTube wird dafür der Kanal über yt-dlp gelistet,
weil der Feed nur 15 Videos enthält. Aufgearbeitete Folgen kommen nur ins Archiv der Seite,
mit ihrem echten Datum: kein Thread, keine Zeile in der Direktnachricht außer dem Fortschritt.
Gearbeitet wird in Blöcken von `archiv_block` Folgen (laden, transkribieren, auswerten, Seite
hochladen), bis `archiv_bis` erreicht ist, das Stoppsignal kommt oder nichts mehr offen ist.
Zuerst kommen die Quellen mit begrenztem Rückstand, neueste zuerst, danach die Quellen mit
„alles“ reihum. `rueckstand_reihenfolge = "alt_zuerst"` arbeitet eine Quelle von der ältesten
Folge an auf. Bei Quellen mit Vorauswahl entscheidet das Modell auch rückwirkend, außer bei
„alles“.

Stufen je Folge: gefunden, geladen, transkribiert, ausgewertet, veroeffentlicht, ohne_funde.
Der Stand liegt in DATEN/stand.json. Ein neuer Lauf macht dort weiter, wo der letzte aufhörte.
Nach `fehlversuche` Fehlern bleibt eine Folge liegen, bis ihr Zähler in stand.json
zurückgesetzt wird.

Stoppsignal: Datei DATEN/stopp. Steht darin „aus“, fährt medienlabor danach herunter.
Zwischen zwei Schritten hält das Programm sofort an. Eine laufende Transkription oder
Auswertung wird fertig gemacht, wenn der Rest in `stopp_hoechstens_minuten` passt, sonst
abgebrochen. Fertige Notizen werden noch veröffentlicht.

Temperatur: Erreicht eine Karte `temperatur_pause`, wird der laufende Schritt angehalten
(SIGSTOP), bis alle Karten unter `temperatur_weiter` liegen.
"""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
from collections import Counter
from datetime import date, datetime, timedelta
from itertools import zip_longest
from pathlib import Path

import requests

from bauen import PROJEKT, lies_env
from holen import NochNichtFertig, Uebersprungen, dateiname, lade, lies_feed, lies_youtube_alle, schreibe_meta

ABGEBROCHEN = 3
STUFEN = ["gefunden", "geladen", "transkribiert", "ausgewertet", "veroeffentlicht"]
VORGABEN = {
    "rueckstand_wochen": 4,
    "archiv_bis": "17:00",
    "archiv_block": 4,
    "stopp_hoechstens_minuten": 10,
    "temperatur_pause": 83,
    "temperatur_weiter": 70,
    "temperatur_abfrage_sekunden": 15,
    "fehlversuche": 3,
    "bericht_an": "sozialwelten",
    "bericht_als_direktnachricht": True,
}


def log(text):
    print(f"{datetime.now():%H:%M:%S} {text}", flush=True)


class Betrieb:
    def __init__(self, konfiguration):
        self.konf = konfiguration
        self.einst = {**VORGABEN, **konfiguration.get("betrieb", {})}
        self.daten = Path(konfiguration["pfade"]["daten"])
        self.notizen = Path(konfiguration["pfade"]["notizen"]) / "inhalt" / "notizen"
        self.stoppdatei = self.daten / "stopp"
        self.stand_pfad = self.daten / "stand.json"
        self.stand = json.loads(self.stand_pfad.read_text(encoding="utf-8")) if self.stand_pfad.exists() else {}
        self.beginn = datetime.now()
        self.heute = Counter()
        self.neue_notizen, self.aktualisiert, self.fehler, self.pausen = [], [], [], []
        self.gpu_anteil = None
        self.gestoppt = False
        self.termine_geaendert = False
        self.bald = []
        self.bericht_pfad = None
        self.archiv_heute = []

    # Stand

    def speichere(self):
        tmp = self.stand_pfad.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.stand, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.stand_pfad)

    def setze(self, folge, stufe, **mehr):
        folge.update(stufe=stufe, versuche=0, fehler="", geaendert=datetime.now().isoformat(timespec="seconds"), **mehr)
        self.heute[stufe] += 1
        self.speichere()

    def scheitert(self, folge, text):
        folge["versuche"] = folge.get("versuche", 0) + 1
        folge["fehler"] = text
        self.fehler.append(f"{folge['titel']}: {text}")
        log(f"  FEHLER ({folge['versuche']}. Versuch): {text}")
        self.speichere()

    def offen(self, stufe, archiv=False):
        """Folgen auf einer Stufe, die nicht liegen geblieben sind.

        Ohne archiv: Folgen des Tagesbetriebs, neue zuerst, dann nach Datum.
        Mit archiv: Folgen der Aufarbeitung in der Reihenfolge von archiv_reihe.
        """
        folgen = [f for f in self.stand.values()
                  if f["stufe"] in stufe.split() and f.get("versuche", 0) < self.einst["fehlversuche"]
                  and bool(f.get("archiv")) == archiv]
        if archiv:
            return self.archiv_reihe(folgen)
        folgen.sort(key=lambda f: f["datum"], reverse=True)
        folgen.sort(key=lambda f: not f.get("neu"))
        return folgen

    def archiv_reihe(self, folgen):
        """Begrenzter Rückstand zuerst (neueste zuerst), danach Quellen mit „alles“ reihum."""
        gruppen = {}
        for f in folgen:
            gruppen.setdefault(f["quelle"], []).append(f)
        begrenzt, alles = [], []
        for kennung, liste in gruppen.items():
            quelle = self.quelle(kennung)
            liste.sort(key=lambda f: f["datum"], reverse=quelle.get("rueckstand_reihenfolge") != "alt_zuerst")
            (alles if str(quelle.get("rueckstand")) == "alles" else begrenzt).append(liste)
        erst = sorted((f for liste in begrenzt for f in liste), key=lambda f: f["datum"], reverse=True)
        dann = [f for runde in zip_longest(*alles) for f in runde if f]
        return erst + dann

    def pfade(self, folge):
        audio = self.daten / "audio" / folge["quelle"] / folge["datei"]
        stem = audio.stem
        return {
            "audio": audio,
            "meta": audio.with_name(stem + ".meta.json"),
            "transkript": self.daten / "transkripte" / folge["quelle"] / f"{stem}.json",
            "notiz": self.notizen / f"{stem}.md",
        }

    def quelle(self, kennung):
        return next((q for q in self.konf["quelle"] if q["kennung"] == kennung), {})

    def quellenname(self, kennung):
        if kennung == "szene":
            return self.konf.get("szene", {}).get("name", "Fediverse-Szene")
        return next((q["name"] for q in self.konf["quelle"] if q["kennung"] == kennung), kennung)

    def nach_quelle(self):
        """Neue Notizen gruppiert: [(Quelle, [Titel, ...]), ...] in der Reihenfolge des Auftretens."""
        gruppen = {}
        for quelle, titel in self.neue_notizen:
            gruppen.setdefault(quelle, []).append(titel)
        return list(gruppen.items())

    def gepostet(self):
        pfad = self.daten / "gepostet.json"
        return json.loads(pfad.read_text(encoding="utf-8")) if pfad.exists() else {}

    # Stopp und Temperatur

    def neu_auswerten(self, auswahl):
        """Setzt fertige Folgen mit Transkript zurück auf „transkribiert“, damit sie neu ausgewertet werden."""
        anzahl = 0
        for schluessel, folge in self.stand.items():
            if folge["stufe"] not in ("ausgewertet", "veroeffentlicht", "ohne_funde"):
                continue
            if auswahl != "alle" and auswahl != folge["quelle"] and auswahl.lower() not in schluessel.lower():
                continue
            if not self.pfade(folge)["transkript"].exists():
                continue
            folge.update(stufe="transkribiert", neu_auswerten=True, versuche=0, fehler="")
            anzahl += 1
        self.speichere()
        log(f"Neu auswerten: {anzahl} Folgen zurückgesetzt")

    def stopp(self):
        if self.stoppdatei.exists():
            if not self.gestoppt:
                log("Stoppsignal erkannt.")
            self.gestoppt = True
        return self.gestoppt

    def temperatur(self):
        try:
            ausgabe = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=20, check=True,
            ).stdout
            return max(int(z) for z in ausgabe.split() if z.strip().isdigit())
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def abkuehlen(self):
        """Vor einem Schritt: warten, falls eine Karte zu heiß ist."""
        grad = self.temperatur()
        if grad is None or grad < self.einst["temperatur_pause"]:
            return
        beginn, hoechst = time.monotonic(), grad
        log(f"  Temperaturpause: {grad} °C, warte bis unter {self.einst['temperatur_weiter']} °C")
        while grad is not None and grad >= self.einst["temperatur_weiter"]:
            time.sleep(self.einst["temperatur_abfrage_sekunden"])
            grad = self.temperatur()
        self.notiere_pause(beginn, hoechst)

    def notiere_pause(self, beginn, hoechst):
        dauer = time.monotonic() - beginn
        self.pausen.append({"beginn": datetime.now().strftime("%H:%M"), "sekunden": round(dauer), "grad": hoechst})
        log(f"  Pause beendet nach {dauer / 60:.1f} Minuten, höchste Temperatur {hoechst} °C")

    def fuehre_aus(self, befehl, ueberwachen=True):
        """Startet einen Schritt als eigenen Prozess, hält ihn bei Hitze an. Gibt den Exit-Code zurück."""
        umgebung = dict(os.environ, PYTHONUNBUFFERED="1")
        prozess = subprocess.Popen(befehl, cwd=PROJEKT, env=umgebung)
        angehalten, beginn, hoechst = False, 0.0, 0
        try:
            while prozess.poll() is None:
                time.sleep(self.einst["temperatur_abfrage_sekunden"])
                if not ueberwachen or prozess.poll() is not None:
                    continue
                grad = self.temperatur()
                if grad is None:
                    continue
                if not angehalten and grad >= self.einst["temperatur_pause"]:
                    prozess.send_signal(signal.SIGSTOP)
                    angehalten, beginn, hoechst = True, time.monotonic(), grad
                    log(f"  Temperaturpause: {grad} °C, Schritt angehalten bis unter {self.einst['temperatur_weiter']} °C")
                elif angehalten:
                    hoechst = max(hoechst, grad)
                    if grad < self.einst["temperatur_weiter"]:
                        prozess.send_signal(signal.SIGCONT)
                        angehalten = False
                        self.notiere_pause(beginn, hoechst)
        finally:
            if angehalten and prozess.poll() is None:
                prozess.send_signal(signal.SIGCONT)
        return prozess.returncode

    # Ollama

    def ollama(self, pfad, **daten):
        adresse = self.konf["ollama"]["adresse"]
        if daten:
            return requests.post(f"{adresse}{pfad}", json=daten, timeout=600).json()
        return requests.get(f"{adresse}{pfad}", timeout=60).json()

    def modell_entladen(self):
        try:
            self.ollama("/api/generate", model=self.konf["ollama"]["modell"], keep_alive=0)
        except (requests.RequestException, ValueError) as fehler:
            log(f"  Ollama nicht erreichbar beim Entladen: {fehler}")

    def modell_pruefen(self):
        """Lädt das Modell und prüft, welcher Anteil auf den Grafikkarten liegt."""
        modell = self.konf["ollama"]["modell"]
        try:
            self.ollama("/api/generate", model=modell, prompt="", keep_alive="15m")
            for m in self.ollama("/api/ps").get("models", []):
                if m.get("name") == modell or m.get("model") == modell:
                    self.gpu_anteil = round(100 * m.get("size_vram", 0) / max(m.get("size", 1), 1))
        except (requests.RequestException, ValueError) as fehler:
            log(f"  Ollama nicht erreichbar: {fehler}")
            return
        if self.gpu_anteil is None:
            log("  Modell nicht in der Liste von Ollama gefunden.")
        elif self.gpu_anteil < 100:
            log(f"  ACHTUNG: Modell liegt nur zu {self.gpu_anteil} % auf den Grafikkarten. "
                "Brave, Obsidian und LibreOffice schließen, sonst wird die Auswertung deutlich langsamer.")
        else:
            log("  Modell liegt vollständig auf den Grafikkarten.")

    # Schritte

    def finden(self):
        log("Feeds abfragen")
        for quelle in self.konf["quelle"]:
            if quelle.get("typ", "podcast") not in ("podcast", "youtube", "tiktok") or quelle.get("aktiv", True) is False:
                continue
            try:
                folgen = lies_feed(quelle)
            except (SystemExit, Exception) as fehler:  # noqa: BLE001
                self.fehler.append(f"Feed {quelle['kennung']}: {fehler}")
                log(f"  FEHLER bei {quelle['kennung']}: {fehler}")
                continue
            ausschluss = [w.lower() for w in quelle.get("ohne_titel_mit", [])]
            folgen = [f for f in folgen if not any(w in f["titel"].lower() for w in ausschluss)]
            folgen.sort(key=lambda f: f["datum"], reverse=True)
            bekannt = [f for f in self.stand.values() if f["quelle"] == quelle["kennung"]]
            bekannte_dateien = {f["datei"] for f in bekannt}
            if bekannt:
                neueste = max(f["datum"] for f in bekannt)
                kandidaten = [f for f in folgen if f["datum"] >= neueste and dateiname(f) not in bekannte_dateien]
                anzahl = int(quelle.get("hoechstens_pro_lauf") or len(kandidaten))
            else:
                anzahl = 1
                kandidaten = folgen[:1]
            abgelehnt = []
            if quelle.get("vorauswahl") and kandidaten:
                schwelle = int(quelle.get("vorauswahl_ab", 3))
                bewertet = sorted(self.vorauswahl(quelle, kandidaten), key=lambda e: -e[1])
                behalten = [f for f, r in bewertet if r >= schwelle][:anzahl]
                abgelehnt = [(f, r) for f, r in bewertet if f not in behalten]
            else:
                behalten = kandidaten[:anzahl]
            auswahl = [(f, bool(bekannt) or nr == 0) for nr, f in enumerate(behalten)]
            for folge, relevanz in abgelehnt:
                datei = dateiname(folge)
                schluessel = f"{quelle['kennung']}/{Path(datei).stem}"
                if schluessel not in self.stand:
                    self.stand[schluessel] = {
                        "quelle": quelle["kennung"], "titel": folge["titel"], "datum": folge["datum"],
                        "datei": datei, "neu": False, "gefunden": datetime.now().isoformat(timespec="seconds"),
                        "stufe": "uebersprungen", "grund": f"Vorauswahl: Relevanz {relevanz}", "versuche": 0, "fehler": "",
                    }
                    self.heute["uebersprungen"] += 1
            neu = 0
            for folge, ist_neu in auswahl:
                datei = dateiname(folge)
                schluessel = f"{quelle['kennung']}/{Path(datei).stem}"
                if schluessel in self.stand:
                    continue
                eintrag = {
                    "quelle": quelle["kennung"], "titel": folge["titel"], "datum": folge["datum"],
                    "datei": datei, "neu": ist_neu, "gefunden": datetime.now().isoformat(timespec="seconds"),
                    "stufe": "gefunden", "versuche": 0, "fehler": "", "seite": folge.get("seite", ""),
                }
                self.stand[schluessel] = eintrag
                self.vorhandenes_uebernehmen(eintrag, folge, quelle)
                neu += 1
                self.heute["gefunden"] += 1
            self.speichere()
            log(f"  {quelle['name']}: {neu} neu eingetragen")
            try:
                self.archiv_finden(quelle, folgen)
            except (SystemExit, Exception) as fehler:  # noqa: BLE001
                self.fehler.append(f"Aufarbeitung {quelle['kennung']}: {fehler}")
                log(f"  FEHLER bei der Aufarbeitung von {quelle['kennung']}: {fehler}")

    def archiv_bereich(self, quelle):
        """("alles", None), ("folgen", n) oder ("wochen", n) nach config.toml."""
        wert = quelle.get("rueckstand")
        if str(wert).strip().lower() == "alles":
            return "alles", None
        if wert is not None:
            return "folgen", int(wert)
        return "wochen", int(quelle.get("rueckstand_wochen", self.einst["rueckstand_wochen"]))

    def bekannte_seiten(self, kennung):
        """Folgenseiten bekannter Folgen einer Quelle, auch aus älteren Einträgen ohne „seite“."""
        seiten = set()
        for f in self.stand.values():
            if f["quelle"] != kennung:
                continue
            seite = f.get("seite")
            if not seite:
                meta = self.pfade(f)["meta"]
                if meta.exists():
                    seite = json.loads(meta.read_text(encoding="utf-8")).get("seite", "")
            if seite:
                seiten.add(seite)
        return seiten

    def archiv_finden(self, quelle, folgen):
        """Trägt ältere Folgen im Rückstand der Quelle zur Aufarbeitung ein."""
        art, wert = self.archiv_bereich(quelle)
        if art != "alles" and wert <= 0:
            return
        grenze = (date.today() - timedelta(weeks=wert)).isoformat() if art == "wochen" else ""
        rss_reicht = (art == "folgen" and wert < 14) or (art == "wochen" and folgen and folgen[-1]["datum"] < grenze)
        if quelle.get("typ") == "youtube" and not rss_reicht:
            hoechstens = None if art == "alles" else (wert + 1 if art == "folgen" else 60)
            cache = self.daten / "archiv" / f"{quelle['kennung']}.json"
            folgen = lies_youtube_alle(quelle, cache, hoechstens)
            ausschluss = [w.lower() for w in quelle.get("ohne_titel_mit", [])]
            folgen = [f for f in folgen if not any(w in f["titel"].lower() for w in ausschluss)]
            folgen.sort(key=lambda f: f["datum"], reverse=True)
        if art == "folgen":
            bereich = folgen[:wert + 1]
        elif art == "wochen":
            bereich = [f for f in folgen if f["datum"] >= grenze]
        else:
            bereich = folgen
        seiten = self.bekannte_seiten(quelle["kennung"])
        kandidaten = []
        for folge in bereich:
            schluessel = f"{quelle['kennung']}/{Path(dateiname(folge)).stem}"
            if schluessel in self.stand or (folge.get("seite") and folge["seite"] in seiten):
                continue
            kandidaten.append(folge)
        if not kandidaten:
            return
        abgelehnt = []
        if quelle.get("vorauswahl") and art != "alles":
            schwelle = int(quelle.get("vorauswahl_ab", 3))
            bewertet = self.vorauswahl(quelle, kandidaten)
            abgelehnt = [(f, r) for f, r in bewertet if r < schwelle]
            kandidaten = [f for f, r in bewertet if r >= schwelle]
        jetzt = datetime.now().isoformat(timespec="seconds")
        for folge, relevanz in abgelehnt:
            datei = dateiname(folge)
            self.stand[f"{quelle['kennung']}/{Path(datei).stem}"] = {
                "quelle": quelle["kennung"], "titel": folge["titel"], "datum": folge["datum"], "datei": datei,
                "neu": False, "archiv": True, "gefunden": jetzt, "stufe": "uebersprungen",
                "grund": f"Vorauswahl: Relevanz {relevanz}", "versuche": 0, "fehler": "", "seite": folge.get("seite", ""),
            }
        for folge in kandidaten:
            datei = dateiname(folge)
            eintrag = {
                "quelle": quelle["kennung"], "titel": folge["titel"], "datum": folge["datum"], "datei": datei,
                "neu": False, "archiv": True, "gefunden": jetzt, "stufe": "gefunden",
                "versuche": 0, "fehler": "", "seite": folge.get("seite", ""),
            }
            self.stand[f"{quelle['kennung']}/{Path(datei).stem}"] = eintrag
            self.vorhandenes_uebernehmen(eintrag, folge, quelle)
        self.speichere()
        log(f"  {quelle['name']}: {len(kandidaten)} zur Aufarbeitung eingetragen"
            + (f", {len(abgelehnt)} durch die Vorauswahl übersprungen" if abgelehnt else ""))

    def vorauswahl(self, quelle, kandidaten):
        """Bewertet die Titel neuer Folgen gegen das Interessenprofil. Gibt [(folge, relevanz), ...] zurück."""
        from auswerten import FORMATE, frage_modell, profil_auszug, waehle_format

        fmt = FORMATE[waehle_format(quelle, "")]
        profil = profil_auszug((self.notizen.parent.parent / "interessenprofil.md").read_text(encoding="utf-8"),
                               fmt["profil"])
        liste = "\n".join(f"{nr}. {f['titel']}" for nr, f in enumerate(kandidaten, 1))
        inhalt = (
            f"Titel neuer Folgen von {quelle['name']}:\n\n{liste}\n\n---\n\nInteressenprofil:\n\n{profil}\n\n---\n\n"
            "Aufgabe: Bewerte für jeden Titel, wie gut die Folge vermutlich zum Interessenprofil passt, "
            "von 1 (gar nicht) bis 5 (sehr gut). Gib für jede Nummer genau eine Bewertung zurück."
        )
        schema = {
            "type": "object",
            "properties": {"bewertungen": {"type": "array", "items": {
                "type": "object",
                "properties": {"nr": {"type": "integer"}, "relevanz": {"type": "integer", "minimum": 1, "maximum": 5}},
                "required": ["nr", "relevanz"],
            }}},
            "required": ["bewertungen"],
        }
        try:
            roh, _ = frage_modell(self.konf, inhalt, schema)
            werte = {b["nr"]: b["relevanz"] for b in json.loads(roh).get("bewertungen", [])}
        except (requests.RequestException, ValueError, KeyError, TypeError) as fehler:
            log(f"  Vorauswahl nicht möglich ({fehler}), nehme die neuesten")
            return [(f, 3) for f in kandidaten]
        ergebnis = [(f, int(werte.get(nr, 0))) for nr, f in enumerate(kandidaten, 1)]
        for f, r in ergebnis:
            log(f"    Vorauswahl {r}: {f['titel'][:70]}")
        return ergebnis

    def vorhandenes_uebernehmen(self, eintrag, folge, quelle):
        """Was es schon gibt, wird nicht noch einmal gemacht: Transkript, Notiz, Thread."""
        p = self.pfade(eintrag)
        if not p["meta"].exists():
            p["audio"].parent.mkdir(parents=True, exist_ok=True)
            schreibe_meta(folge, quelle, p["audio"])
        if p["notiz"].stem in self.gepostet():
            eintrag["stufe"] = "veroeffentlicht"
        elif p["notiz"].exists():
            eintrag["stufe"], eintrag["notiz"] = "ausgewertet", str(p["notiz"])
        elif p["transkript"].exists():
            eintrag["stufe"] = "transkribiert"
        elif p["audio"].exists():
            eintrag["stufe"] = "geladen"
        if eintrag["stufe"] != "gefunden":
            log(f"  schon vorhanden bis Stufe {eintrag['stufe']}: {eintrag['titel']}")

    def frist_um(self, bis):
        return bis is not None and datetime.now() >= bis

    def laden_und_transkribieren(self, warteschlange=None, bis=None):
        if warteschlange is None:
            warteschlange = self.offen("gefunden geladen")
        if not warteschlange:
            return
        log(f"Laden und transkribieren: {len(warteschlange)} Folgen")
        self.modell_entladen()
        for folge in warteschlange:
            if self.stopp() or self.frist_um(bis):
                return
            p = self.pfade(folge)
            log(f"{folge['titel']}")
            if folge["stufe"] == "gefunden":
                meta = json.loads(p["meta"].read_text(encoding="utf-8"))
                try:
                    log("  laden")
                    lade(meta, p["audio"], self.quelle(folge["quelle"]))
                    self.setze(folge, "geladen")
                except NochNichtFertig as grund:
                    log(f"  noch nicht verfügbar ({grund}), beim nächsten Lauf wieder")
                    continue
                except Uebersprungen as grund:
                    log(f"  übersprungen: {grund}")
                    self.setze(folge, "uebersprungen", grund=str(grund))
                    continue
                except Exception as fehler:  # noqa: BLE001
                    self.scheitert(folge, f"Laden: {fehler}")
                    continue
            if self.stopp() or self.frist_um(bis):
                return
            self.abkuehlen()
            log("  transkribieren")
            code = self.fuehre_aus([
                sys.executable, "transkribieren.py", str(p["audio"]),
                "--stoppdatei", str(self.stoppdatei), "--hoechstens", str(self.einst["stopp_hoechstens_minuten"]),
            ])
            if code == ABGEBROCHEN:
                log("  abgebrochen, beim nächsten Lauf neu")
                return
            if code != 0 or not p["transkript"].exists():
                self.scheitert(folge, f"Transkription mit Exit-Code {code}")
                continue
            p["audio"].unlink(missing_ok=True)
            self.setze(folge, "transkribiert")

    def auswerten(self, warteschlange=None, bis=None):
        if warteschlange is None:
            warteschlange = self.offen("transkribiert")
        if not warteschlange or self.stopp():
            return
        log(f"Auswerten: {len(warteschlange)} Folgen")
        self.modell_pruefen()
        for folge in warteschlange:
            if self.stopp() or self.frist_um(bis):
                return
            p = self.pfade(folge)
            if p["notiz"].exists() and not folge.get("neu_auswerten"):
                self.setze(folge, "ausgewertet", notiz=str(p["notiz"]))
                continue
            self.abkuehlen()
            log(f"{folge['titel']}")
            code = self.fuehre_aus([
                sys.executable, "auswerten.py", str(p["transkript"]),
                "--stoppdatei", str(self.stoppdatei), "--hoechstens", str(self.einst["stopp_hoechstens_minuten"]),
            ])
            if code == ABGEBROCHEN:
                log("  abgebrochen, beim nächsten Lauf neu")
                return
            if code != 0:
                self.scheitert(folge, f"Auswertung mit Exit-Code {code}")
            elif p["notiz"].exists():
                self.setze(folge, "ausgewertet", notiz=str(p["notiz"]), neu_auswerten=False)
            else:
                self.setze(folge, "ohne_funde", neu_auswerten=False)

    def szene(self):
        """Fediverse-Konten der Szene auswerten; eine neue Tagesnotiz kommt als „ausgewertet“ in den Stand."""
        if not self.konf.get("szene", {}).get("aktiv") or self.stopp():
            return
        if not (self.notizen.parent.parent / "szene.txt").exists():
            log("Szene: keine szene.txt im Notizen-Repository")
            return
        log("Szene-Konten")
        ergebnis = subprocess.run([sys.executable, "szene.py"], cwd=PROJEKT, capture_output=True, text=True,
                                  env=dict(os.environ, PYTHONUNBUFFERED="1"))
        print(ergebnis.stdout, end="", flush=True)
        if ergebnis.returncode != 0:
            self.fehler.append(f"Szene: Exit-Code {ergebnis.returncode}")
            log(f"  FEHLER: {ergebnis.stderr.strip()[-300:]}")
            return
        letzte = ergebnis.stdout.strip().splitlines()[-1] if ergebnis.stdout.strip() else ""
        notiz = Path(letzte)
        if not letzte.endswith(".md") or not notiz.exists():
            return
        self.stand[f"szene/{notiz.stem}"] = {
            "quelle": "szene", "titel": f"Szene am {datetime.now():%d.%m.%Y}", "datum": f"{datetime.now():%Y-%m-%d}",
            "datei": f"{notiz.stem}.txt", "neu": True, "gefunden": datetime.now().isoformat(timespec="seconds"),
            "stufe": "gefunden", "versuche": 0, "fehler": "",
        }
        self.setze(self.stand[f"szene/{notiz.stem}"], "ausgewertet", notiz=str(notiz))

    def termine(self):
        """Termine sammeln; hat sich die Seite geändert, wird sie mit veröffentlicht."""
        if not self.konf.get("termine", {}).get("aktiv") or self.stopp():
            return
        repo = self.notizen.parent.parent
        if not (repo / "termine.txt").exists():
            log("Termine: keine termine.txt im Notizen-Repository")
            return
        log("Termine")
        ergebnis = subprocess.run([sys.executable, "termine.py"], cwd=PROJEKT, capture_output=True, text=True,
                                  env=dict(os.environ, PYTHONUNBUFFERED="1"))
        print(ergebnis.stdout, end="", flush=True)
        if ergebnis.returncode != 0:
            self.fehler.append(f"Termine: Exit-Code {ergebnis.returncode}")
            log(f"  FEHLER: {ergebnis.stderr.strip()[-300:]}")
            return
        self.fehler += [f"Termine: {z.strip()[7:]}" for z in ergebnis.stdout.splitlines() if z.strip().startswith("FEHLER")]
        letzte = ergebnis.stdout.strip().splitlines()[-1] if ergebnis.stdout.strip() else ""
        self.termine_geaendert = letzte.endswith("index.md")
        bald = self.daten / "termine" / "bald.json"
        self.bald = json.loads(bald.read_text(encoding="utf-8")) if bald.exists() else []

    def veroeffentlichen(self):
        warteschlange = self.offen("ausgewertet")
        if not warteschlange and not self.termine_geaendert:
            return
        log(f"Veröffentlichen: {len(warteschlange)} Notizen" + (", Termine geändert" if self.termine_geaendert else ""))
        if self.fuehre_aus([sys.executable, "bauen.py", "--hochladen"], ueberwachen=False) != 0:
            for folge in warteschlange:
                self.scheitert(folge, "Bauen oder Hochladen fehlgeschlagen")
            if not warteschlange:
                self.fehler.append("Termine: Bauen oder Hochladen fehlgeschlagen")
            return
        for folge in warteschlange:
            notiz = Path(folge["notiz"])
            schon_gepostet = notiz.stem in self.gepostet()
            if not schon_gepostet:
                code = self.fuehre_aus([sys.executable, "posten.py", str(notiz), "--senden"], ueberwachen=False)
                if code != 0:
                    self.scheitert(folge, f"Posten mit Exit-Code {code}")
                    continue
            self.setze(folge, "veroeffentlicht")
            if schon_gepostet:
                self.aktualisiert.append((self.quellenname(folge["quelle"]), folge["titel"]))
            else:
                self.neue_notizen.append((self.quellenname(folge["quelle"]), folge["titel"]))
        self.notizen_committen(warteschlange)

    def notizen_committen(self, folgen, aufarbeitung=False):
        dateien = [str(Path(f["notiz"]).relative_to(self.notizen.parent.parent)) for f in folgen if f["stufe"] == "veroeffentlicht"]
        anzahl = len(dateien)
        mit_terminen = self.termine_geaendert and not aufarbeitung
        if mit_terminen:
            dateien += ["inhalt/termine/index.md", "inhalt/termine/termine.ics"]
        if not dateien:
            return
        repo = self.notizen.parent.parent
        if aufarbeitung:
            nachricht = f"Aufarbeitung {datetime.now():%d.%m.%Y}: {anzahl} Notizen"
        else:
            nachricht = f"Tagesbetrieb {datetime.now():%d.%m.%Y}: {anzahl} Notizen" + (", Termine" if mit_terminen else "")
        try:
            subprocess.run(["git", "-C", str(repo), "add", "--", *dateien], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", nachricht, "--", *dateien],
                           check=True, capture_output=True)
            log(f"  committet: {nachricht}")
        except subprocess.CalledProcessError as fehler:
            log(f"  Commit der Notizen fehlgeschlagen: {fehler.stderr.decode(errors='replace').strip()}")

    # Aufarbeitung

    def archiv(self, bis):
        """Arbeitet ältere Folgen in Blöcken auf, bis die Frist erreicht ist oder nichts mehr offen ist."""
        if self.frist_um(bis):
            log(f"Aufarbeitung: Frist {bis:%H:%M} schon erreicht")
            return
        log(f"Aufarbeitung bis {bis:%H:%M}")
        while not self.stopp() and not self.frist_um(bis):
            block = self.offen("gefunden geladen transkribiert ausgewertet", archiv=True)[:self.einst["archiv_block"]]
            if not block:
                log("Aufarbeitung: nichts mehr offen")
                break
            vorher = [(f["stufe"], f.get("versuche", 0)) for f in block]
            self.laden_und_transkribieren([f for f in block if f["stufe"] in ("gefunden", "geladen")], bis)
            if self.stopp() or self.frist_um(bis):
                break
            self.auswerten([f for f in block if f["stufe"] == "transkribiert"], bis)
            self.archiv_veroeffentlichen([f for f in block if f["stufe"] == "ausgewertet"])
            if [(f["stufe"], f.get("versuche", 0)) for f in block] == vorher:
                log("Aufarbeitung: kein Fortschritt in diesem Block, Ende für heute")
                break
        self.archiv_nachtrag(bis)

    def archiv_veroeffentlichen(self, folgen):
        """Seite bauen und hochladen, ohne Threads. Die Notizen tragen ihr echtes Datum."""
        if not folgen or self.stopp():
            return
        log(f"Aufarbeitung veröffentlichen: {len(folgen)} Notizen")
        if self.fuehre_aus([sys.executable, "bauen.py", "--hochladen"], ueberwachen=False) != 0:
            for folge in folgen:
                self.scheitert(folge, "Bauen oder Hochladen fehlgeschlagen")
            return
        for folge in folgen:
            self.setze(folge, "veroeffentlicht")
            self.archiv_heute.append((self.quellenname(folge["quelle"]), folge["titel"], folge["datum"]))
        self.notizen_committen(folgen, aufarbeitung=True)

    def archiv_stand(self):
        """(erledigt, offen) über alle Folgen der Aufarbeitung."""
        archiv = [f for f in self.stand.values() if f.get("archiv") and f["stufe"] != "uebersprungen"]
        offen = sum(1 for f in archiv if f["stufe"] in STUFEN[:4] and f.get("versuche", 0) < self.einst["fehlversuche"])
        erledigt = sum(1 for f in archiv if f["stufe"] in ("veroeffentlicht", "ohne_funde"))
        return erledigt, offen

    def archiv_nachtrag(self, bis):
        """Hängt das Ergebnis der Aufarbeitung an den Tagesbericht an."""
        erledigt, offen = self.archiv_stand()
        zeilen = ["", f"## Aufarbeitung bis {datetime.now():%H:%M} (Frist {bis:%H:%M})", "",
                  f"- heute veröffentlicht: {len(self.archiv_heute)}",
                  f"- insgesamt erledigt: {erledigt}, offen: {offen}"]
        if self.archiv_heute:
            zeilen.append("")
            zeilen += [f"- {q}: {t} ({d})" for q, t, d in self.archiv_heute]
        text = "\n".join(zeilen) + "\n"
        if self.bericht_pfad:
            with open(self.bericht_pfad, "a", encoding="utf-8") as datei:
                datei.write(text)
        print(text, flush=True)

    # Bericht

    def bericht(self):
        ende = datetime.now()
        stufen = Counter(f["stufe"] for f in self.stand.values() if not f.get("archiv"))
        liegen = [f for f in self.stand.values() if f.get("versuche", 0) >= self.einst["fehlversuche"]]
        offen = sum(stufen[s] for s in ("gefunden", "geladen", "transkribiert", "ausgewertet"))
        pausen_min = sum(p["sekunden"] for p in self.pausen) / 60
        zeilen = [
            f"# Tagesbericht {self.beginn:%d.%m.%Y}",
            "",
            f"- Lauf: {self.beginn:%H:%M} bis {ende:%H:%M}" + (", durch Stoppsignal beendet" if self.gestoppt else ""),
            f"- Neu gefunden: {self.heute['gefunden']}",
            f"- Heute erledigt: {self.heute['geladen']} geladen, {self.heute['transkribiert']} transkribiert, "
            f"{self.heute['ausgewertet']} ausgewertet, {self.heute['ohne_funde']} ohne Funde, "
            f"{self.heute['veroeffentlicht']} veröffentlicht, {self.heute['uebersprungen']} übersprungen",
            f"- Offen: {offen} ({', '.join(f'{stufen[s]} {s}' for s in STUFEN[:4] if stufen[s]) or 'nichts'})",
            f"- Temperaturpausen: {len(self.pausen)}, zusammen {pausen_min:.0f} Minuten"
            + (f", höchstens {max(p['grad'] for p in self.pausen)} °C" if self.pausen else ""),
        ]
        if self.gpu_anteil is not None:
            zeilen.append(f"- Modell auf den Grafikkarten: {self.gpu_anteil} %")
        erledigt, offen_archiv = self.archiv_stand()
        if erledigt or offen_archiv:
            zeilen.append(f"- Aufarbeitung: {erledigt} erledigt, {offen_archiv} offen")
        if self.bald:
            zeilen += ["", "## Termine in den nächsten 48 Stunden", ""]
            zeilen += [f"- {t['zeit']} {t['titel']} ({t['quelle']})" for t in self.bald]
        if self.neue_notizen:
            zeilen += ["", "## Neue Notizen"]
            for quelle, titel in self.nach_quelle():
                zeilen += ["", f"{quelle}:"] + [f"- {t}" for t in titel]
        if self.aktualisiert:
            zeilen += ["", "## Neu ausgewertet", ""] + [f"- {q}: {t}" for q, t in self.aktualisiert]
        if self.fehler:
            zeilen += ["", "## Fehler", ""] + [f"- {t}" for t in self.fehler]
        if liegen:
            zeilen += ["", "## Liegen geblieben", ""] + [f"- {f['titel']}: {f['fehler']}" for f in liegen]
        text = "\n".join(zeilen) + "\n"
        ordner = self.daten / "berichte"
        ordner.mkdir(parents=True, exist_ok=True)
        pfad = ordner / f"{self.beginn:%Y-%m-%d_%H%M%S}.md"
        pfad.write_text(text, encoding="utf-8")
        self.bericht_pfad = pfad
        log(f"Tagesbericht: {pfad}")
        print(text, flush=True)
        if self.einst["bericht_als_direktnachricht"]:
            self.direktnachricht(offen, liegen, pausen_min)

    def direktnachricht(self, offen, liegen, pausen_min):
        teile = [
            f"@{self.einst['bericht_an']} Lesesaal-Lauf {self.beginn:%d.%m.} {self.beginn:%H:%M} bis {datetime.now():%H:%M}"
            + (", gestoppt" if self.gestoppt else ""),
            f"{len(self.neue_notizen)} {'neue Notiz' if len(self.neue_notizen) == 1 else 'neue Notizen'}, "
            f"{self.heute['ohne_funde']} ohne Funde, "
            + (f"{len(self.aktualisiert)} neu ausgewertet, " if self.aktualisiert else "")
            + f"{offen} offen, "
            f"{len(self.fehler)} Fehler" + (f", {len(liegen)} liegen geblieben" if liegen else ""),
        ]
        if self.pausen:
            teile.append(f"Temperaturpausen: {len(self.pausen)}, {pausen_min:.0f} Minuten")
        erledigt, offen_archiv = self.archiv_stand()
        if erledigt or offen_archiv:
            teile.append(f"Aufarbeitung: {erledigt} erledigt, {offen_archiv} offen")
        if self.gpu_anteil is not None and self.gpu_anteil < 100:
            teile.append(f"Modell nur zu {self.gpu_anteil} % auf den Grafikkarten")
        try:
            env = lies_env()
            link = f"{self.konf['seite']['adresse'].rstrip('/')}/{env['GEHEIMER_PFAD']}/"
        except (KeyError, SystemExit):
            env, link = {}, ""
        text = "\n".join(teile)
        platz = 490 - (25 if link else 0)  # Mastodon zählt einen Link mit 23 Zeichen
        if self.bald:
            text += "\n\nBald:"
            for t in self.bald[:4]:
                text += f"\n- {t['zeit']} {t['titel'][:50]}"
            if len(self.bald) > 4:
                text += f"\n- und {len(self.bald) - 4} weitere"
        voll = False
        for quelle, titel_liste in self.nach_quelle():
            for nr, titel in enumerate(titel_liste):
                zusatz = (f"\n\n{quelle}:" if nr == 0 else "") + f"\n- {titel}"
                if len(text) + len(zusatz) > platz:
                    voll = True
                    break
                text += zusatz
            if voll:
                text += "\n…"
                break
        if link:
            text += f"\n\n{link}"
        try:
            antwort = requests.post(
                f"{env['MASTODON_ADRESSE'].rstrip('/')}/api/v1/statuses",
                headers={"Authorization": f"Bearer {env['MASTODON_TOKEN']}",
                         "Idempotency-Key": f"lesesaal-bericht-{self.beginn:%Y%m%d%H%M%S}"},
                data={"status": text, "visibility": "direct", "language": "de"},
                timeout=60,
            )
            antwort.raise_for_status()
            log("Bericht als Direktnachricht verschickt.")
        except (requests.RequestException, KeyError, SystemExit) as fehler:
            log(f"Direktnachricht fehlgeschlagen: {fehler}")


def zeige_stand(betrieb):
    folgen = sorted(betrieb.stand.values(), key=lambda f: (f["quelle"], f["datum"]), reverse=True)
    for f in folgen:
        liegt = " LIEGT" if f.get("versuche", 0) >= betrieb.einst["fehlversuche"] else ""
        fehler = f"  ({f['fehler']})" if f.get("fehler") else ""
        art = "A" if f.get("archiv") else " "
        print(f"{f['datum']} {art} {f['stufe']:<15} {f['quelle']:<20} {f['titel'][:60]}{liegt}{fehler}")
    print(f"\n{len(folgen)} Folgen: " + ", ".join(f"{n} {s}" for s, n in Counter(f["stufe"] for f in folgen).items()))
    erledigt, offen = betrieb.archiv_stand()
    print(f"Aufarbeitung (A): {erledigt} erledigt, {offen} offen")


def main():
    parser = argparse.ArgumentParser(description="Tagesbetrieb der Lesesaal-Pipeline")
    parser.add_argument("--danach-aus", action="store_true", help="medienlabor nach dem Lauf herunterfahren")
    parser.add_argument("--stand", action="store_true", help="nur den Stand aller Folgen anzeigen")
    parser.add_argument("--ohne-archiv", action="store_true", help="heute keine älteren Folgen aufarbeiten")
    parser.add_argument("--archiv-bis", metavar="HH:MM", help="Aufarbeitung heute nur bis zu dieser Uhrzeit")
    parser.add_argument("--nur-archiv", action="store_true",
                        help="nur ältere Folgen aufarbeiten: keine Feeds, kein Posten, keine Direktnachricht")
    parser.add_argument("--neu-auswerten", metavar="AUSWAHL",
                        help="fertige Folgen noch einmal auswerten: alle, eine Quelle oder ein Teil des Namens")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)
    betrieb = Betrieb(konfiguration)
    if args.stand:
        zeige_stand(betrieb)
        return

    sperre = open(betrieb.daten / "tagesbetrieb.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(sperre, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Der Tagesbetrieb läuft schon.") from None

    betrieb.stoppdatei.unlink(missing_ok=True)
    stunde, _, minute = (args.archiv_bis or str(betrieb.einst["archiv_bis"])).partition(":")
    bis = datetime.now().replace(hour=int(stunde), minute=int(minute or 0), second=0, microsecond=0)
    if args.nur_archiv:
        print(f"\n===== Aufarbeitung {betrieb.beginn:%d.%m.%Y %H:%M} =====", flush=True)
        ordner = betrieb.daten / "berichte"
        ordner.mkdir(parents=True, exist_ok=True)
        betrieb.bericht_pfad = ordner / f"{betrieb.beginn:%Y-%m-%d_%H%M%S}_aufarbeitung.md"
        betrieb.bericht_pfad.write_text(f"# Aufarbeitung {betrieb.beginn:%d.%m.%Y %H:%M}\n", encoding="utf-8")
        try:
            betrieb.archiv(bis)
        finally:
            betrieb.stoppdatei.unlink(missing_ok=True)
        return
    print(f"\n===== Tagesbetrieb {betrieb.beginn:%d.%m.%Y %H:%M} =====", flush=True)
    tagesteil_fertig = False
    try:
        if args.neu_auswerten:
            betrieb.neu_auswerten(args.neu_auswerten)
        betrieb.finden()
        betrieb.laden_und_transkribieren()
        betrieb.auswerten()
        betrieb.szene()
        betrieb.termine()
        betrieb.veroeffentlichen()
        tagesteil_fertig = True
    except Exception as fehler:  # noqa: BLE001
        betrieb.fehler.append(f"Programmfehler: {fehler!r}")
        log(f"PROGRAMMFEHLER: {fehler!r}")
        raise
    finally:
        betrieb.bericht()
        if tagesteil_fertig and not args.ohne_archiv and not betrieb.stopp():
            try:
                betrieb.archiv(bis)
            except Exception as fehler:  # noqa: BLE001
                log(f"PROGRAMMFEHLER in der Aufarbeitung: {fehler!r}")
        ausschalten = args.danach_aus
        if betrieb.stoppdatei.exists():
            ausschalten = ausschalten or betrieb.stoppdatei.read_text(encoding="utf-8").strip() == "aus"
            betrieb.stoppdatei.unlink(missing_ok=True)
        if ausschalten:
            log("medienlabor wird heruntergefahren.")
            subprocess.run(["systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    main()
