"""CSV-Logger für ZeroFeed V3.

==========================

Loggt pro Tag eine CSV-Datei mit zwei Zeilentypen:

  sample  – Shelly-Messwerte, Batterie-Output, Oszillationsdetektor-Zustände
             Wird mit jedem Sampling-Zyklus (~1s) geschrieben.

  control – Regler-Interna: Feedback, Feedforward, Setpoint, Osc-Limit
             Wird mit jedem Regel-Zyklus (~1–3s) geschrieben.

CSV-Format (alle Zeilen):
  iso_timestamp, unix_ts, event_type, ...spalten

sample-Spalten:
  phase_a_w, phase_b_w, phase_c_w, total_grid_w,
  battery_output_w, real_consumption_w,
  osc_A_oscillating, osc_A_limit_w,
  osc_B_oscillating, osc_B_limit_w,
  osc_C_oscillating, osc_C_limit_w,
  osc_total_oscillating, osc_total_limit_w

control-Spalten:
  feedback_output_w, ff_output_w, raw_setpoint_w,
  osc_limit_w, final_setpoint_w, setpoint_changed

Nicht geloggt: Zendure-Interna (SOC, Limits) – diese Werte werden im Regelkreis
nicht genutzt und können bei Bedarf separat abgefragt werden.
"""

import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── CSV-Spalten ─────────────────────────────────────────────────────────────

_COMMON_FIELDS = ["iso_timestamp", "unix_ts", "event_type"]

_SAMPLE_FIELDS = [
    "phase_a_w",
    "phase_b_w",
    "phase_c_w",
    "total_grid_w",
    "battery_output_w",
    "real_consumption_w",
    # Oszillationsdetektor pro Phase
    "osc_A_oscillating",
    "osc_A_limit_w",
    "osc_B_oscillating",
    "osc_B_limit_w",
    "osc_C_oscillating",
    "osc_C_limit_w",
    "osc_total_oscillating",
    "osc_total_limit_w",
]

_CONTROL_FIELDS = [
    "feedback_output_w",
    "ff_output_w",
    "raw_setpoint_w",
    "osc_limit_w",
    "final_setpoint_w",
    "setpoint_changed",
]

# Alle möglichen Felder in der Header-Zeile (leere Felder → "")
_ALL_FIELDS = _COMMON_FIELDS + _SAMPLE_FIELDS + _CONTROL_FIELDS


# ── Datenklassen ─────────────────────────────────────────────────────────────


class SampleLogEntry:
    """Werte für eine sample-Zeile."""

    __slots__ = (
        "unix_ts",
        "phase_a_w",
        "phase_b_w",
        "phase_c_w",
        "total_grid_w",
        "battery_output_w",
        "real_consumption_w",
        "osc_A_oscillating",
        "osc_A_limit_w",
        "osc_B_oscillating",
        "osc_B_limit_w",
        "osc_C_oscillating",
        "osc_C_limit_w",
        "osc_total_oscillating",
        "osc_total_limit_w",
    )

    def __init__(
        self,
        unix_ts: float,
        phase_a_w: float,
        phase_b_w: float,
        phase_c_w: float,
        battery_output_w: float,
        osc_A_oscillating: bool,
        osc_A_limit_w: float,
        osc_B_oscillating: bool,
        osc_B_limit_w: float,
        osc_C_oscillating: bool,
        osc_C_limit_w: float,
        osc_total_oscillating: bool,
        osc_total_limit_w: float,
    ):
        self.unix_ts = unix_ts
        self.phase_a_w = phase_a_w
        self.phase_b_w = phase_b_w
        self.phase_c_w = phase_c_w
        self.total_grid_w = phase_a_w + phase_b_w + phase_c_w
        self.battery_output_w = battery_output_w
        self.real_consumption_w = self.total_grid_w + battery_output_w
        self.osc_A_oscillating = osc_A_oscillating
        self.osc_A_limit_w = osc_A_limit_w
        self.osc_B_oscillating = osc_B_oscillating
        self.osc_B_limit_w = osc_B_limit_w
        self.osc_C_oscillating = osc_C_oscillating
        self.osc_C_limit_w = osc_C_limit_w
        self.osc_total_oscillating = osc_total_oscillating
        self.osc_total_limit_w = osc_total_limit_w

    def to_row(self) -> dict:
        iso = datetime.fromtimestamp(self.unix_ts, tz=timezone.utc).isoformat()
        return {
            "iso_timestamp": iso,
            "unix_ts": f"{self.unix_ts:.3f}",
            "event_type": "sample",
            "phase_a_w": f"{self.phase_a_w:.2f}",
            "phase_b_w": f"{self.phase_b_w:.2f}",
            "phase_c_w": f"{self.phase_c_w:.2f}",
            "total_grid_w": f"{self.total_grid_w:.2f}",
            "battery_output_w": f"{self.battery_output_w:.2f}",
            "real_consumption_w": f"{self.real_consumption_w:.2f}",
            "osc_A_oscillating": int(self.osc_A_oscillating),
            "osc_A_limit_w": f"{self.osc_A_limit_w:.1f}" if self.osc_A_oscillating else "",
            "osc_B_oscillating": int(self.osc_B_oscillating),
            "osc_B_limit_w": f"{self.osc_B_limit_w:.1f}" if self.osc_B_oscillating else "",
            "osc_C_oscillating": int(self.osc_C_oscillating),
            "osc_C_limit_w": f"{self.osc_C_limit_w:.1f}" if self.osc_C_oscillating else "",
            "osc_total_oscillating": int(self.osc_total_oscillating),
            "osc_total_limit_w": f"{self.osc_total_limit_w:.1f}"
            if self.osc_total_oscillating
            else "",
        }


class ControlLogEntry:
    """Werte für eine control-Zeile."""

    __slots__ = (
        "unix_ts",
        "feedback_output_w",
        "ff_output_w",
        "raw_setpoint_w",
        "osc_limit_w",
        "final_setpoint_w",
        "setpoint_changed",
    )

    def __init__(
        self,
        unix_ts: float,
        feedback_output_w: float,
        ff_output_w: float,
        raw_setpoint_w: int,
        osc_limit_w: float,
        final_setpoint_w: int,
        setpoint_changed: bool,
    ):
        self.unix_ts = unix_ts
        self.feedback_output_w = feedback_output_w
        self.ff_output_w = ff_output_w
        self.raw_setpoint_w = raw_setpoint_w
        self.osc_limit_w = osc_limit_w
        self.final_setpoint_w = final_setpoint_w
        self.setpoint_changed = setpoint_changed

    def to_row(self) -> dict:
        iso = datetime.fromtimestamp(self.unix_ts, tz=timezone.utc).isoformat()
        return {
            "iso_timestamp": iso,
            "unix_ts": f"{self.unix_ts:.3f}",
            "event_type": "control",
            "feedback_output_w": f"{self.feedback_output_w:.1f}",
            "ff_output_w": f"{self.ff_output_w:.1f}",
            "raw_setpoint_w": self.raw_setpoint_w,
            "osc_limit_w": f"{self.osc_limit_w:.1f}",
            "final_setpoint_w": self.final_setpoint_w,
            "setpoint_changed": int(self.setpoint_changed),
        }


# ── Logger ───────────────────────────────────────────────────────────────────


class ZeroFeedCSVLogger:
    """Schreibt ZeroFeed V3 Telemetrie in tagesweise CSV-Dateien.

    Dateinamen-Schema:
        zerofeed_v3_YYYY-MM-DD.csv   (Datum in Ortszeit)

    Thread- und asyncio-sicher: alle Schreiboperationen sind synchron
    (csv.writer puffert selbst), und der asyncio Event-Loop ist
    single-threaded – kein Lock nötig.
    """

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._current_date: Optional[str] = None  # "YYYY-MM-DD"
        self._file: Optional[io.TextIOWrapper] = None
        self._writer: Optional[csv.DictWriter] = None

    # ── Öffentliche API ──────────────────────────────────────────────

    def log_sample(self, entry: SampleLogEntry) -> None:
        """Schreibt eine sample-Zeile."""
        self._ensure_file(entry.unix_ts)
        writer = self._writer
        file = self._file
        if writer is None or file is None:
            logger.error("CSV-Log nicht initialisiert (sample)")
            return
        try:
            writer.writerow(entry.to_row())
            file.flush()
        except Exception as e:
            logger.error("CSV-Log Schreibfehler (sample): %s", e)

    def log_control(self, entry: ControlLogEntry) -> None:
        """Schreibt eine control-Zeile."""
        self._ensure_file(entry.unix_ts)
        writer = self._writer
        file = self._file
        if writer is None or file is None:
            logger.error("CSV-Log nicht initialisiert (control)")
            return
        try:
            writer.writerow(entry.to_row())
            file.flush()
        except Exception as e:
            logger.error("CSV-Log Schreibfehler (control): %s", e)

    def close(self) -> None:
        """Schließt die aktuelle Log-Datei."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                logger.debug("CSV-Log konnte nicht sauber geschlossen werden", exc_info=True)
            self._file = None
            self._writer = None
            self._current_date = None

    def current_log_path(self) -> Optional[Path]:
        """Gibt den Pfad der aktuell geöffneten Datei zurück."""
        if self._current_date is None:
            return None
        return self.log_dir / f"zerofeed_v3_{self._current_date}.csv"

    # ── Interne Methoden ─────────────────────────────────────────────

    def _ensure_file(self, unix_ts: float) -> None:
        """Stellt sicher dass die korrekte Tages-Datei geöffnet ist."""
        date_str = datetime.fromtimestamp(unix_ts).strftime("%Y-%m-%d")
        if date_str == self._current_date:
            return  # Datei bereits offen und aktuell

        # Datumswechsel oder erster Aufruf
        self.close()
        self._current_date = date_str

        path = self.log_dir / f"zerofeed_v3_{date_str}.csv"
        is_new = not path.exists()

        self._file = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(
            self._file,
            fieldnames=_ALL_FIELDS,
            extrasaction="ignore",
            restval="",
        )

        if is_new:
            self._writer.writeheader()
            self._file.flush()

        logger.info("CSV-Log: %s", path)
