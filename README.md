# run-titler

Automatically renames your Strava runs — or lets you generate titles manually — using OpenAI.

Two independent modes share the same codebase:

| Mode | What it does | What you need |
|---|---|---|
| **manual** | Web form → 3 title suggestions | `OPENAI_API_KEY` + `ACCESS_PASSWORD` |
| **webhook** | Strava sends a run → auto-renamed | Everything in manual + Strava API access |

Start with **manual** mode. You get useful titles immediately with no Strava setup.

---

> **Strava API note:** The webhook mode automates personal data in a way that technically bends Strava's API agreement. It's a private project with low usage, but build it expecting it might stop working some day.
>
> **Strava base URL change:** Strava is moving to `api-v3.strava.com` on **January 4, 2027**. Update `STRAVA_BASE_URL` in your env at that time.

---

## Quick start — manual mode

This gets you a live title generator in about 3 minutes.

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Open `.env` and set:
```
MODE=manual
OPENAI_API_KEY=sk-...
ACCESS_PASSWORD=your-long-random-password
```

Leave all `STRAVA_*` values as placeholders — they're ignored in manual mode.

### 3. Start

```bash
uvicorn main:app --reload
```

Open <http://localhost:8000>. Enter your password, fill in the form, get 3 titles.

---

## Manual mode — what the form does

| Field | Description |
|---|---|
| Distance (km) | How far you ran |
| Time (min) | Moving time |
| Run type | Easy / Tempo / Long / Intervals / Recovery / Race |
| Time of day | Pre-filled to the current time |
| Tone | Fun / Motivational / Minimal / Funny |
| Notes | Optional — anything you want reflected in the title |

Pace is calculated in-app and shown live as you type. The AI never invents facts you didn't provide.

---

## Webhook mode — full Strava automation

### How it works

1. You finish a run; Strava processes it
2. Strava POSTs a webhook event to your server
3. The server waits 30–60 s (Strava needs time to finalize stats), then fetches the activity
4. A compact summary (no GPS, no HR data, no personal name) is sent to OpenAI
5. The generated title is written back to Strava with `PUT /activities/{id}`

Only activities whose current title is one of Strava's defaults (`"Morning Run"`, `"Afternoon Run"`, etc.) are renamed. Titles you set yourself are never touched.

### Prerequisites

| Thing | Where |
|---|---|
| Strava API app | [strava.com/settings/api](https://www.strava.com/settings/api) |
| Public HTTPS URL | ngrok (local), Render or Railway (production) |

### Setup

#### 1. Create a Strava API app

Go to <https://www.strava.com/settings/api> and note your **Client ID** and **Client Secret**.

#### 2. Set env vars

In `.env`:
```
MODE=webhook
STRAVA_CLIENT_ID=...
STRAVA_CLIENT_SECRET=...
STRAVA_VERIFY_TOKEN=any-random-secret
```

#### 3. Get a public HTTPS URL

```bash
# Install ngrok from https://ngrok.com/download
ngrok http 8000
# copy the https://... URL
```

#### 4. Run the OAuth + webhook setup script

Your server must be running and reachable before this step.

```bash
python setup_oauth.py --callback-url https://YOUR_NGROK_URL/webhook
```

The script opens a browser, walks through Strava OAuth, and prints the values to add to `.env`:
```
STRAVA_REFRESH_TOKEN=...
ATHLETE_ID=...
```

#### 5. Test with DRY_RUN=true

Set `DRY_RUN=true` in `.env`, restart the server, then manually create a Strava activity with a default title like `"Morning Run"`. Watch the logs — you should see the webhook, the wait, and a generated title (logged but not written to Strava yet).

Once happy, set `DRY_RUN=false`.

---

## Deploying to Render

1. Confirm `.env` is in `.gitignore` before pushing:
   ```bash
   git status   # .env must NOT appear
   ```
2. Push to GitHub
3. Create a new **Web Service** on [Render](https://render.com)
   - Runtime: **Docker**
   - Health Check Path: `/health`
4. Add all env vars from `.env` in Render's **Environment** tab
5. After deploy, re-register the webhook:
   ```bash
   python setup_oauth.py \
     --callback-url https://YOUR-APP.onrender.com/webhook \
     --skip-oauth
   ```

> Render's free tier sleeps after 15 min of inactivity. A paid plan or Railway avoids cold-start delays on the first webhook.

---

## All environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `MODE` | | `webhook` | `manual` or `webhook` |
| `OPENAI_API_KEY` | ✓ | — | OpenAI API key |
| `OPENAI_MODEL` | | `gpt-4o-mini` | Model name |
| `ACCESS_PASSWORD` | manual only | — | Password for the web UI |
| `STRAVA_CLIENT_ID` | webhook only | — | Strava app client ID |
| `STRAVA_CLIENT_SECRET` | webhook only | — | Strava app client secret |
| `STRAVA_REFRESH_TOKEN` | webhook only | — | Bootstrap refresh token |
| `STRAVA_VERIFY_TOKEN` | webhook only | — | Shared secret for webhook validation |
| `ATHLETE_ID` | webhook only | — | Your Strava athlete ID |
| `STRAVA_BASE_URL` | | `https://www.strava.com/api/v3` | Update after Jan 4 2027 |
| `DRY_RUN` | | `false` | Log title only, don't write to Strava |
| `DB_PATH` | | `run_titler.db` | SQLite path (webhook mode only) |

---

## Cost estimate

`gpt-4o-mini` at 3 requests/week (manual) or 3 runs/week (webhook):

- Manual: ~100 tokens input + 30 output per request
- Webhook: ~150 tokens input + 15 output per activity
- At $0.15/M input, $0.60/M output → **< $0.01/month**

---

## Troubleshooting

**Manual mode won't start**
- Check `ACCESS_PASSWORD` is set in `.env`

**Password prompt keeps reappearing**
- The session token is stored in `sessionStorage` — it clears when you close the browser tab

**Webhook not firing**
- Check the subscription: `python setup_oauth.py --list-only --callback-url x`
- Make sure the server is reachable at the registered URL

**Title not updating (webhook mode)**
- Most likely the activity already had a custom title — check the logs

**Lost the SQLite database**
- Delete `run_titler.db` and restart. Re-run `setup_oauth.py` to get a fresh refresh token.
