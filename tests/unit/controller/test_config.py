"""Unit tests for ZeroFeedConfig validators and config utilities.

Covers:
- Phase fill + validation (feedback/feedforward assignment)
- Watchdog threshold validation
- Control-phase swap in apply_config_update
- current_settings virtual fields
- queue_size calculation
"""

from __future__ import annotations

import pytest

try:
    from ZeroPythia.config.zerofeed import (
        FeedbackPhaseConfig,
        FeedforwardPhaseConfig,
        ZeroFeedConfig,
        apply_config_update,
        current_settings,
    )

    _HAS_RUAMEL = True
except ImportError:
    _HAS_RUAMEL = False

pytestmark = pytest.mark.skipif(not _HAS_RUAMEL, reason="ruamel.yaml not installed")


class TestZeroFeedConfigValidation:

    def test_default_config_fills_all_phases(self) -> None:
        cfg = ZeroFeedConfig()
        assert set(cfg.phases.keys()) == {"A", "B", "C"}

    def test_default_control_phase_is_feedback(self) -> None:
        cfg = ZeroFeedConfig(control_phase="B")
        assert isinstance(cfg.phases["B"], FeedbackPhaseConfig)

    def test_non_control_phases_are_feedforward(self) -> None:
        cfg = ZeroFeedConfig(control_phase="B")
        assert isinstance(cfg.phases["A"], FeedforwardPhaseConfig)
        assert isinstance(cfg.phases["C"], FeedforwardPhaseConfig)

    def test_custom_control_phase_a(self) -> None:
        cfg = ZeroFeedConfig(control_phase="A")
        assert isinstance(cfg.phases["A"], FeedbackPhaseConfig)
        assert isinstance(cfg.phases["B"], FeedforwardPhaseConfig)
        assert isinstance(cfg.phases["C"], FeedforwardPhaseConfig)

    def test_multiple_feedback_phases_raises(self) -> None:
        with pytest.raises(ValueError, match="Only one phase"):
            ZeroFeedConfig(
                control_phase="B",
                phases={
                    "A": FeedbackPhaseConfig(),
                    "B": FeedbackPhaseConfig(),
                    "C": FeedforwardPhaseConfig(),
                },
            )

    def test_control_phase_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="must have role='feedback'"):
            ZeroFeedConfig(
                control_phase="B",
                phases={
                    "A": FeedforwardPhaseConfig(),
                    "B": FeedforwardPhaseConfig(),
                    "C": FeedforwardPhaseConfig(),
                },
            )

    def test_watchdog_threshold_must_be_negative(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            ZeroFeedConfig(watchdog_threshold_w=5.0)

    def test_watchdog_threshold_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            ZeroFeedConfig(watchdog_threshold_w=0.0)


class TestZeroFeedConfigHelpers:

    def test_queue_size(self) -> None:
        cfg = ZeroFeedConfig(control_interval_s=3.0, sampling_interval_s=1.0)
        # ceil(3.0 / 1.0) + 1 = 4
        assert cfg.queue_size() == 4

    def test_feedforward_phases(self) -> None:
        cfg = ZeroFeedConfig(control_phase="B")
        assert cfg.feedforward_phases() == ["A", "C"]

    def test_feedback_phase(self) -> None:
        cfg = ZeroFeedConfig(control_phase="A")
        assert cfg.feedback_phase() == "A"


class TestCurrentSettings:

    def test_virtual_fields_added(self) -> None:
        cfg = ZeroFeedConfig()
        settings = current_settings(cfg)
        for ph in ("A", "B", "C"):
            osc = settings["phases"][ph]["osc"]
            assert "holder_enabled" in osc
            assert "predictor_enabled" in osc

    def test_holder_disabled_shows_false(self) -> None:
        cfg = ZeroFeedConfig()
        # Default: holder is None → holder_enabled = False
        settings = current_settings(cfg)
        assert settings["phases"]["A"]["osc"]["holder_enabled"] is False

    def test_predictor_enabled_by_default(self) -> None:
        cfg = ZeroFeedConfig()
        # Default FeedforwardPhaseConfig: predictor has default_factory → enabled
        settings = current_settings(cfg)
        assert settings["phases"]["A"]["osc"]["predictor_enabled"] is True


class TestApplyConfigUpdate:

    def test_partial_update_preserves_other_fields(self) -> None:
        cfg = ZeroFeedConfig(target_power_w=5.0, control_interval_s=3.0)
        new_cfg = apply_config_update({"target_power_w": 10.0}, cfg)
        assert new_cfg.target_power_w == 10.0
        assert new_cfg.control_interval_s == 3.0

    def test_control_phase_swap(self) -> None:
        cfg = ZeroFeedConfig(control_phase="B")
        new_cfg = apply_config_update({"control_phase": "A"}, cfg)
        assert new_cfg.control_phase == "A"
        assert isinstance(new_cfg.phases["A"], FeedbackPhaseConfig)
        assert isinstance(new_cfg.phases["B"], FeedforwardPhaseConfig)

    def test_control_phase_swap_preserves_shared_tuning(self) -> None:
        cfg = ZeroFeedConfig(control_phase="B")
        # Set custom hysteresis on phase B
        cfg.phases["B"].hysteresis_w = 25.0
        new_cfg = apply_config_update({"control_phase": "A"}, cfg)
        # Phase B should carry over hysteresis_w to new feedforward config
        assert new_cfg.phases["B"].hysteresis_w == 25.0

    def test_phase_tuning_update(self) -> None:
        cfg = ZeroFeedConfig(control_phase="B")
        new_cfg = apply_config_update(
            {"phases": {"A": {"kp": 0.8}}},
            cfg,
        )
        assert new_cfg.phases["A"].kp == 0.8
