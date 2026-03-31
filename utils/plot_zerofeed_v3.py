"""ZeroFeed V3 – Log-Analyse und Plot.
====================================

Liest eine oder mehrere CSV-Logdateien aus start_zerofeed_v3.py und
zeigt vier gestapelte Subplots mit geteilter Zeitachse:

  1. Netzleistung       – Phasen A/B/C, Total Grid, Zielband
  2. Batterie           – Tatsächlicher Output, Setpoint, Osc-Limit
  3. Regler-Interna     – Feedback, Feedforward, Differenz
  4. Oszillation        – Pro-Phase + Total Osc-Flags (Balken)

Usage:
    python src/tools/plot_zerofeed_v3.py
    python src/tools/plot_zerofeed_v3.py logs/zerofeed_v3/zerofeed_v3_2026-03-31.csv
    python src/tools/plot_zerofeed_v3.py *.csv --save --no-show
    python src/tools/plot_zerofeed_v3.py --target 5 --band 20
"""

import argparse
import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Repository-Root auf sys.path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import matplotlib
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    from matplotlib.figure import Figure
except ImportError:
    sys.exit("matplotlib nicht installiert. Bitte: pip install matplotlib")


# ── Datenstrukturen ──────────────────────────────────────────────────────────

@dataclass
class SampleRow:
    ts: datetime
    phase_a: float
    phase_b: float
    phase_c: float
    total_grid: float
    battery_output: float
    real_consumption: float
    osc_A: bool
    osc_A_limit: Optional[float]
    osc_B: bool
    osc_B_limit: Optional[float]
    osc_C: bool
    osc_C_limit: Optional[float]
    osc_total: bool
    osc_total_limit: Optional[float]


@dataclass
class ControlRow:
    ts: datetime
    feedback_w: float
    ff_w: float
    raw_setpoint: int
    osc_limit: float
    final_setpoint: int
    changed: bool


@dataclass
class LogData:
    path: Path
    samples: List[SampleRow] = field(default_factory=list)
    controls: List[ControlRow] = field(default_factory=list)

    @property
    def title(self) -> str:
        if not self.samples:
            return self.path.name
        t0 = self.samples[0].ts.astimezone()
        t1 = self.samples[-1].ts.astimezone()
        dur_min = (t1 - t0).total_seconds() / 60
        return f"{self.path.name}  ({t0.strftime('%H:%M')}–{t1.strftime('%H:%M')} Ortszeit, {dur_min:.0f} min)"


# ── CSV laden ────────────────────────────────────────────────────────────────

def _parse_float(s: str) -> Optional[float]:
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_log(path: Path) -> LogData:
    data = LogData(path=path)
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["iso_timestamp"])

            if row["event_type"] == "sample":
                data.samples.append(SampleRow(
                    ts=ts,
                    phase_a=float(row["phase_a_w"]),
                    phase_b=float(row["phase_b_w"]),
                    phase_c=float(row["phase_c_w"]),
                    total_grid=float(row["total_grid_w"]),
                    battery_output=float(row["battery_output_w"]),
                    real_consumption=float(row["real_consumption_w"]),
                    osc_A=row["osc_A_oscillating"] == "1",
                    osc_A_limit=_parse_float(row["osc_A_limit_w"]),
                    osc_B=row["osc_B_oscillating"] == "1",
                    osc_B_limit=_parse_float(row["osc_B_limit_w"]),
                    osc_C=row["osc_C_oscillating"] == "1",
                    osc_C_limit=_parse_float(row["osc_C_limit_w"]),
                    osc_total=row["osc_total_oscillating"] == "1",
                    osc_total_limit=_parse_float(row["osc_total_limit_w"]),
                ))

            elif row["event_type"] == "control":
                fb = _parse_float(row["feedback_output_w"])
                ff = _parse_float(row["ff_output_w"])
                sp = _parse_float(row["final_setpoint_w"])
                if fb is None or ff is None or sp is None:
                    continue
                data.controls.append(ControlRow(
                    ts=ts,
                    feedback_w=fb,
                    ff_w=ff,
                    raw_setpoint=int(_parse_float(row["raw_setpoint_w"]) or sp),
                    osc_limit=float(row["osc_limit_w"] or 800),
                    final_setpoint=int(sp),
                    changed=row["setpoint_changed"] == "1",
                ))

    return data


# ── Statistiken ──────────────────────────────────────────────────────────────

def print_statistics(data: LogData, target_w: float = 5.0, band_w: float = 20.0) -> None:
    s = data.samples
    c = data.controls
    if not s:
        print(f"{data.path.name}: keine Daten")
        return

    t0 = s[0].ts.astimezone()
    t1 = s[-1].ts.astimezone()
    dur_min = (t1 - t0).total_seconds() / 60

    grid = [r.total_grid for r in s]
    batt = [r.battery_output for r in s]
    real = [r.real_consumption for r in s]

    lo, hi = target_w - band_w, target_w + band_w
    in_band = sum(1 for g in grid if lo <= g <= hi)

    n = len(grid)
    mean_g = sum(grid) / n
    std_g = (sum((g - mean_g) ** 2 for g in grid) / n) ** 0.5
    mean_b = sum(batt) / n
    mean_r = sum(real) / n

    osc_samples = sum(1 for r in s if r.osc_A or r.osc_B or r.osc_C or r.osc_total)
    setpoint_changes = sum(1 for r in c if r.changed)

    print(f"\n{'='*70}")
    print(f"  {data.path.name}")
    print(f"  {t0.strftime('%Y-%m-%d %H:%M:%S')} – {t1.strftime('%H:%M:%S')} ({dur_min:.1f} min)")
    print(f"{'='*70}")
    print(f"  Samples:          {len(s)}  |  Control-Zyklen: {len(c)}")
    print(f"  Setpoint-Änder.:  {setpoint_changes} ({100*setpoint_changes/max(len(c),1):.0f}% der Zyklen)")
    print(f"  Zielband [{lo:.0f}..{hi:.0f}W]:  {in_band}/{n} = {100*in_band/n:.1f}%")
    print(f"  Total Grid:       Ø {mean_g:+.1f}W  σ {std_g:.1f}W  [{min(grid):.1f}..{max(grid):.1f}]")
    print(f"  Batterie-Output:  Ø {mean_b:.1f}W  [{min(batt):.0f}..{max(batt):.0f}]")
    print(f"  Realer Verbrauch: Ø {mean_r:.1f}W")
    print(f"  Oszillation:      {osc_samples}/{n} = {100*osc_samples/n:.1f}% der Samples")

    if c:
        fb_vals = [r.feedback_w for r in c]
        ff_vals = [r.ff_w for r in c]
        print(f"  Feedback:         Ø {sum(fb_vals)/len(fb_vals):.1f}W  [{min(fb_vals):.1f}..{max(fb_vals):.1f}]")
        print(f"  Feedforward:      Ø {sum(ff_vals)/len(ff_vals):.1f}W  [{min(ff_vals):.1f}..{max(ff_vals):.1f}]")
        ff_dominated = sum(1 for r in c if r.ff_w >= r.feedback_w)
        print(f"  FF > Feedback:    {ff_dominated}/{len(c)} = {100*ff_dominated/len(c):.0f}% der Zyklen")


# ── Plotting ─────────────────────────────────────────────────────────────────

_COLORS = {
    "phase_a": "#4477AA",
    "phase_b": "#EE7733",
    "phase_c": "#228833",
    "total":   "#000000",
    "battery": "#CC3311",
    "setpoint": "#EE7733",
    "osc_limit": "#AA3377",
    "feedback": "#4477AA",
    "ff":       "#228833",
    "target_band": "#BBDDAA",
    "feedin_band": "#FFCCCC",
}


def plot_log(
    data: LogData,
    target_w: float = 5.0,
    band_w: float = 20.0,
    save_path: Optional[Path] = None,
    show: bool = True,
) -> Figure:
    s = data.samples
    c = data.controls
    if not s:
        raise ValueError(f"Keine Sample-Daten in {data.path}")

    # Zeitachse in Ortszeit
    ts_s = [r.ts.astimezone() for r in s]
    ts_c = [r.ts.astimezone() for r in c]

    any_osc = any(r.osc_A or r.osc_B or r.osc_C or r.osc_total for r in s)

    # Grid: 4 Subplots wenn Osc vorhanden, sonst 3
    n_plots = 4 if any_osc else 3
    height_ratios = [3, 2, 1.5, 1] if any_osc else [3, 2, 1.5]

    fig, axes = plt.subplots(
        n_plots, 1,
        figsize=(14, 3.2 * n_plots),
        sharex=True,
        gridspec_kw={"height_ratios": height_ratios, "hspace": 0.08},
    )
    fig.suptitle(data.title, fontsize=11, fontweight="bold", y=0.995)

    ax_grid, ax_batt, ax_ctrl = axes[0], axes[1], axes[2]
    ax_osc = axes[3] if any_osc else None

    # ── 1. Netzleistung ───────────────────────────────────────────────
    ax = ax_grid
    lo, hi = target_w - band_w, target_w + band_w

    # Zielband (grün = Bezug OK, rot = Einspeisung)
    ax.axhspan(lo, hi, alpha=0.12, color=_COLORS["target_band"], label=f"Zielband ({lo:.0f}..{hi:.0f}W)")
    ax.axhspan(hi, ax.get_ylim()[1] if ax.get_ylim()[1] > hi else hi + 200,
               alpha=0.06, color="#FFDDAA")  # Überbezug-Warnung
    ax.axhspan(ax.get_ylim()[0] if ax.get_ylim()[0] < lo else lo - 200, lo,
               alpha=0.06, color=_COLORS["feedin_band"])  # Einspeisung

    # Phasen (dünn, halbtransparent)
    ax.plot(ts_s, [r.phase_a for r in s], color=_COLORS["phase_a"],
            lw=0.9, alpha=0.7, label="Phase A")
    ax.plot(ts_s, [r.phase_b for r in s], color=_COLORS["phase_b"],
            lw=0.9, alpha=0.7, label="Phase B (netto)")
    ax.plot(ts_s, [r.phase_c for r in s], color=_COLORS["phase_c"],
            lw=0.9, alpha=0.7, label="Phase C")

    # Total Grid (dick)
    ax.plot(ts_s, [r.total_grid for r in s], color=_COLORS["total"],
            lw=1.8, label="Total Grid", zorder=3)

    # Ziel-Linie
    ax.axhline(target_w, color="#226622", lw=0.8, ls="--", alpha=0.6, label=f"Ziel ({target_w:.0f}W)")
    ax.axhline(0, color="#999999", lw=0.5, ls=":")

    ax.set_ylabel("Netzleistung [W]")
    ax.legend(loc="upper right", fontsize=8, ncol=3)
    ax.grid(True, alpha=0.3, lw=0.5)
    _annotate_stats(ax, [r.total_grid for r in s], lo, hi)

    # ── 2. Batterie & Setpoint ────────────────────────────────────────
    ax = ax_batt

    # Tatsächlicher Output (gemessener Wert vom Shelly auf Phase B)
    ax.plot(ts_s, [r.battery_output for r in s], color=_COLORS["battery"],
            lw=1.2, alpha=0.8, label="Battery Output (Shelly)")

    if c:
        # Setpoint (gesendeter Befehl)
        ax.step(ts_c, [r.final_setpoint for r in c], color=_COLORS["setpoint"],
                lw=1.4, where="post", label="Setpoint (Befehl)", zorder=3)

        # Osc-Limit nur wenn aktiv (< max)
        osc_active = [(ts_c[i], c[i].osc_limit) for i in range(len(c)) if c[i].osc_limit < 790]
        if osc_active:
            t_osc, v_osc = zip(*osc_active, strict=False)
            ax.scatter(t_osc, v_osc, marker="v", color=_COLORS["osc_limit"],
                       s=40, zorder=5, label="Osc-Limit aktiv")

        # Setpoint-Änderungen als vertikale Marker
        changed_ts = [ts_c[i] for i in range(len(c)) if c[i].changed]
        if changed_ts:
            for t in changed_ts:
                ax.axvline(t, color="#AAAAAA", lw=0.4, alpha=0.5)

    ax.set_ylabel("Leistung [W]")
    ax.legend(loc="upper right", fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3, lw=0.5)

    # ── 3. Regler-Interna ─────────────────────────────────────────────
    ax = ax_ctrl
    if c:
        ax.plot(ts_c, [r.feedback_w for r in c], color=_COLORS["feedback"],
                lw=1.2, label="Feedback (P-Regler)")
        ax.plot(ts_c, [r.ff_w for r in c], color=_COLORS["ff"],
                lw=1.2, label="Feedforward (A+C)", ls="--")
        ax.step(ts_c, [r.final_setpoint for r in c], color="#888888",
                lw=0.8, where="post", alpha=0.6, label="Setpoint")

        # Bereich wo FF > Feedback (FF dominiert)
        for i in range(len(c) - 1):
            if c[i].ff_w > c[i].feedback_w:
                ax.axvspan(ts_c[i], ts_c[i + 1], alpha=0.08, color=_COLORS["ff"])

    ax.set_ylabel("Regler [W]")
    ax.legend(loc="upper right", fontsize=8, ncol=3)
    ax.grid(True, alpha=0.3, lw=0.5)
    ax.annotate("← FF dominiert (grün)", xy=(0.01, 0.07), xycoords="axes fraction",
                fontsize=7, color=_COLORS["ff"], alpha=0.8)

    # ── 4. Oszillations-Flags ─────────────────────────────────────────
    if ax_osc is not None:
        ax = ax_osc
        phase_defs = [
            ("osc_A",     "Phase A", _COLORS["phase_a"]),
            ("osc_B",     "Phase B", _COLORS["phase_b"]),
            ("osc_C",     "Phase C", _COLORS["phase_c"]),
            ("osc_total", "Total",   _COLORS["total"]),
        ]
        offsets = {"osc_A": 3, "osc_B": 2, "osc_C": 1, "osc_total": 0}
        for attr, label, color in phase_defs:
            off = offsets[attr]
            vals = [getattr(r, attr) for r in s]
            for i in range(len(ts_s) - 1):
                if vals[i]:
                    ax.barh(off, (ts_s[i + 1] - ts_s[i]).total_seconds() / 86400,
                            left=mdates.date2num(ts_s[i]), height=0.7,
                            color=color, alpha=0.7)
            ax.text(mdates.date2num(ts_s[-1]), off, f" {label}", va="center",
                    fontsize=7, color=color)

        ax.set_yticks([0, 1, 2, 3])
        ax.set_yticklabels(["Total", "C", "B", "A"], fontsize=7)
        ax.set_ylabel("Osc", fontsize=8)
        ax.set_ylim(-0.5, 3.7)
        ax.grid(True, alpha=0.2, axis="x")
        ax.set_title("Oszillations-Flags", fontsize=8, pad=2)

    # ── X-Achse ───────────────────────────────────────────────────────
    bottom_ax = ax_osc if ax_osc is not None else ax_ctrl
    _format_time_axis(bottom_ax, ts_s)
    plt.setp(ax_grid.get_xticklabels(), visible=False)
    plt.setp(ax_batt.get_xticklabels(), visible=False)
    if ax_osc:
        plt.setp(ax_ctrl.get_xticklabels(), visible=False)

    fig.subplots_adjust(top=0.97, hspace=0.08)

    if save_path:
        fig.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"Gespeichert: {save_path}")

    if show:
        plt.show()

    return fig


def _annotate_stats(ax, grid_vals: list, lo: float, hi: float) -> None:
    """Statistik-Box oben links im Grid-Plot."""
    n = len(grid_vals)
    if n == 0:
        return
    mean = sum(grid_vals) / n
    in_band = sum(1 for g in grid_vals if lo <= g <= hi)
    txt = (
        f"Ø {mean:+.1f} W  |  "
        f"Band {100*in_band/n:.0f}%  |  "
        f"n={n}"
    )
    ax.text(0.01, 0.97, txt, transform=ax.transAxes,
            fontsize=8, va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.2", fc="white", alpha=0.7, ec="none"))


def _format_time_axis(ax, ts_list: list) -> None:
    """Zeitachse mit sinnvoller Auto-Skalierung."""
    if not ts_list:
        return
    dur_s = (ts_list[-1] - ts_list[0]).total_seconds()
    if dur_s < 300:      # < 5 min → Sekunden
        fmt = "%H:%M:%S"
        loc = mdates.SecondLocator(interval=30)
    elif dur_s < 3600:   # < 1h → Minuten
        fmt = "%H:%M"
        loc = mdates.MinuteLocator(interval=max(1, int(dur_s / 60 / 8)))
    elif dur_s < 86400:  # < 1 Tag → Stunden
        fmt = "%H:%M"
        loc = mdates.HourLocator(interval=max(1, int(dur_s / 3600 / 8)))
    else:
        fmt = "%Y-%m-%d"
        loc = mdates.DayLocator()

    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=ts_list[0].tzinfo))
    ax.xaxis.set_major_locator(loc)
    ax.set_xlabel("Uhrzeit (Ortszeit)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _default_log_dir() -> Path:
    return _ROOT / "logs" / "zerofeed_v3"


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(
        description="ZeroFeed V3 – Log-Analyse & Plot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="CSV-Logdateien. Ohne Angabe: alle *.csv im Standard-Log-Verzeichnis.",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        metavar="PFAD",
        help=f"Log-Verzeichnis (Standard: {_default_log_dir()})",
    )
    parser.add_argument(
        "--target",
        type=float,
        default=5.0,
        metavar="W",
        help="Ziel-Netzleistung",
    )
    parser.add_argument(
        "--band",
        type=float,
        default=20.0,
        metavar="W",
        help="±Bandbreite um das Ziel",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Plot als PNG im selben Verzeichnis speichern",
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="Kein interaktives Fenster (nur speichern)",
    )
    parser.add_argument(
        "--stats-only",
        action="store_true",
        help="Nur Statistiken ausgeben, kein Plot",
    )
    args = parser.parse_args(argv)

    # Dateien ermitteln
    if args.files:
        paths = [Path(f) for f in args.files]
    else:
        log_dir = Path(args.log_dir) if args.log_dir else _default_log_dir()
        paths = sorted(log_dir.glob("zerofeed_v3_*.csv"))
        if not paths:
            sys.exit(f"Keine CSV-Dateien in {log_dir}")

    for path in paths:
        if not path.exists():
            print(f"Nicht gefunden: {path}", file=sys.stderr)
            continue

        data = load_log(path)
        print_statistics(data, target_w=args.target, band_w=args.band)

        if args.stats_only:
            continue

        save_path = path.with_suffix(".png") if args.save else None
        plot_log(
            data,
            target_w=args.target,
            band_w=args.band,
            save_path=save_path,
            show=not args.no_show,
        )


if __name__ == "__main__":
    main()
