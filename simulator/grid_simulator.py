"""Grid Simulator – Virtueller Shelly aus CSV + Zendure-Mock-Kopplung.

===================================================================

Lädt Shelly-CSV-Daten und stellt sie als virtuellen Shelly bereit.
Die Batterie-Einspeisung (Zendure Mock) wird auf Phase B subtrahiert,
so dass der Controller die gleiche Sicht hat wie in der Realität.

CSV-Formate:
  - 3-Phasen: Timestamp, Phase A, Phase B, Phase C, Total Power
  - 1-Phase:  Timestamp, Power (W)
    → Phase A = 5%, Phase C = 30%, Phase B = 65% (geschätzte Verteilung)

Timing:
  - get_phase_powers() liefert den Wert zum aktuellen Simulationszeitpunkt
  - Die Batterie-Einspeisung (grid_output_power) wird von Phase B abgezogen
  - Interpolation oder Halteglied zwischen CSV-Datenpunkten
"""

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PhaseRecord:
    """Ein Datenpunkt mit 3 Phasen."""

    timestamp: float  # Unix timestamp
    phase_a: float
    phase_b: float
    phase_c: float

    @property
    def total(self) -> float:
        return self.phase_a + self.phase_b + self.phase_c


def load_csv(path: Path) -> List[PhaseRecord]:
    """Lädt CSV-Datei und gibt Liste von PhaseRecords zurück.

    Erkennt automatisch ob 3-Phasen oder 1-Phasen Format.
    Füllt fehlende Werte mit dem letzten gültigen Wert auf.
    """
    records: List[PhaseRecord] = []

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)

        is_3phase = len(header) >= 4 and "Phase A" in header[1]

        last_valid: Optional[PhaseRecord] = None

        for row in reader:
            if not row or not row[0].strip():
                continue

            try:
                ts_str = row[0].strip()
                dt = datetime.fromisoformat(ts_str)
                ts = dt.timestamp()

                if is_3phase:
                    phase_a = float(row[1])
                    phase_b = float(row[2])
                    phase_c = float(row[3])
                else:
                    total = float(row[1])
                    # Geschätzte Verteilung für 1-Phasen-Daten
                    phase_a = total * 0.05
                    phase_b = total * 0.65
                    phase_c = total * 0.30

                rec = PhaseRecord(
                    timestamp=ts,
                    phase_a=phase_a,
                    phase_b=phase_b,
                    phase_c=phase_c,
                )
                last_valid = rec
                records.append(rec)

            except (ValueError, IndexError):
                # Fehlende/ungültige Daten → letzten gültigen Wert verwenden
                if last_valid is not None:
                    try:
                        ts_str = row[0].strip()
                        dt = datetime.fromisoformat(ts_str)
                        ts = dt.timestamp()
                        records.append(
                            PhaseRecord(
                                timestamp=ts,
                                phase_a=last_valid.phase_a,
                                phase_b=last_valid.phase_b,
                                phase_c=last_valid.phase_c,
                            )
                        )
                    except (ValueError, IndexError):
                        continue

    logger.info(
        "CSV geladen: %s → %d Datenpunkte (%s)",
        path.name,
        len(records),
        "3-Phasen" if is_3phase else "1-Phase → geschätzte Verteilung",
    )
    return records


def clean_csv_data(records: List[PhaseRecord]) -> List[PhaseRecord]:
    """Bereinigt CSV-Daten.

    - Entfernt Duplikate (gleicher Timestamp)
    - Sortiert nach Timestamp
    - Füllt Lücken > 2s mit Hold-Werten auf (~1s Raster).
    """
    if not records:
        return records

    # Sortieren und Duplikate entfernen
    records.sort(key=lambda r: r.timestamp)
    cleaned: List[PhaseRecord] = [records[0]]

    for rec in records[1:]:
        if rec.timestamp <= cleaned[-1].timestamp:
            continue  # Duplikat überspringen

        gap = rec.timestamp - cleaned[-1].timestamp
        if gap > 2.0:
            # Lücke auffüllen mit Hold-Werten (letzter gültiger Wert)
            last = cleaned[-1]
            fill_ts = last.timestamp + 1.0
            while fill_ts < rec.timestamp - 0.5:
                cleaned.append(
                    PhaseRecord(
                        timestamp=fill_ts,
                        phase_a=last.phase_a,
                        phase_b=last.phase_b,
                        phase_c=last.phase_c,
                    )
                )
                fill_ts += 1.0

        cleaned.append(rec)

    return cleaned


class GridSimulator:
    """Virtueller Shelly-Stromzähler aus CSV-Daten + Zendure-Mock-Kopplung.

    Implementiert das GridMeter Protocol für den ZeroFeed V3 Controller.
    Die Batterie-Einspeisung wird von Phase B abgezogen.
    """

    def __init__(
        self,
        records: List[PhaseRecord],
        battery_mock=None,
    ):
        """Initialisiert den GridSimulator.

        records: Bereinigte CSV-Daten
        battery_mock: Optional SolarFlowAsyncMockClient
                       (get_grid_output_power() wird von Phase B abgezogen).
        """
        self._records = records
        self._battery = battery_mock
        self._index = 0
        self._sim_time: Optional[float] = None

        if records:
            self._start_time = records[0].timestamp
            self._end_time = records[-1].timestamp
        else:
            self._start_time = 0.0
            self._end_time = 0.0

    @property
    def start_time(self) -> float:
        return self._start_time

    @property
    def end_time(self) -> float:
        return self._end_time

    @property
    def duration_s(self) -> float:
        return self._end_time - self._start_time

    @property
    def num_records(self) -> int:
        return len(self._records)

    def set_simulation_time(self, t: float) -> None:
        """Setzt Simulationszeit (Unix Timestamp)."""
        self._sim_time = t

    def _get_time(self) -> float:
        return self._sim_time if self._sim_time is not None else 0.0

    def _find_record(self, t: float) -> Optional[PhaseRecord]:
        """Findet den passenden Record für Zeitpunkt t (Hold-Verhalten)."""
        if not self._records:
            return None

        # Schneller Forward-Scan ab letztem Index
        while (
            self._index < len(self._records) - 1 and self._records[self._index + 1].timestamp <= t
        ):
            self._index += 1

        # Rückwärts-Check (falls Zeit zurückgesetzt wurde)
        while self._index > 0 and self._records[self._index].timestamp > t:
            self._index -= 1

        return self._records[self._index]

    async def get_phase_powers(self) -> Optional[Tuple[float, float, float]]:
        """Liefert (phase_a, phase_b, phase_c) in Watt.

        Phase B wird um die Batterie-Einspeisung reduziert.
        """
        t = self._get_time()
        rec = self._find_record(t)
        if rec is None:
            return None

        phase_a = rec.phase_a
        phase_b = rec.phase_b
        phase_c = rec.phase_c

        # Batterie-Einspeisung von Phase B abziehen
        if self._battery is not None:
            battery_power = self._battery.get_grid_output_power()
            phase_b -= battery_power

        return (phase_a, phase_b, phase_c)

    async def get_total_power(self) -> Optional[float]:
        """Gesamtleistung aller Phasen."""
        phases = await self.get_phase_powers()
        if phases is None:
            return None
        return sum(phases)

    def get_records_in_range(self, start: float, end: float) -> List[PhaseRecord]:
        """Gibt Records im Zeitbereich [start, end] zurück."""
        result = []
        for rec in self._records:
            if rec.timestamp < start:
                continue
            if rec.timestamp > end:
                break
            result.append(rec)
        return result
