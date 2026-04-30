"""Dashboard UI translations.

Add a new language by extending TRANSLATIONS with the full set of
string keys present in the ``en`` entry (the reference / default).

Usage in server.py::

    from .i18n import TRANSLATIONS, build_js_t
    t = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    js_t_json = build_js_t(t)
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# Translation tables
# ---------------------------------------------------------------------------
# Keys prefixed with "t_"   → Python-side HTML placeholders
# Keys prefixed with "js_"  → injected into the dashboard JS as the T object
# ---------------------------------------------------------------------------

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        # ── HTML placeholders ──────────────────────────────────────────────
        "t_title": "Zendure Dashboard",
        "t_pv": "PV",
        "t_soc": "SoC",
        "t_grid": "Grid",
        "t_load": "Load",
        "t_bat": "Bat",
        "t_overview_card": "Overview &amp; Controller",
        "t_controller": "Controller",
        "t_setpoint": "Battery setpoint",
        "t_raw_target": "Target (raw)",
        "t_ff_total": "FF total",
        "t_feedback": "Feedback",
        "t_watchdog_resets": "Watchdog resets",
        "t_zfi_regulation": "ZFI Regulation",
        "t_deviation": "Deviation (Grid \u2212 Target)",
        "t_grid_row": "Grid",
        "t_demand": "Demand",
        "t_osc_limit": "OSC limit",
        "t_load_est": "Load (est.)",
        "t_setpoint_fb": "Setpoint (FB)",
        "t_mode_card": "Operating Mode",
        "t_btn_idle": "Idle",
        "t_btn_zf": "Zero-Feed",
        "t_btn_charge": "AC Charge \u25b8",
        "t_btn_auto": "Auto \u25b8",
        "t_charge_power_label": "Charge power [W]",
        "t_charge_start": "Start charging",
        "t_broker_label": "MQTT Broker URL",
        "t_device_id_label": "Device ID",
        "t_auto_activate": "Activate Auto",
        "t_auto_deactivate": "Deactivate",
        "t_settings_card": "Controller Settings",
        "t_apply": "Apply",
        "t_auto_card": "GridPythia Plan",
        "t_plan_published": "Plan published",
        "t_effective_label": "Effective",
        "t_version_prefix": "v",
        "t_footer_project": "Zero Feed Controller",
        "t_github": "GitHub",
        "t_saved": "Saved",
        # ── JS T object keys ───────────────────────────────────────────────
        "js_mode_idle": "Idle",
        "js_mode_charge": "AC Charge",
        "js_mode_zf": "Zero-Feed",
        "js_mode_auto": "Auto",
        "js_paused_soc": "SoC min",
        "js_paused_full": "Bat full",
        "js_paused_soc_full": "Paused (SoC min)",
        "js_paused_full_full": "Paused (Bat full)",
        "js_paused": "Paused",
        "js_active": "Active",
        "js_inactive": "Inactive",
        "js_saved": "Saved",
        "js_broker_required": "Broker and Device ID required",
        "js_auto_activated": "Auto mode activated",
        "js_auto_deactivated": "Auto mode deactivated",
        "js_connected_plan": "Connected \u2713 Plan",
        "js_connected_no_plan": "Connected (no plan)",
        "js_disconnected": "Disconnected",
        "js_no_plan": "No plan",
        "js_ends_next_day": "ends next day",
    },
    "de": {
        # ── HTML placeholders ──────────────────────────────────────────────
        "t_title": "Zendure Dashboard",
        "t_pv": "PV",
        "t_soc": "SoC",
        "t_grid": "Netz",
        "t_load": "Haus",
        "t_bat": "Bat",
        "t_overview_card": "Gesamt &amp; Regler",
        "t_controller": "Regler",
        "t_setpoint": "Setpoint Batterie",
        "t_raw_target": "Ziel (roh)",
        "t_ff_total": "FF gesamt",
        "t_feedback": "Feedback",
        "t_watchdog_resets": "Watchdog Resets",
        "t_zfi_regulation": "ZFI Regelung",
        "t_deviation": "Abweichung (Netz \u2212 Ziel)",
        "t_grid_row": "Netz",
        "t_demand": "Anforderung",
        "t_osc_limit": "OSZ-Limit",
        "t_load_est": "Verbrauch (est.)",
        "t_setpoint_fb": "Setpoint (FB)",
        "t_mode_card": "Betriebsmodus",
        "t_btn_idle": "Idle",
        "t_btn_zf": "Zero-Feed",
        "t_btn_charge": "AC Laden \u25b8",
        "t_btn_auto": "Auto \u25b8",
        "t_charge_power_label": "Ladeleistung [W]",
        "t_charge_start": "Laden starten",
        "t_broker_label": "MQTT Broker URL",
        "t_device_id_label": "Ger\u00e4t-ID",
        "t_auto_activate": "Auto aktivieren",
        "t_auto_deactivate": "Deaktivieren",
        "t_settings_card": "Regler-Einstellungen",
        "t_apply": "Anwenden",
        "t_auto_card": "GridPythia Plan",
        "t_plan_published": "Plan erstellt",
        "t_effective_label": "Aktuell effektiv",
        "t_version_prefix": "v",
        "t_footer_project": "Zero Feed Controller",
        "t_github": "GitHub",
        "t_saved": "Gespeichert",
        # ── JS T object keys ───────────────────────────────────────────────
        "js_mode_idle": "Idle",
        "js_mode_charge": "AC Laden",
        "js_mode_zf": "Zero-Feed",
        "js_mode_auto": "Auto",
        "js_paused_soc": "SoC-Min",
        "js_paused_full": "Batt. voll",
        "js_paused_soc_full": "Pausiert (SoC-Min)",
        "js_paused_full_full": "Pausiert (Batt. voll)",
        "js_paused": "Pausiert",
        "js_active": "Aktiv",
        "js_inactive": "Inaktiv",
        "js_saved": "Gespeichert",
        "js_broker_required": "Broker und Device ID erforderlich",
        "js_auto_activated": "Auto-Modus aktiviert",
        "js_auto_deactivated": "Auto-Modus deaktiviert",
        "js_connected_plan": "Verbunden \u2713 Plan",
        "js_connected_no_plan": "Verbunden (kein Plan)",
        "js_disconnected": "Getrennt",
        "js_no_plan": "Kein Plan",
        "js_ends_next_day": "endet n\u00e4chsten Tag",
    },
}


def build_js_t(t: dict[str, str]) -> str:
    """Build the JSON string for the dashboard JS ``T`` object.

    Only keys prefixed with ``js_`` are included; the prefix is stripped.
    """
    js_keys = {k[3:]: v for k, v in t.items() if k.startswith("js_")}
    return json.dumps(js_keys, ensure_ascii=False)
