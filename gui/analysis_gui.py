"""ZeroFeed V3 Analyse-GUI.
=========================

GUI für den ZeroFeed V3 Controller mit:
- Dateiauswahl (einzeln oder Batch)
- Parameter-Anpassung
- Simulation + Live-Plot
- Statistiken
- Optimierung
"""

import asyncio
import logging
import sys
import tkinter as tk
from dataclasses import fields, is_dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends._backend_tk import NavigationToolbar2Tk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from simulator.batch_runner import (
    SimulationResult,
    Statistics,
    compute_statistics,
    run_simulation,
)
from simulator.grid_simulator import clean_csv_data, load_csv
from simulator.optimizer import ParameterOptimizer
from src.controller.oscillation_detectorv2 import (
    BaseloadHolderSettings,
    BaseloadPredictorSettings,
)
from src.controller.phase_controllers import (
    BatteryPhaseControllerSettings,
    DisturbanceControllerSettings,
    PhaseManagerSettings,
)
from src.controller.zerofeed_v3 import ZeroFeedV3Settings

logger = logging.getLogger(__name__)


class ZeroFeedV3GUI:
    """GUI für ZeroFeed V3 Analyse."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ZeroFeed V3 Analyse")
        self.root.geometry("1400x900")

        self.settings = self._create_default_settings()
        self._csv_files: List[Path] = []
        self._results: List[Tuple[SimulationResult, Statistics]] = []
        self._current_result: Optional[SimulationResult] = None
        self._current_stats: Optional[Statistics] = None

        self._param_entries: Dict[str, tk.StringVar] = {}

        self._create_widgets()
        self._populate_params()

    def _create_default_settings(self) -> ZeroFeedV3Settings:
        return ZeroFeedV3Settings(
            manager=PhaseManagerSettings(
                min_output_w=20,
                max_output_w=800,
                target_total_grid_w=5.0,
                min_change_w=3.0,
            ),
            disturbance=DisturbanceControllerSettings(
                kp=1.0,
                hysteresis_w=5.0,
                kp_hysteresis=0.3,
            ),
            battery_phase=BatteryPhaseControllerSettings(
                kp_draw=0.95,
                kp_feed_in=1.05,
                hysteresis_w=10.0,
                kp_hysteresis=0.3,
                target_power_w=5.0,
            ),
            holder_settings=BaseloadHolderSettings(),
            predictor_settings=BaseloadPredictorSettings(),
            sampling_interval=1.0,
            control_interval_s=3.0,
        )

    def _create_widgets(self):
        # Main layout: Left panel (params) | Right panel (plot + stats)
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # === Left Panel: Parameters ===
        left_frame = ttk.Frame(main_paned, width=350)
        main_paned.add(left_frame, weight=0)

        # File selection
        file_frame = ttk.LabelFrame(left_frame, text="CSV-Dateien")
        file_frame.pack(fill=tk.X, padx=5, pady=5)

        btn_row = ttk.Frame(file_frame)
        btn_row.pack(fill=tk.X, padx=5, pady=2)
        ttk.Button(btn_row, text="Datei(en)...", command=self._select_files).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(btn_row, text="Ordner...", command=self._select_folder).pack(
            side=tk.LEFT, padx=2
        )

        self._file_label = ttk.Label(file_frame, text="Keine Dateien ausgewählt")
        self._file_label.pack(fill=tk.X, padx=5, pady=2)

        # Parameters (scrollable)
        param_outer = ttk.LabelFrame(left_frame, text="Parameter")
        param_outer.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        canvas = tk.Canvas(param_outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(param_outer, orient="vertical", command=canvas.yview)
        self._param_frame = ttk.Frame(canvas)

        self._param_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self._param_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Action buttons
        action_frame = ttk.Frame(left_frame)
        action_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Button(action_frame, text="Simulieren", command=self._run_simulation).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(
            action_frame, text="Batch Simulieren", command=self._run_batch
        ).pack(fill=tk.X, pady=2)
        ttk.Button(
            action_frame, text="Optimieren", command=self._run_optimization
        ).pack(fill=tk.X, pady=2)
        ttk.Button(action_frame, text="Reset", command=self._reset_params).pack(
            fill=tk.X, pady=2
        )

        # === Right Panel: Plot + Stats ===
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=1)

        # Plot
        self._fig = Figure(figsize=(10, 7), dpi=100)
        self._canvas = FigureCanvasTkAgg(self._fig, master=right_frame)
        self._canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        toolbar = NavigationToolbar2Tk(self._canvas, right_frame)
        toolbar.update()
        toolbar.pack(fill=tk.X)

        # Stats
        stats_frame = ttk.LabelFrame(right_frame, text="Statistiken")
        stats_frame.pack(fill=tk.X, padx=5, pady=5)

        self._stats_text = tk.Text(stats_frame, height=6, font=("Consolas", 9))
        self._stats_text.pack(fill=tk.X, padx=5, pady=5)

    def _extract_fields(self, obj: Any, prefix: str = "") -> List[Tuple[str, str, Any, type]]:
        """Extrahiert alle Felder aus einem Dataclass (rekursiv)."""
        result = []
        if not is_dataclass(obj):
            return result

        for f in fields(obj):
            val = getattr(obj, f.name)
            full_name = f"{prefix}.{f.name}" if prefix else f.name

            if is_dataclass(val):
                result.extend(self._extract_fields(val, full_name))
            elif isinstance(val, (int, float, str, bool)):
                result.append((full_name, f.name, val, type(val)))

        return result

    def _populate_params(self):
        """Befüllt das Parameter-Panel."""
        for widget in self._param_frame.winfo_children():
            widget.destroy()

        self._param_entries.clear()
        all_fields = self._extract_fields(self.settings)

        current_group = ""
        for full_name, short_name, value, _typ in all_fields:
            # Gruppierung nach Prefix
            parts = full_name.split(".")
            group = parts[0] if len(parts) > 1 else "general"

            if group != current_group:
                current_group = group
                ttk.Separator(self._param_frame, orient="horizontal").pack(
                    fill=tk.X, pady=5
                )
                ttk.Label(
                    self._param_frame, text=group.upper(), font=("", 9, "bold")
                ).pack(anchor="w", padx=5)

            row = ttk.Frame(self._param_frame)
            row.pack(fill=tk.X, padx=5, pady=1)

            ttk.Label(row, text=short_name, width=25, anchor="w").pack(
                side=tk.LEFT
            )

            var = tk.StringVar(value=str(value))
            self._param_entries[full_name] = var
            entry = ttk.Entry(row, textvariable=var, width=12)
            entry.pack(side=tk.RIGHT)

    def _apply_params(self) -> bool:
        """Wendet die GUI-Parameter auf die Settings an."""
        try:
            settings = self._create_default_settings()

            for full_name, var in self._param_entries.items():
                parts = full_name.split(".")
                value_str = var.get()

                # Navigate to the right object
                obj = settings
                for part in parts[:-1]:
                    obj = getattr(obj, part)

                field_name = parts[-1]
                current_val = getattr(obj, field_name)

                # Convert
                if isinstance(current_val, bool):
                    new_val = value_str.lower() in ("true", "1", "yes")
                elif isinstance(current_val, int):
                    new_val = int(float(value_str))
                elif isinstance(current_val, float):
                    new_val = float(value_str)
                else:
                    new_val = value_str

                setattr(obj, field_name, new_val)

            self.settings = settings
            return True
        except Exception as e:
            messagebox.showerror("Parameter-Fehler", str(e))
            return False

    def _reset_params(self):
        self.settings = self._create_default_settings()
        self._populate_params()

    def _select_files(self):
        files = filedialog.askopenfilenames(
            title="CSV-Dateien auswählen",
            filetypes=[("CSV", "*.csv")],
            initialdir=str(
                Path(__file__).parent.parent.parent.parent / "shelly_logger" / "ShellyLog"
            ),
        )
        if files:
            self._csv_files = [Path(f) for f in files]
            self._file_label.config(
                text=f"{len(self._csv_files)} Datei(en) ausgewählt"
            )

    def _select_folder(self):
        folder = filedialog.askdirectory(
            title="CSV-Ordner auswählen",
            initialdir=str(
                Path(__file__).parent.parent.parent.parent / "shelly_logger" / "ShellyLog"
            ),
        )
        if folder:
            folder_path = Path(folder)
            self._csv_files = sorted(folder_path.glob("shelly3em_power_*.csv"))
            # Filter auf 3-Phasen
            filtered = []
            for f in self._csv_files:
                with open(f, "r", encoding="utf-8") as fh:
                    header = fh.readline()
                    if "Phase A" in header:
                        filtered.append(f)
            if filtered:
                self._csv_files = filtered
            self._file_label.config(
                text=f"{len(self._csv_files)} Datei(en) in {folder_path.name}"
            )

    def _run_simulation(self):
        """Simuliert die erste ausgewählte Datei."""
        if not self._csv_files:
            messagebox.showwarning("Keine Datei", "Bitte zuerst CSV-Datei auswählen")
            return

        if not self._apply_params():
            return

        csv_file = self._csv_files[0]
        self._stats_text.delete("1.0", tk.END)
        self._stats_text.insert(tk.END, f"Simuliere {csv_file.name}...\n")
        self.root.update()

        asyncio.run(self._async_simulate(csv_file))

    async def _async_simulate(self, csv_file: Path):
        records = load_csv(csv_file)
        records = clean_csv_data(records)

        if not records:
            messagebox.showwarning("Keine Daten", f"Keine gültigen Daten in {csv_file.name}")
            return

        result = await run_simulation(
            records=records,
            settings=self.settings,
            csv_name=csv_file.name,
            show_progress=False,
        )

        stats = compute_statistics(result)
        self._current_result = result
        self._current_stats = stats
        self._results = [(result, stats)]

        self._plot_result(result, stats)
        self._show_stats(stats)

    def _run_batch(self):
        """Simuliert alle ausgewählten Dateien."""
        if not self._csv_files:
            messagebox.showwarning("Keine Dateien", "Bitte zuerst CSV-Dateien auswählen")
            return

        if not self._apply_params():
            return

        self._stats_text.delete("1.0", tk.END)
        self._stats_text.insert(tk.END, f"Batch-Simulation: {len(self._csv_files)} Dateien...\n")
        self.root.update()

        asyncio.run(self._async_batch())

    async def _async_batch(self):
        self._results = []

        for csv_file in self._csv_files:
            records = load_csv(csv_file)
            records = clean_csv_data(records)
            if not records:
                continue

            result = await run_simulation(
                records=records,
                settings=self.settings,
                csv_name=csv_file.name,
                show_progress=False,
            )
            stats = compute_statistics(result)
            self._results.append((result, stats))

            self._stats_text.insert(
                tk.END,
                f"  {csv_file.name}: mean={stats.mean_grid_w:+.1f}W "
                f"band={stats.time_in_band_pct:.0f}% "
                f"eff={stats.efficiency_pct:.0f}%\n",
            )
            self.root.update()

        if self._results:
            # Zeige letztes Ergebnis im Plot
            result, stats = self._results[-1]
            self._current_result = result
            self._current_stats = stats
            self._plot_result(result, stats)

            # Zusammenfassung
            mean_eff = sum(s.efficiency_pct for _, s in self._results) / len(
                self._results
            )
            mean_band = sum(s.time_in_band_pct for _, s in self._results) / len(
                self._results
            )
            self._stats_text.insert(
                tk.END,
                f"\nGesamt: Ø Eff={mean_eff:.1f}%  Ø Band={mean_band:.1f}%\n",
            )

    def _run_optimization(self):
        """Startet Parameter-Optimierung."""
        if not self._csv_files:
            messagebox.showwarning(
                "Keine Dateien",
                "Bitte zuerst CSV-Dateien auswählen (Ordner empfohlen)",
            )
            return

        if not self._apply_params():
            return

        self._stats_text.delete("1.0", tk.END)
        self._stats_text.insert(tk.END, "Starte Optimierung...\n")
        self.root.update()

        # Verwende das Verzeichnis der ersten Datei
        csv_dir = self._csv_files[0].parent

        asyncio.run(self._async_optimize(csv_dir))

    async def _async_optimize(self, csv_dir: Path):
        optimizer = ParameterOptimizer(
            csv_dir=csv_dir,
            base_settings=self.settings,
            three_phase_only=True,
        )

        results = await optimizer.optimize_phase_controller()

        self._stats_text.delete("1.0", tk.END)
        self._stats_text.insert(tk.END, "=== Optimierungsergebnisse ===\n\n")

        for i, r in enumerate(results[:10]):
            self._stats_text.insert(
                tk.END,
                f"#{i+1} Score={r.score:.1f}  "
                f"Eff={r.mean_efficiency:.1f}%  "
                f"Band={r.mean_band_pct:.1f}%  "
                f"Export={r.total_export_wh:.0f}Wh\n"
                f"   {r.label}\n\n",
            )

        # Bestes Ergebnis als neue Settings übernehmen
        if results:
            best = results[0]
            self.settings = best.settings
            self._populate_params()
            self._stats_text.insert(
                tk.END, "\n→ Beste Parameter übernommen!\n"
            )

    def _plot_result(self, result: SimulationResult, stats: Statistics):
        """Plottet Simulationsergebnis."""
        self._fig.clear()

        if not result.timestamps:
            return

        # Zeitachse in Stunden ab Start
        t0 = result.timestamps[0]
        t_h = [(t - t0) / 3600 for t in result.timestamps]

        # 3 Subplots
        ax1 = self._fig.add_subplot(3, 1, 1)
        ax2 = self._fig.add_subplot(3, 1, 2, sharex=ax1)
        ax3 = self._fig.add_subplot(3, 1, 3, sharex=ax1)

        # --- Plot 1: Grid Power (3 Phasen + Total) ---
        ax1.plot(t_h, result.phase_a, label="Phase A", alpha=0.6, linewidth=0.5)
        ax1.plot(t_h, result.phase_b, label="Phase B", alpha=0.6, linewidth=0.5)
        ax1.plot(t_h, result.phase_c, label="Phase C", alpha=0.6, linewidth=0.5)
        ax1.plot(t_h, result.grid_total, label="Total", color="black", linewidth=1.0)
        ax1.axhline(y=5, color="green", linestyle="--", alpha=0.5, label="Ziel (5W)")
        ax1.axhline(y=0, color="red", linestyle="--", alpha=0.3)
        ax1.set_ylabel("Grid Power [W]")
        ax1.set_title(
            f"{result.csv_file} — Mean={stats.mean_grid_w:+.1f}W  "
            f"Band={stats.time_in_band_pct:.0f}%  Eff={stats.efficiency_pct:.0f}%"
        )
        ax1.legend(loc="upper right", fontsize=7)
        ax1.grid(True, alpha=0.3)

        # --- Plot 2: Battery Output + Setpoint ---
        ax2.plot(
            t_h, result.battery_output, label="Battery Output", color="orange", linewidth=0.8
        )
        ax2.plot(
            t_h, result.setpoints, label="Setpoint", color="blue", linewidth=1.0, alpha=0.7
        )
        ax2.plot(
            t_h, result.osc_limits, label="Osc Limit", color="red", linewidth=0.8, alpha=0.5, linestyle="--"
        )
        ax2.set_ylabel("Power [W]")
        ax2.legend(loc="upper right", fontsize=7)
        ax2.grid(True, alpha=0.3)

        # --- Plot 3: Cumulative Energy ---
        import_wh = []
        export_wh = []
        battery_wh = []
        cum_import = 0.0
        cum_export = 0.0
        cum_battery = 0.0

        for i in range(len(result.timestamps)):
            if i > 0:
                dt_h = (result.timestamps[i] - result.timestamps[i - 1]) / 3600.0
                p = result.grid_total[i]
                if p > 0:
                    cum_import += p * dt_h
                else:
                    cum_export += abs(p) * dt_h
                cum_battery += result.battery_output[i] * dt_h

            import_wh.append(cum_import)
            export_wh.append(cum_export)
            battery_wh.append(cum_battery)

        ax3.plot(t_h, import_wh, label="Import", color="red")
        ax3.plot(t_h, export_wh, label="Export", color="green")
        ax3.plot(t_h, battery_wh, label="Batterie", color="orange")
        ax3.set_ylabel("Energie [Wh]")
        ax3.set_xlabel("Zeit [h]")
        ax3.legend(loc="upper left", fontsize=7)
        ax3.grid(True, alpha=0.3)

        self._fig.tight_layout()
        self._canvas.draw()

    def _show_stats(self, stats: Statistics):
        """Zeigt Statistiken im Text-Widget."""
        self._stats_text.delete("1.0", tk.END)

        text = (
            f"Datei:      {stats.csv_file}\n"
            f"Dauer:      {stats.duration_h:.1f}h  |  Samples: {stats.num_samples}\n"
            f"Grid:       Mean={stats.mean_grid_w:+.1f}W  Std={stats.std_grid_w:.1f}W  "
            f"Min={stats.min_grid_w:.0f}W  Max={stats.max_grid_w:.0f}W\n"
            f"Zielband:   {stats.time_in_band_pct:.1f}% (±20W um 5W)\n"
            f"Energie:    Import={stats.total_grid_import_wh:.0f}Wh  "
            f"Export={stats.total_grid_export_wh:.0f}Wh  "
            f"Batterie={stats.total_battery_wh:.0f}Wh\n"
            f"Effizienz:  {stats.efficiency_pct:.1f}%  |  "
            f"Oszillation: {stats.oscillation_time_pct:.1f}%\n"
        )

        self._stats_text.insert(tk.END, text)


def main():
    root = tk.Tk()
    ZeroFeedV3GUI(root)
    root.mainloop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    main()
