"""Status einer Notiz auf „geprüft“ setzen, committen und die Seite neu hochladen.

Aufruf:
  .venv/bin/python pruefen.py TEIL_DES_NAMENS            auf „geprüft“ setzen
  .venv/bin/python pruefen.py TEIL_DES_NAMENS --zurueck  wieder auf „ungeprüft“ setzen

Gesucht wird im Dateinamen der Notizen, ohne Rücksicht auf Groß- und Kleinschreibung,
etwa „2026-11-14_314“ oder „kachelofen“. Passt mehr als eine Notiz, zeigt das Skript
die Treffer und ändert nichts.

Läuft gerade der Tagesbetrieb, wird nur der Status gesetzt und committet. Auf die Seite
kommt die Änderung dann beim nächsten Hochladen.
"""

import argparse
import fcntl
import re
import subprocess
import sys
import tomllib
from pathlib import Path

PROJEKT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description="Notiz als geprüft markieren")
    parser.add_argument("suche", help="Teil des Dateinamens der Notiz")
    parser.add_argument("--zurueck", action="store_true", help="wieder auf „ungeprüft“ setzen")
    args = parser.parse_args()

    with open(PROJEKT / "config.toml", "rb") as datei:
        konfiguration = tomllib.load(datei)
    repo = Path(konfiguration["pfade"]["notizen"])
    ordner = repo / "inhalt" / "notizen"
    treffer = sorted(p for p in ordner.glob("*.md") if args.suche.lower() in p.stem.lower())
    if not treffer:
        raise SystemExit(f"Keine Notiz enthält „{args.suche}“ im Namen.")
    if len(treffer) > 1:
        print(f"{len(treffer)} Notizen passen, bitte genauer:")
        for p in treffer:
            print(f"  {p.stem}")
        sys.exit(1)

    notiz = treffer[0]
    neu = "ungeprüft" if args.zurueck else "geprüft"
    text = notiz.read_text(encoding="utf-8")
    geaendert, anzahl = re.subn(r'^status: ".*"$', f'status: "{neu}"', text, count=1, flags=re.MULTILINE)
    if anzahl == 0:
        raise SystemExit(f"In {notiz.name} steht keine Zeile „status:“.")
    if geaendert == text:
        print(f"{notiz.stem} ist schon „{neu}“.")
        return
    notiz.write_text(geaendert, encoding="utf-8")
    datei = str(notiz.relative_to(repo))
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"{notiz.stem}: {neu}", "--", datei], check=True)
    print(f"{notiz.stem}: {neu}, committet")

    sperre = open(Path(konfiguration["pfade"]["daten"]) / "tagesbetrieb.lock", "w")  # noqa: SIM115
    try:
        fcntl.flock(sperre, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Der Tagesbetrieb läuft gerade. Auf die Seite kommt die Änderung beim nächsten Hochladen.")
        return
    try:
        ergebnis = subprocess.run([sys.executable, "bauen.py", "--hochladen"], cwd=PROJEKT,
                                  capture_output=True, text=True)
    finally:
        sperre.close()
    if ergebnis.returncode != 0:
        print(ergebnis.stdout[-2000:], ergebnis.stderr[-2000:], sep="\n")
        raise SystemExit("Hochladen fehlgeschlagen.")
    print("Seite neu gebaut und hochgeladen.")


if __name__ == "__main__":
    main()
