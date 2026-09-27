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


## Kalshi trading

FreeHoliday now includes an optional Kalshi trading layer. The forecast engines remain unchanged.

### User flow

1. Deploy the Python backend in `backend/`.
2. Enter that backend URL in the FreeHoliday Kalshi panel.
3. Enter a Kalshi API Key ID and upload the matching private-key PEM.
4. The backend verifies the pair with a signed Kalshi balance request and encrypts the credentials server-side.
5. For each current 15-minute FreeHoliday forecast, the backend finds the matching Kalshi market and returns its published target/threshold and current YES/NO quote.
6. The user reviews the predicted close versus the Kalshi target and explicitly chooses **Confirm** or **Skip** for that coin and that 15-minute window.
7. With Auto Trade OFF, a confirmed setup gets a manual Place Trade button.
8. With Auto Trade ON, pressing Confirm immediately attempts the order after all risk controls pass.

Prediction confidence is displayed for context but is **not** an auto-trade qualification threshold.

### Guardrails

- Maximum dollars per trade
- Maximum daily exposure
- Daily loss stop for FreeHoliday-originated settled trades
- Maximum open positions
- Maximum contract purchase price
- Per-asset enable/disable
- One FreeHoliday order per asset per 15-minute window
- Pause/Resume trading
- Disconnect Kalshi

### Security

The PEM is never stored in GitHub Pages, localStorage, sessionStorage, or frontend JavaScript. The backend encrypts the API key ID and PEM at rest using `FREEHOLIDAY_FERNET_KEY`. The browser receives only a random connection token, kept in sessionStorage.

### Deployment

GitHub Pages serves the frontend but cannot run Python. A `render.yaml` blueprint and `backend/Dockerfile` are included for deploying the FastAPI backend. Before connecting a live Kalshi account, configure a strong `FREEHOLIDAY_FERNET_KEY` and use HTTPS.

See `backend/README.md` for local and deployment setup.
