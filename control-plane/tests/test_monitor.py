"""Tests for monitor.py classification and budget logic (no network)."""
import monitor


class FakeTransport:
    """Stand-in for a transport: only count() is needed by DailyBudget."""
    def __init__(self, start=0):
        self._n = start

    def count(self):
        return self._n

    def advance(self, by):
        self._n += by


# --- is_infra / has_service_signal -----------------------------------------
def test_is_infra_header():
    assert monitor.is_infra(200, {"X-Frontier-Infra": "1"}, "{}")
    assert monitor.is_infra(200, {"x-frontier-infra": "1"}, "{}")  # case-insensitive


def test_is_infra_status_codes():
    for code in (502, 503, 504):
        assert monitor.is_infra(code, {}, "")
    assert not monitor.is_infra(500, {}, "")
    assert not monitor.is_infra(200, {}, "{}")


def test_is_infra_application_not_found_body():
    assert monitor.is_infra(404, {}, "Application not found")
    assert not monitor.is_infra(404, {}, '{"error":"E404"}')


def test_has_service_signal():
    assert monitor.has_service_signal({"X-Req": "abc"})
    assert monitor.has_service_signal({"x-req": "abc"})
    assert not monitor.has_service_signal({"content-type": "application/json"})
    assert not monitor.has_service_signal({})


# --- RoundMetrics ----------------------------------------------------------
def test_round_metrics_observe():
    rm = monitor.RoundMetrics()
    rm.observe(100.0, 200, {"x-req": "a"}, '{"ok":true}')
    rm.observe(6000.0, 200, {"x-req": "b"}, '{"ok":true}')     # slow
    rm.observe(50.0, 503, {}, "")                               # 5xx + infra
    rm.observe(50.0, 200, {"x-frontier-infra": "1"}, "x")       # infra
    rm.observe(50.0, 200, {"content-type": "x"}, "x")           # no x-req
    assert rm.req == 5
    assert rm.n_slow == 1
    assert rm.n_5xx == 1
    assert rm.n_infra == 2
    assert rm.no_xreq == 1
    p50, p99, mx = rm.pcts()
    assert mx == 6000.0


# --- DailyBudget -----------------------------------------------------------
def test_daily_budget_local_does_not_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "BUDGET_FILE", str(tmp_path / "budget.json"))
    monkeypatch.setattr(monitor, "DRIFT_LOG", str(tmp_path / "drift.log"))
    t = FakeTransport(start=10)
    b = monitor.DailyBudget(cap=100, transport=t, persist=False)
    t.advance(5)
    assert b.session_spent() == 5
    assert b.spent() == 5
    assert b.remaining() == 95
    assert b.ok()
    assert b.enforce_daily() is False       # never enforces when local
    assert "locales" in b.line() or "local" in b.line().lower()


def test_daily_budget_run_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "BUDGET_FILE", str(tmp_path / "budget.json"))
    t = FakeTransport(start=0)
    b = monitor.DailyBudget(cap=3, transport=t, persist=True)
    assert b.ok()
    t.advance(3)
    assert not b.ok()                       # spent >= cap


def test_daily_budget_warns_at_70_percent(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "BUDGET_FILE", str(tmp_path / "budget.json"))
    monkeypatch.setattr(monitor, "DRIFT_LOG", str(tmp_path / "drift.log"))
    t = FakeTransport(start=0)
    b = monitor.DailyBudget(cap=100000, transport=t, persist=True)
    b.base_today = int(monitor.DAILY_CAP * 0.70)   # exactly the warn threshold
    assert b.enforce_daily() is False              # warns, does not stop
    assert b._warned
    assert not b.stopped


def test_daily_budget_stops_at_90_percent(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "BUDGET_FILE", str(tmp_path / "budget.json"))
    monkeypatch.setattr(monitor, "DRIFT_LOG", str(tmp_path / "drift.log"))
    t = FakeTransport(start=0)
    b = monitor.DailyBudget(cap=100000, transport=t, persist=True)
    b.base_today = int(monitor.DAILY_CAP * 0.90)
    assert b.enforce_daily() is True
    assert b.stopped
    assert not b.ok()


def test_err_code_and_suggestion_helpers():
    assert monitor.err_code('{"error":"E401"}') == "E401"
    assert monitor.err_code('{"ok":true}') is None
    assert monitor.err_code("not json") is None
    assert monitor.suggestion_of('{"error":"E301","sugerencia":"ALTA"}') == "ALTA"
    assert monitor.suggestion_of('{"error":"E301"}') is None
