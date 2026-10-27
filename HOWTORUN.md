# How to Run

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

## 2. Set up your .env file

Copy the example and fill in 3 values:

```bash
cp .env.example .env
```

Open `.env` and set:

```
MODE=manual
OPENAI_API_KEY=sk-...          # from platform.openai.com/api-keys
ACCESS_PASSWORD=any-long-password-you-pick
```

Leave everything else as-is.

## 3. Start the server

```bash
uvicorn main:app --reload
```

## 4. Open the app

Go to http://localhost:8000 in your browser.

Enter your `ACCESS_PASSWORD`, fill in the run details, and click **Get Titles**.

---

## Stopping the server

Press `Ctrl + C` in the terminal.

---

## Running with Docker instead

```bash
docker-compose up
```

Then open http://localhost:8000.

To stop:

```bash
docker-compose down
```
