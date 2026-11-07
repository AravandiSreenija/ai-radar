# AI Radar

AI moves too fast to follow by hand. AI Radar collects the last 24 hours of AI news
from public sources, lets Claude pick the 10 that matter, and publishes them as a
static website and a daily email, every link pointing to the original source.

## How it works

1. **Collect** - RSS/Atom feeds (OpenAI, Google AI, DeepMind, Hugging Face, NVIDIA,
   TechCrunch, The Verge), GitHub releases (ollama, llama.cpp, transformers, langchain,
   vllm) and the Hacker News API.
2. **Filter and dedupe** - only items from the last 24h (widened once on quiet days),
   duplicates removed by URL and normalised title.
3. **Curate** - Claude receives numbered candidates and returns ids, a category, a short
   summary and a "why it matters" line for a reader profile you set.
   - **Personas:** the same Claude call also scores each story 1-5 for a developer, a product manager and an executive and writes a persona-specific "why it matters". The page lets you switch views.
4. **Publish** - `site/index.html` (GitHub Pages) and an HTML email.
5. **Schedule** - a GitHub Actions cron runs it every morning.

### Safeguards
- Claude returns **ids only**; titles and URLs come from the fetched feeds, so links cannot be invented.
- Time-window filter and dedupe before the model sees anything.
- One failing source never stops a run.
- Pages carry a note that summaries are AI-generated.

## Features
- Top-10 view plus **Developer / Product / Leadership** persona views
- Category filters, search, relevance badges, pinned top story
- 30-day archive (the workflow commits `archive/<date>.json`) with a day picker
- Source health in the footer

## Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...            # optional: without it a simple fallback ranking is used
python radar.py --no-email              # writes site/index.html
```

Email (optional): set `GMAIL_USER`, `GMAIL_APP_PASSWORD` (a Gmail app password) and `EMAIL_TO`.
Tune with `RADAR_PROFILE`, `RADAR_HOURS`, `RADAR_TOP_N`, `RADAR_MODEL`.

## Deploy

1. Push to GitHub.
2. Settings -> Secrets and variables -> Actions: add `ANTHROPIC_API_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `EMAIL_TO`.
3. Settings -> Pages -> Source: **GitHub Actions**.
4. Actions tab -> *Daily AI Radar* -> **Run workflow**.

## Roadmap
- "Ask the radar" chat grounded in the collected items, with citations
- Public subscribe form with unsubscribe
