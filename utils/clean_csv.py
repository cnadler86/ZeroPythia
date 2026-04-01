"""CSV-Daten bereinigen.

=====================

Liest alle Shelly-CSV-Dateien, bereinigt sie und überschreibt die Originale.
Bereinigung:
  - Fehlende Werte mit letztem gültigen Wert auffüllen
  - Duplikate (gleicher Timestamp) entfernen
  - Sortierung nach Timestamp sicherstellen
"""

import csv
import sys
from datetime import datetime
from pathlib import Path

from simulator.grid_simulator import PhaseRecord, clean_csv_data, load_csv


def write_csv_3phase(records: list[PhaseRecord], path: Path) -> None:
    """Schreibt bereinigte 3-Phasen-Daten."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "Phase A", "Phase B", "Phase C", "Total Power"])
        for rec in records:
            dt = datetime.fromtimestamp(rec.timestamp)
            writer.writerow(
                [
                    dt.isoformat(),
                    round(rec.phase_a, 2),
                    round(rec.phase_b, 2),
                    round(rec.phase_c, 2),
                    round(rec.total, 2),
                ]
            )


def write_csv_1phase(records: list[PhaseRecord], path: Path) -> None:
    """Schreibt bereinigte 1-Phasen-Daten."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "Power (W)"])
        for rec in records:
            dt = datetime.fromtimestamp(rec.timestamp)
            writer.writerow([dt.isoformat(), round(rec.total, 2)])


def clean_all_csvs(csv_dir: Path, overwrite: bool = True) -> None:
    """Bereinigt alle CSV-Dateien im Verzeichnis."""
    csv_files = sorted(csv_dir.glob("shelly3em_power_*.csv"))

    for csv_file in csv_files:
        # Header prüfen
        with open(csv_file, "r", encoding="utf-8") as f:
            header = f.readline()
        is_3phase = "Phase A" in header

        # Laden und bereinigen
        records = load_csv(csv_file)
        original_count = len(records)
        records = clean_csv_data(records)
        cleaned_count = len(records)

        diff = cleaned_count - original_count

        if diff != 0:
            status = f"bereinigt: {original_count} → {cleaned_count} ({diff:+d} Punkte)"
        else:
            status = f"OK ({original_count} Punkte)"

        print(f"  {csv_file.name}: {status}")

        if overwrite and records:
            if is_3phase:
                write_csv_3phase(records, csv_file)
            else:
                write_csv_1phase(records, csv_file)


if __name__ == "__main__":
    # HEMS/src/tools/clean_csv.py → HEMS → ../shelly_logger
    csv_dir = Path(__file__).resolve().parent.parent.parent.parent / "shelly_logger" / "ShellyLog"

    if not csv_dir.exists():
        print(f"Verzeichnis nicht gefunden: {csv_dir}")
        sys.exit(1)

    print(f"Bereinige CSV-Dateien in: {csv_dir}")
    print()
    clean_all_csvs(csv_dir, overwrite=True)
    print()
    print("Fertig!")
