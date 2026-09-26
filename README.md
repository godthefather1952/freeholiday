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


## Tight Mode

Each asset now uses a chronological 60/20/20 workflow:

- Oldest 60%: train the probability and expected-move model.
- Middle 20%: choose an asset-specific minimum confidence cutoff.
- Newest 20%: untouched final verification.

The cutoff search requires at least 20% of the validation segment, with a minimum of 100 forecasts, and favors the strongest lower-confidence-bound accuracy rather than tiny high-accuracy samples.

Every quarter-hour prediction is still stored and settled for the All Results Ledger. Forecasts below the learned cutoff display PASS and do not count toward the Tight Results Ledger. Existing pre-Tight-Mode records remain part of the All ledger and are not retroactively classified as qualified.
