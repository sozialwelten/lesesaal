"""Tagesbetrieb: Feeds abfragen, laden, transkribieren, auswerten, veröffentlichen.

Aufruf:
  .venv/bin/python tagesbetrieb.py              ein Lauf über alle Quellen
  .venv/bin/python tagesbetrieb.py --danach-aus medienlabor nach dem Lauf herunterfahren
  .venv/bin/python tagesbetrieb.py --stand      nur anzeigen, welche Folgen auf welcher Stufe stehen

Gedacht für den Start per Alias im Hintergrund (lesesaal). Ablauf:
  1. Feeds abfragen. Beim ersten Lauf einer Quelle kommen die neueste Folge und
     `rueckstand` ältere dazu, danach nur Folgen, die neuer sind als die bekannten.
  2. Laden und transkribieren, neue Folgen zuerst. Die Audiodatei wird danach gelöscht.
  3. Auswerten, erst wenn alles transkribiert ist. Whisper und qwen3:32b passen nicht
     gleichzeitig in den Grafikspeicher.
  4. Seite bauen, hochladen, je neue Notiz einen Thread posten, Notizen committen.
  5. Tagesbericht als Datei und als Direktnachricht.

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
from datetime import datetime
from pathlib import Path

import requests

from bauen import PROJEKT, lies_env
from holen import dateiname, lade, lies_feed, schreibe_meta

ABGEBROCHEN = 3
STUFEN = ["gefunden", "geladen", "transkribiert", "ausgewertet", "veroeffentlicht"]
VORGABEN = {
    "rueckstand": 3,
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
        self.neue_notizen, self.fehler, self.pausen = [], [], []
        self.gpu_anteil = None
        self.gestoppt = False

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

    def offen(self, stufe):
        """Folgen auf einer Stufe, die nicht liegen geblieben sind; neue zuerst, dann nach Datum."""
        folgen = [f for f in self.stand.values()
                  if f["stufe"] in stufe.split() and f.get("versuche", 0) < self.einst["fehlversuche"]]
        folgen.sort(key=lambda f: f["datum"], reverse=True)
        folgen.sort(key=lambda f: not f.get("neu"))
        return folgen

    def pfade(self, folge):
        audio = self.daten / "audio" / folge["quelle"] / folge["datei"]
        stem = audio.stem
        return {
            "audio": audio,
            "meta": audio.with_name(stem + ".meta.json"),
            "transkript": self.daten / "transkripte" / folge["quelle"] / f"{stem}.json",
            "notiz": self.notizen / f"{stem}.md",
        }

    def gepostet(self):
        pfad = self.daten / "gepostet.json"
        return json.loads(pfad.read_text(encoding="utf-8")) if pfad.exists() else {}

    # Stopp und Temperatur

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
            if quelle.get("typ", "podcast") != "podcast" or quelle.get("aktiv", True) is False:
                continue
            try:
                folgen = lies_feed(quelle)
            except (SystemExit, Exception) as fehler:  # noqa: BLE001
                self.fehler.append(f"Feed {quelle['kennung']}: {fehler}")
                log(f"  FEHLER bei {quelle['kennung']}: {fehler}")
                continue
            bekannt = [f for f in self.stand.values() if f["quelle"] == quelle["kennung"]]
            if bekannt:
                neueste = max(f["datum"] for f in bekannt)
                auswahl = [(f, True) for f in folgen if f["datum"] >= neueste]
            else:
                anzahl = 1 + int(quelle.get("rueckstand", self.einst["rueckstand"]))
                auswahl = [(f, i == 0) for i, f in enumerate(folgen[:anzahl])]
            neu = 0
            for folge, ist_neu in auswahl:
                datei = dateiname(folge)
                schluessel = f"{quelle['kennung']}/{Path(datei).stem}"
                if schluessel in self.stand:
                    continue
                eintrag = {
                    "quelle": quelle["kennung"], "titel": folge["titel"], "datum": folge["datum"],
                    "datei": datei, "neu": ist_neu, "gefunden": datetime.now().isoformat(timespec="seconds"),
                    "stufe": "gefunden", "versuche": 0, "fehler": "",
                }
                self.stand[schluessel] = eintrag
                self.vorhandenes_uebernehmen(eintrag, folge, quelle)
                neu += 1
                self.heute["gefunden"] += 1
            self.speichere()
            log(f"  {quelle['name']}: {neu} neu eingetragen")

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

    def laden_und_transkribieren(self):
        warteschlange = self.offen("gefunden geladen")
        if not warteschlange:
            return
        log(f"Laden und transkribieren: {len(warteschlange)} Folgen")
        self.modell_entladen()
        for folge in warteschlange:
            if self.stopp():
                return
            p = self.pfade(folge)
            log(f"{folge['titel']}")
            if folge["stufe"] == "gefunden":
                meta = json.loads(p["meta"].read_text(encoding="utf-8"))
                try:
                    log("  laden")
                    lade(meta, p["audio"])
                    self.setze(folge, "geladen")
                except Exception as fehler:  # noqa: BLE001
                    self.scheitert(folge, f"Laden: {fehler}")
                    continue
            if self.stopp():
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

    def auswerten(self):
        warteschlange = self.offen("transkribiert")
        if not warteschlange or self.stopp():
            return
        log(f"Auswerten: {len(warteschlange)} Folgen")
        self.modell_pruefen()
        for folge in warteschlange:
            if self.stopp():
                return
            p = self.pfade(folge)
            if p["notiz"].exists():
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
                self.setze(folge, "ausgewertet", notiz=str(p["notiz"]))
            else:
                self.setze(folge, "ohne_funde")

    def veroeffentlichen(self):
        warteschlange = self.offen("ausgewertet")
        if not warteschlange:
            return
        log(f"Veröffentlichen: {len(warteschlange)} Notizen")
        if self.fuehre_aus([sys.executable, "bauen.py", "--hochladen"], ueberwachen=False) != 0:
            for folge in warteschlange:
                self.scheitert(folge, "Bauen oder Hochladen fehlgeschlagen")
            return
        for folge in warteschlange:
            notiz = Path(folge["notiz"])
            if notiz.stem not in self.gepostet():
                code = self.fuehre_aus([sys.executable, "posten.py", str(notiz), "--senden"], ueberwachen=False)
                if code != 0:
                    self.scheitert(folge, f"Posten mit Exit-Code {code}")
                    continue
            self.setze(folge, "veroeffentlicht")
            self.neue_notizen.append(folge["titel"])
        self.notizen_committen(warteschlange)

    def notizen_committen(self, folgen):
        dateien = [str(Path(f["notiz"]).relative_to(self.notizen.parent.parent)) for f in folgen if f["stufe"] == "veroeffentlicht"]
        if not dateien:
            return
        repo = self.notizen.parent.parent
        try:
            subprocess.run(["git", "-C", str(repo), "add", "--", *dateien], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", f"Tagesbetrieb {datetime.now():%d.%m.%Y}: "
                            f"{len(dateien)} Notizen", "--", *dateien], check=True, capture_output=True)
            log(f"  {len(dateien)} Notizen committet")
        except subprocess.CalledProcessError as fehler:
            log(f"  Commit der Notizen fehlgeschlagen: {fehler.stderr.decode(errors='replace').strip()}")

    # Bericht

    def bericht(self):
        ende = datetime.now()
        stufen = Counter(f["stufe"] for f in self.stand.values())
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
            f"{self.heute['veroeffentlicht']} veröffentlicht",
            f"- Offen: {offen} ({', '.join(f'{stufen[s]} {s}' for s in STUFEN[:4] if stufen[s]) or 'nichts'})",
            f"- Temperaturpausen: {len(self.pausen)}, zusammen {pausen_min:.0f} Minuten"
            + (f", höchstens {max(p['grad'] for p in self.pausen)} °C" if self.pausen else ""),
        ]
        if self.gpu_anteil is not None:
            zeilen.append(f"- Modell auf den Grafikkarten: {self.gpu_anteil} %")
        if self.neue_notizen:
            zeilen += ["", "## Neue Notizen", ""] + [f"- {t}" for t in self.neue_notizen]
        if self.fehler:
            zeilen += ["", "## Fehler", ""] + [f"- {t}" for t in self.fehler]
        if liegen:
            zeilen += ["", "## Liegen geblieben", ""] + [f"- {f['titel']}: {f['fehler']}" for f in liegen]
        text = "\n".join(zeilen) + "\n"
        ordner = self.daten / "berichte"
        ordner.mkdir(parents=True, exist_ok=True)
        pfad = ordner / f"{self.beginn:%Y-%m-%d_%H%M%S}.md"
        pfad.write_text(text, encoding="utf-8")
        log(f"Tagesbericht: {pfad}")
        print(text, flush=True)
        if self.einst["bericht_als_direktnachricht"]:
            self.direktnachricht(offen, liegen, pausen_min)

    def direktnachricht(self, offen, liegen, pausen_min):
        teile = [
            f"@{self.einst['bericht_an']} Lesesaal-Lauf {self.beginn:%d.%m.} {self.beginn:%H:%M} bis {datetime.now():%H:%M}"
            + (", gestoppt" if self.gestoppt else ""),
            f"{len(self.neue_notizen)} neue Notizen, {self.heute['ohne_funde']} ohne Funde, {offen} offen, "
            f"{len(self.fehler)} Fehler" + (f", {len(liegen)} liegen geblieben" if liegen else ""),
        ]
        if self.pausen:
            teile.append(f"Temperaturpausen: {len(self.pausen)}, {pausen_min:.0f} Minuten")
        if self.gpu_anteil is not None and self.gpu_anteil < 100:
            teile.append(f"Modell nur zu {self.gpu_anteil} % auf den Grafikkarten")
        text = "\n".join(teile)
        for titel in self.neue_notizen:
            if len(text) + len(titel) + 3 > 490:
                text += "\n…"
                break
            text += f"\n- {titel}"
        try:
            env = lies_env()
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
        print(f"{f['datum']}  {f['stufe']:<15} {f['quelle']:<20} {f['titel'][:60]}{liegt}{fehler}")
    print(f"\n{len(folgen)} Folgen: " + ", ".join(f"{n} {s}" for s, n in Counter(f["stufe"] for f in folgen).items()))


def main():
    parser = argparse.ArgumentParser(description="Tagesbetrieb der Lesesaal-Pipeline")
    parser.add_argument("--danach-aus", action="store_true", help="medienlabor nach dem Lauf herunterfahren")
    parser.add_argument("--stand", action="store_true", help="nur den Stand aller Folgen anzeigen")
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
    print(f"\n===== Tagesbetrieb {betrieb.beginn:%d.%m.%Y %H:%M} =====", flush=True)
    try:
        betrieb.finden()
        betrieb.laden_und_transkribieren()
        betrieb.auswerten()
        betrieb.veroeffentlichen()
    except Exception as fehler:  # noqa: BLE001
        betrieb.fehler.append(f"Programmfehler: {fehler!r}")
        log(f"PROGRAMMFEHLER: {fehler!r}")
        raise
    finally:
        betrieb.bericht()
        ausschalten = args.danach_aus
        if betrieb.stoppdatei.exists():
            ausschalten = ausschalten or betrieb.stoppdatei.read_text(encoding="utf-8").strip() == "aus"
            betrieb.stoppdatei.unlink(missing_ok=True)
        if ausschalten:
            log("medienlabor wird heruntergefahren.")
            subprocess.run(["systemctl", "poweroff"], check=False)


if __name__ == "__main__":
    main()
