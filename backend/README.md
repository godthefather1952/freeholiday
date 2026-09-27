# FreeHoliday Kalshi backend

FastAPI service for connecting a user's Kalshi API key + PEM, keeping the PEM encrypted server-side, discovering the matching 15-minute Kalshi market, showing the Kalshi target, recording Confirm/Skip decisions, and submitting V2 orders after risk checks.

## Security model

- The browser uploads the PEM only to this backend over HTTPS.
- Credentials are encrypted at rest with `FREEHOLIDAY_FERNET_KEY`.
- The PEM is never written into frontend JavaScript, GitHub Pages storage, localStorage, or sessionStorage.
- The browser only receives a random short-lived connection token; the current frontend keeps it in `sessionStorage`.
- Every order requires a per-asset, per-window confirmation.
- Auto Trade means: after the user presses **Confirm** for that coin/window, the backend may immediately place the order if all risk controls pass. There is no confidence-threshold gate.
- Duplicate coin/window orders are rejected by both application logic and a database uniqueness constraint.

## Local setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export FREEHOLIDAY_FERNET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
uvicorn app:app --reload --port 8000
```

The API will be at `http://127.0.0.1:8000`.

## Kalshi flow

1. `POST /api/kalshi/connect` with API key ID + PEM.
2. Backend proves the key pair by making a signed balance request.
3. `POST /api/kalshi/opportunities` with current FreeHoliday forecasts.
4. Backend discovers the open 15-minute market and returns its `floor_strike` target plus current YES/NO executable quote.
5. User presses Confirm or Skip.
6. With Auto Trade OFF, a confirmed opportunity can be submitted with `POST /api/kalshi/orders`.
7. With Auto Trade ON, pressing Confirm immediately runs the risk checks and attempts the order.

## Important deployment note

GitHub Pages cannot execute Python. Deploy this `backend/` directory to a real Python host (Render, Railway, Fly.io, a VPS, etc.) and configure the frontend's API URL. Use HTTPS and a persistent database/disk in production.
