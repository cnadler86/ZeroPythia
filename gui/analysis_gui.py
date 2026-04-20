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
import math
import sys
import tkinter as tk
from dataclasses import fields, is_dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
import matplotlib.dates as mdates

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
from src.controller.phase_controller import (
    InverterPhaseControllerSettings,
    PhaseControllerSettings,
    ZeroFeedManagerSettings,
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
        self._plot_figures: Dict[str, Figure] = {}
        self._plot_canvases: Dict[str, FigureCanvasTkAgg] = {}

        self._create_widgets()
        self._populate_params()

    def _create_default_settings(self) -> ZeroFeedV3Settings:
        return ZeroFeedV3Settings(
            manager=ZeroFeedManagerSettings(
                min_output_w=20,
                max_output_w=800,
                target_power_w=3.0,
            ),
            phase_controller=PhaseControllerSettings(
                kp=1.0,
                hysteresis_w=5.0,
                kp_hysteresis=0.3,
            ),
            inverter_controller=InverterPhaseControllerSettings(
                kp_draw=0.95,
                kp_feed_in=1.05,
                hysteresis_w=10.0,
                kp_hysteresis=0.3,
                target_power_w=3.0,
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
        ttk.Button(action_frame, text="Batch Simulieren", command=self._run_batch).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(action_frame, text="Optimieren", command=self._run_optimization).pack(
            fill=tk.X, pady=2
        )
        ttk.Button(action_frame, text="Reset", command=self._reset_params).pack(fill=tk.X, pady=2)

        # === Right Panel: Plot + Stats ===
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=1)

        # Plot-Tabs
        self._plot_tabs = ttk.Notebook(right_frame)
        self._plot_tabs.pack(fill=tk.BOTH, expand=True)

        self._create_plot_tab("A", "Phase A")
        self._create_plot_tab("B", "Phase B")
        self._create_plot_tab("C", "Phase C")
        self._create_plot_tab("ALL", "Alles")

        # Stats
        stats_frame = ttk.LabelFrame(right_frame, text="Statistiken")
        stats_frame.pack(fill=tk.X, padx=5, pady=5)

        self._stats_text = tk.Text(stats_frame, height=6, font=("Consolas", 9))
        self._stats_text.pack(fill=tk.X, padx=5, pady=5)

    def _create_plot_tab(self, key: str, title: str) -> None:
        tab = ttk.Frame(self._plot_tabs)
        self._plot_tabs.add(tab, text=title)

        fig = Figure(figsize=(10, 7), dpi=100)
        canvas = FigureCanvasTkAgg(fig, master=tab)
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        toolbar = NavigationToolbar2Tk(canvas, tab)
        toolbar.update()
        toolbar.pack(fill=tk.X)

        self._plot_figures[key] = fig
        self._plot_canvases[key] = canvas

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
                ttk.Separator(self._param_frame, orient="horizontal").pack(fill=tk.X, pady=5)
                ttk.Label(self._param_frame, text=group.upper(), font=("", 9, "bold")).pack(
                    anchor="w", padx=5
                )

            row = ttk.Frame(self._param_frame)
            row.pack(fill=tk.X, padx=5, pady=1)

            ttk.Label(row, text=short_name, width=25, anchor="w").pack(side=tk.LEFT)

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
            self._file_label.config(text=f"{len(self._csv_files)} Datei(en) ausgewählt")

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
            self._file_label.config(text=f"{len(self._csv_files)} Datei(en) in {folder_path.name}")

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
            mean_eff = sum(s.efficiency_pct for _, s in self._results) / len(self._results)
            mean_band = sum(s.time_in_band_pct for _, s in self._results) / len(self._results)
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
                f"#{i + 1} Score={r.score:.1f}  "
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
            self._stats_text.insert(tk.END, "\n→ Beste Parameter übernommen!\n")

    def _plot_result(self, result: SimulationResult, stats: Statistics):
        """Plottet Simulationsergebnis in allen Reitern."""
        if not result.timestamps:
            return

        # Zeitachse als echte Datetime für lesbare Uhrzeiten
        t_axis = [datetime.fromtimestamp(t) for t in result.timestamps]

        self._plot_phase_tab(
            key="A",
            title=f"{result.csv_file} - Phase A",
            t_axis=t_axis,
            phase_values=result.phase_a,
            correction_values=result.phase_a_correction,
            osc_limits=result.phase_a_osc_limit,
            controlled_grid_values=[
                p - c for p, c in zip(result.phase_a, result.phase_a_correction)
            ],
            controlled_label="Geregeltes Grid A",
        )
        self._plot_phase_b_tab(result=result, t_axis=t_axis)
        self._plot_phase_tab(
            key="C",
            title=f"{result.csv_file} - Phase C",
            t_axis=t_axis,
            phase_values=result.phase_c,
            correction_values=result.phase_c_correction,
            osc_limits=result.phase_c_osc_limit,
            controlled_grid_values=[
                p - c for p, c in zip(result.phase_c, result.phase_c_correction)
            ],
            controlled_label="Geregeltes Grid C",
        )
        self._plot_all_tab(result=result, stats=stats, t_axis=t_axis)

    def _plot_phase_tab(
        self,
        key: str,
        title: str,
        t_axis: List[datetime],
        phase_values: List[float],
        correction_values: List[float],
        osc_limits: List[float],
        controlled_grid_values: List[float],
        controlled_label: str,
    ) -> None:
        fig = self._plot_figures[key]
        fig.clear()

        ax1 = fig.add_subplot(2, 1, 1)
        ax2 = fig.add_subplot(2, 1, 2, sharex=ax1)

        target_w = self.settings.manager.target_power_w
        osc_active = [math.isfinite(v) for v in osc_limits]
        self._shade_oscillation_regions(ax1, t_axis, osc_active)
        self._shade_oscillation_regions(ax2, t_axis, osc_active)

        ax1.plot(
            t_axis,
            phase_values,
            label=f"CSV-Grid {key}",
            color="#1f4e79",
            linewidth=1.2,
        )
        ax1.plot(
            t_axis,
            correction_values,
            label=f"Controller-Correction {key}",
            color="#d97706",
            linewidth=1.1,
        )
        ax1.axhline(y=0, color="red", linestyle="--", alpha=0.3)
        ax1.set_ylabel("Leistung [W]")
        ax1.set_title(title)
        ax1.legend(loc="upper right", fontsize=8)
        ax1.grid(True, alpha=0.22)

        ax2.plot(
            t_axis,
            controlled_grid_values,
            label=controlled_label,
            color="#0f766e",
            linewidth=1.2,
        )
        ax2.axhline(y=target_w, color="#15803d", linestyle="--", alpha=0.7, label="Zielwert")
        ax2.axhline(y=0, color="red", linestyle="--", alpha=0.22)
        ax2.set_ylabel("Leistung [W]")
        ax2.set_xlabel("Uhrzeit")
        ax2.legend(loc="upper right", fontsize=8)
        ax2.grid(True, alpha=0.22)
        self._format_time_axis(ax2)
        self._bind_lower_autoscale_on_zoom(
            fig=fig,
            ax_top=ax1,
            ax_bottom=ax2,
            t_axis=t_axis,
            y_series=[controlled_grid_values],
            reference_lines=[target_w, 0.0],
        )

        fig.tight_layout()
        self._plot_canvases[key].draw()

    def _plot_phase_b_tab(self, result: SimulationResult, t_axis: List[datetime]) -> None:
        key = "B"
        fig = self._plot_figures[key]
        fig.clear()

        ax1 = fig.add_subplot(2, 1, 1)
        ax2 = fig.add_subplot(2, 1, 2, sharex=ax1)

        target_w = self.settings.manager.target_power_w
        osc_active = [math.isfinite(v) for v in result.phase_b_osc_limit]
        controlled_total_grid = [
            g - c for g, c in zip(result.grid_total, result.phase_b_correction)
        ]

        self._shade_oscillation_regions(ax1, t_axis, osc_active)
        self._shade_oscillation_regions(ax2, t_axis, osc_active)

        ax1.plot(t_axis, result.phase_b, label="CSV-Grid B", color="#1f4e79", linewidth=1.2)
        ax1.plot(
            t_axis,
            result.phase_b_correction,
            label="Controller-Correction B",
            color="#d97706",
            linewidth=1.1,
        )
        ax1.axhline(y=0, color="red", linestyle="--", alpha=0.3)
        ax1.set_ylabel("Leistung [W]")
        ax1.set_title(f"{result.csv_file} - Phase B")
        ax1.legend(loc="upper right", fontsize=8)
        ax1.grid(True, alpha=0.22)

        ax2.plot(
            t_axis,
            controlled_total_grid,
            label="Geregeltes Total-Grid durch Regler B",
            color="#0f766e",
            linewidth=1.2,
        )
        ax2.plot(
            t_axis,
            result.grid_total,
            label="Ist-Total-Grid",
            color="#6b7280",
            linewidth=0.9,
            alpha=0.65,
        )
        ax2.axhline(y=target_w, color="#15803d", linestyle="--", alpha=0.7, label="Zielwert")
        ax2.axhline(y=0, color="red", linestyle="--", alpha=0.22)
        ax2.set_ylabel("Leistung [W]")
        ax2.set_xlabel("Uhrzeit")
        ax2.legend(loc="upper right", fontsize=8)
        ax2.grid(True, alpha=0.22)
        self._format_time_axis(ax2)
        self._bind_lower_autoscale_on_zoom(
            fig=fig,
            ax_top=ax1,
            ax_bottom=ax2,
            t_axis=t_axis,
            y_series=[controlled_total_grid, result.grid_total],
            reference_lines=[target_w, 0.0],
        )

        fig.tight_layout()
        self._plot_canvases[key].draw()

    def _plot_all_tab(
        self, result: SimulationResult, stats: Statistics, t_axis: List[datetime]
    ) -> None:
        fig = self._plot_figures["ALL"]
        fig.clear()

        # 3 Subplots
        ax1 = fig.add_subplot(3, 1, 1)
        ax2 = fig.add_subplot(3, 1, 2, sharex=ax1)
        ax3 = fig.add_subplot(3, 1, 3, sharex=ax1)

        osc_active = [v < self.settings.manager.max_output_w for v in result.osc_limits]
        self._shade_oscillation_regions(ax1, t_axis, osc_active)
        self._shade_oscillation_regions(ax2, t_axis, osc_active)

        # --- Plot 1: Grid Power (3 Phasen + Total) ---
        ax1.plot(t_axis, result.phase_a, label="Phase A", alpha=0.6, linewidth=0.5)
        ax1.plot(t_axis, result.phase_b, label="Phase B", alpha=0.6, linewidth=0.5)
        ax1.plot(t_axis, result.phase_c, label="Phase C", alpha=0.6, linewidth=0.5)
        ax1.plot(t_axis, result.grid_total, label="Total", color="black", linewidth=1.0)
        target_w = self.settings.manager.target_power_w
        ax1.axhline(
            y=target_w, color="green", linestyle="--", alpha=0.5, label=f"Ziel ({target_w:.0f}W)"
        )
        ax1.axhline(y=0, color="red", linestyle="--", alpha=0.3)
        ax1.set_ylabel("Grid Power [W]")
        ax1.set_title(
            f"{result.csv_file} - Mean={stats.mean_grid_w:+.1f}W  "
            f"Band={stats.time_in_band_pct:.0f}%  Eff={stats.efficiency_pct:.0f}%"
        )
        ax1.legend(loc="upper right", fontsize=7)
        ax1.grid(True, alpha=0.22)

        # --- Plot 2: Battery Output + Setpoint ---
        ax2.plot(
            t_axis,
            result.battery_output,
            label="Battery Output",
            color="orange",
            linewidth=0.8,
        )
        ax2.plot(t_axis, result.setpoints, label="Setpoint", color="blue", linewidth=1.0, alpha=0.7)
        ax2.plot(
            t_axis,
            result.osc_limits,
            label="Osc Limit",
            color="red",
            linewidth=0.8,
            alpha=0.5,
            linestyle="--",
        )
        ax2.set_ylabel("Power [W]")
        ax2.legend(loc="upper right", fontsize=7)
        ax2.grid(True, alpha=0.22)

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

        ax3.plot(t_axis, import_wh, label="Import", color="red")
        ax3.plot(t_axis, export_wh, label="Export", color="green")
        ax3.plot(t_axis, battery_wh, label="Batterie", color="orange")
        ax3.set_ylabel("Energie [Wh]")
        ax3.set_xlabel("Uhrzeit")
        ax3.legend(loc="upper left", fontsize=7)
        ax3.grid(True, alpha=0.22)
        self._format_time_axis(ax3)

        fig.tight_layout()
        self._plot_canvases["ALL"].draw()

    def _shade_oscillation_regions(self, ax, t_axis: List[datetime], active: List[bool]) -> None:
        """Markiert zusammenhängende Oszillationsbereiche als Hintergrund."""
        if not t_axis or not active:
            return

        start_idx: Optional[int] = None
        for idx, is_active in enumerate(active):
            if is_active and start_idx is None:
                start_idx = idx
            elif not is_active and start_idx is not None:
                ax.axvspan(t_axis[start_idx], t_axis[idx], color="#fee2e2", alpha=0.45, lw=0)
                start_idx = None

        if start_idx is not None:
            ax.axvspan(t_axis[start_idx], t_axis[-1], color="#fee2e2", alpha=0.45, lw=0)

    def _format_time_axis(self, ax) -> None:
        """Dynamische Zeitformatierung für Zoom-Stufen bis Sekunden."""
        locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
        formatter = mdates.ConciseDateFormatter(locator)
        formatter.formats = ["%Y", "%b", "%d", "%H:%M", "%H:%M", "%H:%M:%S"]
        formatter.zero_formats = ["", "%Y", "%b", "%d", "%H:%M", "%H:%M:%S"]
        formatter.offset_formats = ["", "%Y", "%Y-%b", "%Y-%b-%d", "%Y-%b-%d", "%Y-%b-%d"]
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)

    def _bind_lower_autoscale_on_zoom(
        self,
        fig: Figure,
        ax_top,
        ax_bottom,
        t_axis: List[datetime],
        y_series: List[List[float]],
        reference_lines: List[float],
    ) -> None:
        """Koppelt Y-Autoskalierung des unteren Plots an X-Zoom im oberen Plot."""
        if not t_axis or not y_series:
            return

        x_nums = mdates.date2num(t_axis)

        def _rescale_bottom(_ax=None) -> None:
            x_min, x_max = ax_bottom.get_xlim()
            if x_min > x_max:
                x_min, x_max = x_max, x_min

            visible_idx = [i for i, x in enumerate(x_nums) if x_min <= x <= x_max]
            if not visible_idx:
                return

            y_vals: list[float] = []
            for series in y_series:
                y_vals.extend(series[i] for i in visible_idx if i < len(series))
            y_vals.extend(reference_lines)

            if not y_vals:
                return

            y_min = min(y_vals)
            y_max = max(y_vals)
            y_range = y_max - y_min
            pad = 1.0 if y_range <= 0 else y_range * 0.08
            ax_bottom.set_ylim(y_min - pad, y_max + pad)
            fig.canvas.draw_idle()

        ax_top.callbacks.connect("xlim_changed", _rescale_bottom)
        _rescale_bottom()

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
