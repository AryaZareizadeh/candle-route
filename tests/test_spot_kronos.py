from datetime import datetime, timedelta, timezone

import pytest

import spot_kronos
from spot_paper import SpotPaperService
from storage import Store


BOUNDARY = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def candles(asset="BTC", source="binance", boundary=BOUNDARY):
    return [
        {"asset": asset, "exchange": source, "quote": "USDT", "interval": "1h",
         "open_time": (boundary - timedelta(hours=offset)).isoformat(),
         "available_at": (boundary - timedelta(hours=offset - 1)).isoformat(),
         "open": 100, "high": 101, "low": 99, "close": 100, "volume_base": 1000}
        for offset in range(spot_kronos.LOOKBACK, 0, -1)
    ]


@pytest.mark.parametrize("change", ["gap", "unfinished", "mixed_exchange", "quote", "off_grid"])
def test_kronos_context_rejects_ineligible_candles(change):
    rows = candles()
    boundary = BOUNDARY.timestamp()
    if change == "gap":
        rows.pop(12)
        rows.insert(0, rows[0].copy())
    elif change == "unfinished":
        rows = candles(boundary=BOUNDARY + timedelta(hours=1))
    elif change == "mixed_exchange":
        rows[38]["exchange"] = "kucoin"
    elif change == "quote":
        rows[-1]["quote"] = "IRT"
    else:
        rows[-1]["available_at"] = (BOUNDARY - timedelta(seconds=7)).isoformat()
    with pytest.raises(ValueError):
        spot_kronos.candle_context("BTC", rows, boundary, "binance")


class PublicSignals:
    def fetch(self, asset, interval, start, end, **kwargs):
        assert interval == "1h"
        assert start == end - spot_kronos.LOOKBACK * 3600
        source = "kucoin" if asset == "HYPE" else "binance"
        return {"candles": candles(asset=asset, source=source)}


class PaperVenue:
    def books(self):
        return {asset: {"lastUpdate": (BOUNDARY.timestamp() + 55) * 1000,
                        "asks": [["100.1", "100"]], "bids": [["100", "100"]]}
                for asset in ("BTC", "HYPE")}

    def rules(self):
        return {"min_notional": "0.5", "quantity_steps": {"BTC": "0.000001", "HYPE": "0.000001"}}


@pytest.fixture
def paper(tmp_path, monkeypatch):
    monkeypatch.setattr("spot_paper.time.time", lambda: BOUNDARY.timestamp() + 60)
    service = SpotPaperService(Store(tmp_path / "owner" / "trader.sqlite3"), signals=PublicSignals(),
                               venue=PaperVenue(), forecast_workspace=tmp_path / "owner" / "forecasting")
    monkeypatch.setattr(service, "kronos_status", lambda: {"ready": True, "method": spot_kronos.MODEL, "reason": None})
    return service


def test_kronos_forecast_is_used_by_paper_allocator_without_research_model(paper, monkeypatch):
    calls = []

    def predict(workspace, contexts, closed_at):
        calls.append((workspace, list(contexts), closed_at))
        assert contexts["HYPE"][-1]["timestamp"] == (BOUNDARY - timedelta(hours=1)).isoformat()
        return {"HYPE": {"return": 0.05, "downside": 0.01, "horizon_hours": 4}}

    monkeypatch.setattr(spot_kronos, "predict", predict)
    assert not paper.model_path.exists()
    assert paper.start(["BTC", "HYPE"], 200, signal_model="kronos-small")["signal_model"] == "kronos-small"
    result = paper.tick()
    assert calls == [(paper.forecast_workspace, ["BTC", "HYPE"], BOUNDARY.timestamp())]
    assert result["ok"] is True
    state = result["state"]
    assert state["holdings"].keys() == {"HYPE"}
    assert state["fills"][0]["estimated"] is True
    assert state["decisions"][0]["signal_sources"] == {"HYPE": "kucoin"}
    assert state["decisions"][0]["model_version"] == "kronos-small:original:4h"
    assert paper.tick()["reason"] == "Already processed this closed hour"
    paper.stop()
    assert paper.start(budget=200)["signal_model"] == "kronos-small"
    with pytest.raises(ValueError, match="Reset"):
        paper.start(budget=200, signal_model="research")


def test_missing_kronos_weights_cannot_start_or_fall_back_to_research(paper, monkeypatch):
    monkeypatch.setattr(paper, "kronos_status", lambda: {"ready": False, "reason": "No local weights"})
    with pytest.raises(ValueError, match="No local weights"):
        paper.start(["BTC"], 200, signal_model="kronos-small")
    assert paper.state() is None
    assert paper.start(["BTC"], 200)["signal_model"] == "research"


def test_no_kronos_forecast_does_not_liquidate_owned_assets(paper, monkeypatch):
    state = paper.start(["BTC", "HYPE"], 200, signal_model="kronos-small")
    state["holdings"]["BTC"] = "0.5"
    paper._save_state(state)
    monkeypatch.setattr(spot_kronos, "predict", lambda *args: {"HYPE": {"return": 0.05, "downside": 0.01}})
    assert paper.tick() == {"ok": False, "reason": "Kronos has no valid forecast or is missing a forecast for a held asset"}
    assert paper.state()["holdings"] == {"BTC": "0.5"}
    assert not paper.state()["orders"]


def test_offline_paper_service_blocks_network_on_manual_tick_and_research(tmp_path, monkeypatch):
    monkeypatch.setattr("spot_paper.time.time", lambda: BOUNDARY.timestamp() + 60)
    service = SpotPaperService(Store(tmp_path / "state.db"), offline=True)
    monkeypatch.setattr(service, "kronos_status", lambda: {"ready": True})
    service.start(["BTC"], 200, signal_model="kronos-small")
    with pytest.raises(ValueError, match="while CandleRoute is offline"):
        service.tick()
    with pytest.raises(ValueError, match="while CandleRoute is offline"):
        service.research(["BTC"], days=90)


def test_concurrent_inference_abstains_without_starting_another_worker(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Paper inference already has a running worker")

    monkeypatch.setattr(spot_kronos.subprocess, "run", unexpected)
    with spot_kronos._INFERENCE_SLOT, pytest.raises(ValueError, match="already running"):
        spot_kronos.predict(tmp_path, {}, BOUNDARY.timestamp())
