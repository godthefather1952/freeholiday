# freeholiday

Mobile-first quarter-hour crypto forecasting dashboard with four independent forecasting engines:

- Sixcents — BTC-USD
- Bandjovi — ETH-USD
- Solsister — DOGE-USD
- NEAR Protocol — NEAR-USD

## Unified dashboard

The root `index.html` presents all four engines as matching forecast cards on one page and includes a quarter-hour Results Ledger aligned by forecast window.

Each engine preserves its own:

- Coinbase Exchange market-data requests
- historical candle cache
- calibration/model state
- signal scoring
- expected-move model
- active locked forecast
- local prediction history
- WIN / LOSS / PUSH settlements

## NEAR engine

`near.html` uses the Coinbase Exchange `NEAR-USD` product with 15-minute candles and an independent 90-day historical calibration.

Storage namespaces:

- `near_history_v1`
- `near_predictions_v1`
- `near_model_v1`
- `near_calibration_v1`

The NEAR model uses the same seven-component technical score structure as the other score-based engines, fits its own logistic Higher/Lower probability from NEAR history, builds score-specific median expected moves, and evaluates the frozen model on the newest 30% of historical samples.
