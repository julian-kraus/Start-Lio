"""plan.clock: real time by default, scenario time from the environment or the plan.clock row."""

from datetime import datetime, timedelta, timezone

import pytest

from plan import clock

T0 = datetime(2030, 1, 10, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(clock.ENV_VAR, raising=False)


def test_real_time_by_default():
    before = datetime.now(timezone.utc)
    got = clock.now()
    assert got.tzinfo is not None and before <= got <= datetime.now(timezone.utc)
    assert not clock.is_scenario()


def test_env_scenario_time(monkeypatch):
    monkeypatch.setenv(clock.ENV_VAR, "2030-01-10T09:00:00+01:00")
    assert clock.now() == T0
    assert clock.is_scenario()


def test_env_time_needs_offset(monkeypatch):
    monkeypatch.setenv(clock.ENV_VAR, "2030-01-10T09:00:00")
    with pytest.raises(clock.ClockError):
        clock.now()
    with pytest.raises(clock.ClockError):
        clock.parse("tomorrow")


def test_scenario_time_runs_at_rate():
    set_at = T0 + timedelta(days=1)
    assert clock.scenario_time(T0, 0, set_at, set_at + timedelta(hours=3)) == T0
    assert clock.scenario_time(T0, 60, set_at, set_at + timedelta(minutes=1)) == T0 + timedelta(hours=1)
    assert clock.scenario_time(T0, 1, set_at, set_at - timedelta(minutes=5)) == T0


def test_parse_accepts_z():
    assert clock.parse("2030-01-10T08:00:00Z") == T0
