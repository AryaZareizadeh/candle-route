"""Use CandleRoute's original Kronos-small weights for paper spot decisions."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from threading import Lock
from types import SimpleNamespace


LOOKBACK = 120
HORIZON = 4
HOUR = 3600
MODEL = "kronos-small"
SUPPORTED_SOURCES = {"binance", "kucoin", "bitfinex"}
MISSING_WEIGHTS = "Set up the original Kronos-small weights in Forecasting"
_INFERENCE_SLOT = Lock()


def model_status(workspace):
    if workspace is None or any(find_spec(module) is None for module in
                                ("crypto_forecast", "torch", "huggingface_hub", "einops", "safetensors", "tqdm")):
        return {"ready": False, "method": MODEL, "reason": "Install CandleRoute's optional forecasting dependencies"}
    reason = MISSING_WEIGHTS
    try:
        if workspace.resolve() == workspace and all(_has_weights(workspace, part) for part in ("model", "tokenizer")):
            reason = None
    except (OSError, RuntimeError):
        pass  # Unreadable or unresolvable model files do not make other paper modes unavailable.
    return {"ready": reason is None, "method": MODEL, "reason": reason}


def _has_weights(workspace, part):
    directory = workspace / "models" / MODEL / part
    config = (directory / "config.json").resolve()
    weights = [(directory / name).resolve() for name in ("model.safetensors", "pytorch_model.bin")]
    return (config.is_relative_to(workspace) and config.is_file()
            and any(path.is_relative_to(workspace) and path.is_file() for path in weights))


def candle_context(asset, candles, closed_at, source):
    """Return one contiguous public USDT spot series that ends at the decision boundary."""
    if source not in SUPPORTED_SOURCES or len(candles) < LOOKBACK:
        raise ValueError("Incomplete Kronos candle context")
    candles = sorted(candles, key=lambda bar: datetime.fromisoformat(bar["open_time"]).timestamp())[-LOOKBACK:]
    context = []
    for index, bar in enumerate(candles):
        opened = datetime.fromisoformat(bar["open_time"])
        available = datetime.fromisoformat(bar["available_at"])
        opening = closed_at - (LOOKBACK - index) * HOUR
        if (bar["asset"], bar["exchange"], bar["quote"], bar["interval"]) != (asset, source, "USDT", "1h"):
            raise ValueError("Mixed source or instrument in Kronos candle context")
        if (opened.tzinfo is None or available.tzinfo is None or opened.timestamp() != opening
                or available.timestamp() != opening + HOUR):
            raise ValueError("Kronos requires contiguous completed candles through the decision hour")
        prices = {field: float(bar[field]) for field in ("open", "high", "low", "close")}
        volume = float(bar["volume_base"])
        if (not all(math.isfinite(value) and value > 0 for value in prices.values())
                or not math.isfinite(volume) or volume < 0
                or prices["high"] < max(prices["open"], prices["close"])
                or prices["low"] > min(prices["open"], prices["close"])):
            raise ValueError("Invalid OHLCV in Kronos candle context")
        context.append({"timestamp": opened.isoformat(), **prices, "volume": volume})
    return context


def predict(workspace, contexts, closed_at):
    """Run inference separately from the trading process and its model trainer."""
    payload = json.dumps({"context": contexts, "closed_at": closed_at}, allow_nan=False)
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("NOBITEX_", "TRADER_", "SMTP_", "DASHBOARD_"))}
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    if not _INFERENCE_SLOT.acquire(blocking=False):
        raise ValueError("Kronos paper inference is already running; the next tick can retry")
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve())], cwd=workspace,
                                input=payload, text=True, capture_output=True, check=True, env=env,
                                timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        raise ValueError("Kronos paper inference timed out") from None
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        raise ValueError("Kronos paper inference failed; check the local forecasting dependencies and weights") from None
    finally:
        _INFERENCE_SLOT.release()


def _predict_locally(workspace, contexts, closed_at):
    import pandas as pd
    import torch

    from crypto_forecast._vendor.kronos import KronosPredictor
    from crypto_forecast.chart_forecast import original_paths
    from crypto_forecast.kronos_backend import load_pretrained

    torch.set_num_threads(min(4, os.cpu_count() or 1))
    torch.manual_seed(7)
    model_path, tokenizer_path = original_paths(workspace, MODEL)
    model, tokenizer = load_pretrained(SimpleNamespace(kronos_model_path=str(model_path),
                                                       kronos_tokenizer_path=str(tokenizer_path)), "cpu")
    model.eval().requires_grad_(False)
    tokenizer.eval().requires_grad_(False)
    predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
    frames = [pd.DataFrame(candles).assign(amount=0.0) for candles in contexts.values()]
    times = [pd.to_datetime(frame["timestamp"], utc=True) for frame in frames]
    origin = datetime.fromtimestamp(closed_at, timezone.utc)
    future = pd.Series(pd.date_range(origin, periods=HORIZON, freq="h"))
    with torch.inference_mode():
        output = predictor.predict_batch(frames, times, [future] * len(frames), HORIZON, verbose=False)

    forecasts = {}
    for (asset, context), prediction in zip(contexts.items(), output, strict=True):
        prices = prediction[["open", "high", "low", "close"]].to_numpy().ravel()
        if len(prediction) != HORIZON or not all(math.isfinite(price) and price > 0 for price in prices):
            continue
        reference = context[-1]["close"]
        expected_return = float(prediction["close"].iloc[-1] / reference - 1)
        downside = max(0.001, 1 - min(prices) / reference)
        if math.isfinite(expected_return) and math.isfinite(downside):
            forecasts[asset] = {"return": expected_return, "downside": float(downside), "horizon_hours": HORIZON}
    return forecasts


if __name__ == "__main__":
    # The parent passes only validated public candle rows and the UTC close.
    request = json.load(sys.stdin)
    with redirect_stdout(sys.stderr):
        forecasts = _predict_locally(Path.cwd(), request["context"], request["closed_at"])
    print(json.dumps(forecasts, allow_nan=False))
