# Meeting transcription bot

A Recall.ai bot for **Google Meet**, **Zoom**, and **Microsoft Teams**. Paste a link to join, get a transcript, and auto-generate polished FAQs.

## Setup

1. Create an API key in the [Recall.ai dashboard](https://www.recall.ai) (Developers → API keys). Note the **region** (`us-east-1`, `us-west-2`, `eu-central-1`, or `ap-northeast-1`).
2. Create an [OpenAI API key](https://platform.openai.com/api-keys) for FAQ extraction.
3. Copy the env file and fill in keys:

```bash
cp .env.example .env
```

4. Python 3.10+ — see `requirements.txt` (stdlib only today).

## Run locally

```bash
python3 server.py
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

## Docker

```bash
cp .env.example .env   # set RECALL_API_KEY, RECALL_REGION, OPENAI_API_KEY
docker compose up --build
```

App: [http://127.0.0.1:8765](http://127.0.0.1:8765).

Or without Compose:

```bash
docker build -t meetwithrecall .
docker run --rm -p 8765:8765 --env-file .env meetwithrecall
```

## Deploy notes

- Bind with `HOST=0.0.0.0` (already set in the image / `.env.example`).
- Pass secrets as env vars: `RECALL_API_KEY`, `RECALL_REGION`, `OPENAI_API_KEY`.
- Expose container port `8765` (or set `PORT`).
- Persist `/app/transcripts` if you want saved transcripts across restarts.

## Run from the terminal

```bash
python3 meeting_bot.py "https://meet.google.com/abc-defg-hij"
```

Transcripts land in `transcripts/`.
