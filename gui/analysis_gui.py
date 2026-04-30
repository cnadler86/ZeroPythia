"""ZeroFeed V4 Analyse-GUI.

GUI für den ZeroFeed V4 Regler mit:
- CSV-Dateiauswahl (einzeln oder Ordner)
- V4 Parameter-Anpassung (alle Phasen, Oszillation, PT1)
- Einzel-Simulation + Batch-Simulation
- Live-Plots pro Phase + Gesamtübersicht
- Statistiken
"""

import logging
import math
import sys
import tkinter as tk
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

sys.path.insert(0, str(Path(__file__).parent.parent))

from simulator.batch_runner import (
    SimulationResult,
    Statistics,
    compute_statistics,
    run_simulation,
)
from simulator.grid_simulator import clean_csv_data, load_csv
from src.config.zerofeed_v4 import ZeroFeedV4Config, config_to_flat, flat_to_config
from src.dashboard.regulators.v4_adapter import ZeroFeedV4Regulator

logger = logging.getLogger(__name__)


def _parse_value(value_str: str, schema_entry: Dict[str, Any]) -> Any:
    t = schema_entry.get("type", "number")
    if t == "boolean":
        return value_str.lower() in ("true", "1", "yes")
    if t == "integer":
        return int(float(value_str))
    if t == "string":
        return value_str
    return float(value_str)


class ZeroFeedV4GUI:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("ZeroFeed V4 Analyse")
        self.root.geometry("1440x900")

        self._config: ZeroFeedV4Config = ZeroFeedV4Config()
        self._csv_files: List[Path] = []
        self._results: List[Tuple[SimulationResult, Statistics]] = []
        self._current_result: Optional[SimulationResult] = None
        self._current_stats: Optional[Statistics] = None
        self._schema: Dict[str, Any] = ZeroFeedV4Regulator(settings=self._config).settings_schema()
        self._param_vars: Dict[str, tk.StringVar] = {}
        self._plot_figures: Dict[str, Figure] = {}
        self._plot_canvases: Dict[str, FigureCanvasTkAgg] = {}

        self._create_widgets()
        self._populate_params()

    def _create_widgets(self) -> None:
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_frame = ttk.Frame(main_paned, width=360)
        main_paned.add(left_frame, weight=0)

        file_frame = ttk.LabelFrame(left_frame, text="CSV-Dateien")
        file_frame.pack(fill=tk.X, padx=5, pady=5)
        btn_row = ttk.Frame(file_frame)
        btn_row.pack(fill=tk.X, padx=5, pady=2)
        ttk.Button(btn_row, text="Datei(en)...", command=self._select_files).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_row, text="Ordner...", command=self._select_folder).pack(side=tk.LEFT, padx=2)
        self._file_label = ttk.Label(file_frame, text="Keine Dateien ausgewählt")
        self._file_label.pack(fill=tk.X, padx=5, pady=2)

        param_outer = ttk.LabelFrame(left_frame, text="V4 Parameter")
        param_outer.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        cv = tk.Canvas(param_outer, highlightthickness=0)
        sb = ttk.Scrollbar(param_outer, orient="vertical", command=cv.yview)
        self._param_frame = ttk.Frame(cv)
        self._param_frame.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=self._param_frame, anchor="nw")
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        cv.bind_all("<MouseWheel>", lambda e: cv.yview_scroll(int(-1*(e.delta/120)), "units"))

        action_frame = ttk.Frame(left_frame)
        action_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(action_frame, text="Simulieren", command=self._run_simulation).pack(fill=tk.X, pady=2)
        ttk.Button(action_frame, text="Batch Simulieren", command=self._run_batch).pack(fill=tk.X, pady=2)
        ttk.Button(action_frame, text="Reset", command=self._reset_params).pack(fill=tk.X, pady=2)

        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=1)
        self._plot_tabs = ttk.Notebook(right_frame)
        self._plot_tabs.pack(fill=tk.BOTH, expand=True)
        for key, title in [("A", "Phase A"), ("B", "Phase B"), ("C", "Phase C"), ("ALL", "Alles")]:
            self._create_plot_tab(key, title)

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

    def _populate_params(self) -> None:
        for w in self._param_frame.winfo_children():
            w.destroy()
        self._param_vars.clear()
        flat = config_to_flat(self._config)
        current_group = ""
        for key, entry in self._schema.items():
            group = entry.get("group", "")
            if group != current_group:
                current_group = group
                ttk.Separator(self._param_frame, orient="horizontal").pack(fill=tk.X, pady=4)
                ttk.Label(self._param_frame, text=group.upper(), font=("", 9, "bold")).pack(anchor="w", padx=5)
            row = ttk.Frame(self._param_frame)
            row.pack(fill=tk.X, padx=5, pady=1)
            ttk.Label(row, text=entry.get("title", key), width=28, anchor="w").pack(side=tk.LEFT)
            current_val = flat.get(key, entry.get("default", ""))
            var = tk.StringVar(value=str(current_val))
            self._param_vars[key] = var
            if entry.get("type") == "boolean":
                var.set("True" if current_val else "False")
                ttk.Checkbutton(row, variable=var, onvalue="True", offvalue="False").pack(side=tk.RIGHT)
            elif entry.get("enum"):
                ttk.Combobox(row, textvariable=var, values=entry["enum"], width=8, state="readonly").pack(side=tk.RIGHT)
            else:
                ttk.Entry(row, textvariable=var, width=10).pack(side=tk.RIGHT)

    def _apply_params(self) -> bool:
        try:
            flat: Dict[str, Any] = {}
            for key, var in self._param_vars.items():
                flat[key] = _parse_value(var.get(), self._schema.get(key, {}))
            self._config = flat_to_config(flat, self._config)
            self._schema = ZeroFeedV4Regulator(settings=self._config).settings_schema()
            return True
        except Exception as e:
            messagebox.showerror("Parameter-Fehler", str(e))
            return False

    def _reset_params(self) -> None:
        self._config = ZeroFeedV4Config()
        self._schema = ZeroFeedV4Regulator(settings=self._config).settings_schema()
        self._populate_params()

    def _select_files(self) -> None:
        files = filedialog.askopenfilenames(title="CSV-Dateien auswählen", filetypes=[("CSV", "*.csv")])
        if files:
            self._csv_files = [Path(f) for f in files]
            self._file_label.config(text=f"{len(self._csv_files)} Datei(en) ausgewählt")

    def _select_folder(self) -> None:
        folder = filedialog.askdirectory(title="CSV-Ordner auswählen")
        if not folder:
            return
        folder_path = Path(folder)
        all_csv = sorted(folder_path.glob("*.csv"))
        filtered = []
        for f in all_csv:
            try:
                with open(f, encoding="utf-8") as fh:
                    header = fh.readline()
                if "Phase A" in header:
                    filtered.append(f)
            except OSError:
                pass
        self._csv_files = filtered if filtered else all_csv
        self._file_label.config(text=f"{len(self._csv_files)} Datei(en) in {folder_path.name}")

    def _run_simulation(self) -> None:
        if not self._csv_files:
            messagebox.showwarning("Keine Datei", "Bitte zuerst CSV-Datei auswählen")
            return
        if not self._apply_params():
            return
        csv_file = self._csv_files[0]
        self._stats_text.delete("1.0", tk.END)
        self._stats_text.insert(tk.END, f"Simuliere {csv_file.name}...\n")
        self.root.update()
        records = load_csv(csv_file)
        records = clean_csv_data(records)
        if not records:
            messagebox.showwarning("Keine Daten", f"Keine gültigen Daten in {csv_file.name}")
            return
        result = run_simulation(records, self._config, csv_name=csv_file.name)
        stats = compute_statistics(result, target_power_w=self._config.target_power_w)
        self._current_result = result
        self._current_stats = stats
        self._results = [(result, stats)]
        self._plot_result(result, stats)
        self._show_stats(stats)

    def _run_batch(self) -> None:
        if not self._csv_files:
            messagebox.showwarning("Keine Dateien", "Bitte zuerst CSV-Dateien auswählen")
            return
        if not self._apply_params():
            return
        self._stats_text.delete("1.0", tk.END)
        self._stats_text.insert(tk.END, f"Batch-Simulation: {len(self._csv_files)} Dateien...\n")
        self.root.update()
        self._results = []
        for csv_file in self._csv_files:
            records = load_csv(csv_file)
            records = clean_csv_data(records)
            if not records:
                continue
            result = run_simulation(records, self._config, csv_name=csv_file.name)
            stats = compute_statistics(result, target_power_w=self._config.target_power_w)
            self._results.append((result, stats))
            self._stats_text.insert(
                tk.END,
                f"  {csv_file.name}: mean={stats.mean_grid_w:+.1f}W "
                f"band={stats.time_in_band_pct:.0f}% eff={stats.efficiency_pct:.0f}%\n",
            )
            self.root.update()
        if self._results:
            result, stats = self._results[-1]
            self._current_result = result
            self._current_stats = stats
            self._plot_result(result, stats)
            mean_eff = sum(s.efficiency_pct for _, s in self._results) / len(self._results)
            mean_band = sum(s.time_in_band_pct for _, s in self._results) / len(self._results)
            self._stats_text.insert(tk.END, f"\nGesamt: Ø Eff={mean_eff:.1f}%  Ø Band={mean_band:.1f}%\n")

    def _plot_result(self, result: SimulationResult, stats: Statistics) -> None:
        if not result.timestamps:
            return
        t_axis = [datetime.fromtimestamp(t) for t in result.timestamps]
        ctrl_ph = result.control_phase
        for phase in ("A", "B", "C"):
            if phase == ctrl_ph:
                self._plot_fb_tab(result, t_axis, phase)
            else:
                self._plot_ff_tab(result, t_axis, phase)
        self._plot_all_tab(result, stats, t_axis)

    def _plot_ff_tab(self, result: SimulationResult, t_axis: List[datetime], phase: str) -> None:
        fig = self._plot_figures[phase]
        fig.clear()
        ax1 = fig.add_subplot(2, 1, 1)
        ax2 = fig.add_subplot(2, 1, 2, sharex=ax1)
        grid_vals = result.phase_values(phase)
        corr_vals = result.correction(phase)
        osc_active = result.osc_active(phase)
        effective = [g - c for g, c in zip(grid_vals, corr_vals)]
        x = mdates.date2num(t_axis)
        self._shade_osc(ax1, t_axis, osc_active)
        self._shade_osc(ax2, t_axis, osc_active)
        ax1.plot(x, grid_vals, label=f"CSV-Grid {phase}", color="#1f4e79", linewidth=1.2)
        ax1.plot(x, corr_vals, label=f"FF-Anfrage {phase}", color="#d97706", linewidth=1.1)
        ax1.axhline(y=0, color="red", linestyle="--", alpha=0.3)
        ax1.set_ylabel("Leistung [W]")
        ax1.set_title(f"{result.csv_file} – Phase {phase} (Feedforward-Steuerung)")
        ax1.legend(loc="upper right", fontsize=8)
        ax1.grid(True, alpha=0.22)
        ax2.plot(x, effective, label=f"Effektiv Grid {phase}", color="#0f766e", linewidth=1.2)
        ax2.axhline(y=0, color="#15803d", linestyle="--", alpha=0.7, label="Ziel 0W")
        ax2.set_ylabel("Leistung [W]")
        ax2.set_xlabel("Uhrzeit")
        ax2.legend(loc="upper right", fontsize=8)
        ax2.grid(True, alpha=0.22)
        self._format_time_axis(ax2)
        self._bind_lower_autoscale(fig, ax1, ax2, t_axis, [effective], [0.0])
        fig.tight_layout()
        self._plot_canvases[phase].draw()

    def _plot_fb_tab(self, result: SimulationResult, t_axis: List[datetime], phase: str) -> None:
        fig = self._plot_figures[phase]
        fig.clear()
        ax1 = fig.add_subplot(2, 1, 1)
        ax2 = fig.add_subplot(2, 1, 2, sharex=ax1)
        target_w = self._config.target_power_w
        grid_ctrl = result.phase_values(phase)
        osc_active = result.osc_active(phase)
        x = mdates.date2num(t_axis)
        self._shade_osc(ax1, t_axis, osc_active)
        self._shade_osc(ax2, t_axis, osc_active)
        ax1.plot(x, grid_ctrl, label=f"Grid {phase} (Batterie abgezogen)", color="#1f4e79", linewidth=1.2)
        ax1.plot(x, result.fb_correction, label="FB-Korrektur", color="#d97706", linewidth=1.1)
        ax1.plot(x, result.ff_sum, label="FF-Summe", color="#7c3aed", linewidth=0.9, alpha=0.7)
        ax1.axhline(y=0, color="red", linestyle="--", alpha=0.3)
        ax1.set_ylabel("Leistung [W]")
        ax1.set_title(f"{result.csv_file} – Phase {phase} (Feedback-Regelung / Batterie-Phase)")
        ax1.legend(loc="upper right", fontsize=8)
        ax1.grid(True, alpha=0.22)
        ax2.plot(x, result.grid_total, label="Total Grid", color="#0f766e", linewidth=1.2)
        ax2.plot(x, result.battery_output, label="Batterie (PT1)", color="#ea580c", linewidth=0.9, alpha=0.8)
        ax2.plot(x, result.setpoints, label="Setpoint", color="#2563eb", linewidth=1.0, alpha=0.7)
        ax2.axhline(y=target_w, color="#15803d", linestyle="--", alpha=0.7, label=f"Ziel {target_w:.0f}W")
        ax2.axhline(y=0, color="red", linestyle="--", alpha=0.22)
        ax2.set_ylabel("Leistung [W]")
        ax2.set_xlabel("Uhrzeit")
        ax2.legend(loc="upper right", fontsize=8)
        ax2.grid(True, alpha=0.22)
        self._format_time_axis(ax2)
        self._bind_lower_autoscale(fig, ax1, ax2, t_axis, [result.grid_total, result.battery_output, result.setpoints], [target_w, 0.0])
        fig.tight_layout()
        self._plot_canvases[phase].draw()

    def _plot_all_tab(self, result: SimulationResult, stats: Statistics, t_axis: List[datetime]) -> None:
        fig = self._plot_figures["ALL"]
        fig.clear()
        ax1 = fig.add_subplot(3, 1, 1)
        ax2 = fig.add_subplot(3, 1, 2, sharex=ax1)
        ax3 = fig.add_subplot(3, 1, 3, sharex=ax1)
        target_w = self._config.target_power_w
        x = mdates.date2num(t_axis)
        osc_combined = [a or b or c for a, b, c in zip(result.osc_active_a, result.osc_active_b, result.osc_active_c)]
        for ax in (ax1, ax2):
            self._shade_osc(ax, t_axis, osc_combined)
        ax1.plot(x, result.phase_a, label="Phase A", alpha=0.6, linewidth=0.7)
        ax1.plot(x, result.phase_b, label="Phase B", alpha=0.6, linewidth=0.7)
        ax1.plot(x, result.phase_c, label="Phase C", alpha=0.6, linewidth=0.7)
        ax1.plot(x, result.grid_total, label="Total", color="black", linewidth=1.1)
        ax1.axhline(y=target_w, color="green", linestyle="--", alpha=0.5, label=f"Ziel {target_w:.0f}W")
        ax1.axhline(y=0, color="red", linestyle="--", alpha=0.3)
        ax1.set_ylabel("Grid Power [W]")
        ax1.set_title(f"{result.csv_file}  ·  Mean={stats.mean_grid_w:+.1f}W  Band={stats.time_in_band_pct:.0f}%  Eff={stats.efficiency_pct:.0f}%  ctrl={result.control_phase}")
        ax1.legend(loc="upper right", fontsize=7)
        ax1.grid(True, alpha=0.22)
        ax2.plot(x, result.battery_output, label="Batterie (PT1)", color="orange", linewidth=0.9)
        ax2.plot(x, result.setpoints, label="Setpoint", color="blue", linewidth=1.0, alpha=0.7)
        ax2.plot(x, result.ff_sum, label="FF-Summe", color="#7c3aed", linewidth=0.8, alpha=0.7)
        ax2.set_ylabel("Power [W]")
        ax2.legend(loc="upper right", fontsize=7)
        ax2.grid(True, alpha=0.22)
        import_wh, export_wh, battery_wh = [], [], []
        cum_i = cum_e = cum_b = 0.0
        for i in range(len(result.timestamps)):
            if i > 0:
                dt_h = (result.timestamps[i] - result.timestamps[i - 1]) / 3600.0
                p = result.grid_total[i]
                if p > 0:
                    cum_i += p * dt_h
                else:
                    cum_e += abs(p) * dt_h
                cum_b += result.battery_output[i] * dt_h
            import_wh.append(cum_i)
            export_wh.append(cum_e)
            battery_wh.append(cum_b)
        ax3.plot(x, import_wh, label="Import", color="red")
        ax3.plot(x, export_wh, label="Export", color="green")
        ax3.plot(x, battery_wh, label="Batterie", color="orange")
        ax3.set_ylabel("Energie [Wh]")
        ax3.set_xlabel("Uhrzeit")
        ax3.legend(loc="upper left", fontsize=7)
        ax3.grid(True, alpha=0.22)
        self._format_time_axis(ax3)
        fig.tight_layout()
        self._plot_canvases["ALL"].draw()

    def _shade_osc(self, ax, t_axis: List[datetime], active: List[bool]) -> None:
        start: Optional[int] = None
        for idx, is_active in enumerate(active):
            if is_active and start is None:
                start = idx
            elif not is_active and start is not None:
                ax.axvspan(t_axis[start], t_axis[idx], color="#fee2e2", alpha=0.45, lw=0)
                start = None
        if start is not None:
            ax.axvspan(t_axis[start], t_axis[-1], color="#fee2e2", alpha=0.45, lw=0)

    def _format_time_axis(self, ax) -> None:
        locator = mdates.AutoDateLocator(minticks=4, maxticks=10)
        formatter = mdates.ConciseDateFormatter(locator)
        formatter.formats = ["%Y", "%b", "%d", "%H:%M", "%H:%M", "%H:%M:%S"]
        formatter.zero_formats = ["", "%Y", "%b", "%d", "%H:%M", "%H:%M:%S"]
        formatter.offset_formats = ["", "%Y", "%Y-%b", "%Y-%b-%d", "%Y-%b-%d", "%Y-%b-%d"]
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)

    def _bind_lower_autoscale(self, fig, ax_top, ax_bottom, t_axis, y_series, reference_lines) -> None:
        if not t_axis or not y_series:
            return
        x_nums = mdates.date2num(t_axis)
        def _rescale(_ax=None) -> None:
            x_min, x_max = ax_bottom.get_xlim()
            if x_min > x_max:
                x_min, x_max = x_max, x_min
            vis = [i for i, x in enumerate(x_nums) if x_min <= x <= x_max]
            if not vis:
                return
            y_vals = [s[i] for s in y_series for i in vis if i < len(s)]
            y_vals.extend(reference_lines)
            if not y_vals:
                return
            y_min, y_max = min(y_vals), max(y_vals)
            pad = max(1.0, (y_max - y_min) * 0.08)
            ax_bottom.set_ylim(y_min - pad, y_max + pad)
            fig.canvas.draw_idle()
        ax_top.callbacks.connect("xlim_changed", _rescale)
        _rescale()

    def _show_stats(self, stats: Statistics) -> None:
        self._stats_text.delete("1.0", tk.END)
        self._stats_text.insert(
            tk.END,
            f"Datei:      {stats.csv_file}\n"
            f"Dauer:      {stats.duration_h:.1f}h  |  Samples: {stats.num_samples}\n"
            f"Grid:       Mean={stats.mean_grid_w:+.1f}W  Std={stats.std_grid_w:.1f}W  "
            f"Min={stats.min_grid_w:.0f}W  Max={stats.max_grid_w:.0f}W\n"
            f"Zielband:   {stats.time_in_band_pct:.1f}% (+-20W um {self._config.target_power_w:.0f}W)\n"
            f"Energie:    Import={stats.total_grid_import_wh:.0f}Wh  "
            f"Export={stats.total_grid_export_wh:.0f}Wh  "
            f"Batterie={stats.total_battery_wh:.0f}Wh\n"
            f"Effizienz:  {stats.efficiency_pct:.1f}%  |  "
            f"Oszillation: {stats.oscillation_time_pct:.1f}%\n",
        )


def main() -> None:
    root = tk.Tk()
    ZeroFeedV4GUI(root)
    root.mainloop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
