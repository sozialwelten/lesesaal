"""Eine Audiodatei mit faster-whisper transkribieren.

Aufruf:
  .venv/bin/python transkribieren.py DATEI          Sprache aus der Quelle in config.toml
  .venv/bin/python transkribieren.py DATEI --neu    vorhandenes Transkript überschreiben

Ergebnis unter DATEN/transkripte/<quelle>/:
  <name>.json  alle Abschnitte mit Start, Ende und Text
  <name>.txt   Text mit Zeitmarken zum Lesen

Das Audio dekodiert ffmpeg aus dem System, nicht PyAV.
"""

import os
import sys
from pathlib import Path


def sorge_fuer_cuda():
    """Startet das Skript einmal neu, damit CUDA-Bibliotheken aus der .venv gefunden werden."""
    os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    pfade = sorted(str(p) for p in Path(sys.prefix).glob("lib/python3.*/site-packages/nvidia/*/lib"))
    if not pfade:
        return
    bisher = os.environ.get("LD_LIBRARY_PATH", "")
    if all(p in bisher.split(":") for p in pfade):
        return
    os.environ["LD_LIBRARY_PATH"] = ":".join(pfade + ([bisher] if bisher else []))
    os.execv(sys.executable, [sys.executable] + sys.argv)


sorge_fuer_cuda()

import argparse  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import tomllib  # noqa: E402

import numpy as np  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

PROJEKT = Path(__file__).resolve().parent
ABTASTRATE = 16000


def zeitmarke(sekunden):
    s = int(sekunden)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def lies_audio(pfad):
    """Dekodiert eine Audio- oder Videodatei mit ffmpeg zu Mono, 16 kHz, als Gleitkommawerte."""
    befehl = [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(pfad),
        "-f", "s16le", "-ac", "1", "-ar", str(ABTASTRATE), "-",
    ]
    roh = subprocess.run(befehl, capture_output=True, check=True).stdout
    return np.frombuffer(roh, np.int16).astype(np.float32) / 32768.0


def main():
    parser = argparse.ArgumentParser(description="Audiodatei transkribieren")
    parser.add_argument("datei", type=Path, help="Audiodatei unter DATEN/audio/<quelle>/")
    parser.add_argument("--sprache", help="Sprache erzwingen, etwa de oder en")
    parser.add_argument("--neu", action="store_true", help="vorhandenes Transkript überschreiben")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)

    audio_pfad = args.datei.resolve()
    if not audio_pfad.exists():
        raise SystemExit(f"Datei nicht gefunden: {audio_pfad}")
    kennung = audio_pfad.parent.name
    quelle = next((q for q in konfiguration["quelle"] if q["kennung"] == kennung), {})
    sprache = args.sprache or quelle.get("sprache")

    ziel = Path(konfiguration["pfade"]["daten"]) / "transkripte" / kennung
    ziel_json = ziel / f"{audio_pfad.stem}.json"
    ziel_txt = ziel / f"{audio_pfad.stem}.txt"
    if ziel_json.exists() and not args.neu:
        print(f"Schon vorhanden: {ziel_json}")
        return

    beginn = time.monotonic()
    print("Dekodiere Audio ...")
    audio = lies_audio(audio_pfad)
    dauer = len(audio) / ABTASTRATE

    whisper = konfiguration["whisper"]
    print(f"Lade Modell auf Karte {whisper['karte']} ...")
    modell = WhisperModel(
        konfiguration["pfade"]["whisper_modell"],
        device="cuda",
        device_index=whisper["karte"],
        compute_type=whisper["rechenart"],
    )
    abschnitte_roh, info = modell.transcribe(audio, language=sprache, vad_filter=True, beam_size=5)
    print(f"Sprache {info.language}, Dauer {zeitmarke(dauer)}")

    abschnitte = []
    naechste_meldung = 0
    for abschnitt in abschnitte_roh:
        abschnitte.append({
            "start": round(abschnitt.start, 2),
            "ende": round(abschnitt.end, 2),
            "text": abschnitt.text.strip(),
        })
        if abschnitt.end >= naechste_meldung:
            print(f"  {zeitmarke(abschnitt.end)} von {zeitmarke(dauer)}")
            naechste_meldung += 300
    laufzeit = time.monotonic() - beginn

    ziel.mkdir(parents=True, exist_ok=True)
    ergebnis = {
        "audio": str(audio_pfad),
        "quelle": kennung,
        "sprache": info.language,
        "modell": konfiguration["pfade"]["whisper_modell"],
        "dauer": round(dauer, 1),
        "laufzeit": round(laufzeit, 1),
        "abschnitte": abschnitte,
    }
    ziel_json.write_text(json.dumps(ergebnis, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(ziel_txt, "w", encoding="utf-8") as text:
        for a in abschnitte:
            text.write(f"[{zeitmarke(a['start'])}] {a['text']}\n")

    woerter = sum(len(a["text"].split()) for a in abschnitte)
    print(f"Fertig in {zeitmarke(laufzeit)}, {dauer / laufzeit:.1f}-fache Echtzeit, {woerter} Wörter")
    print(f"Gespeichert: {ziel_txt}")


if __name__ == "__main__":
    main()
