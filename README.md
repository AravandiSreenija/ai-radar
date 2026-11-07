# AI Radar

AI moves too fast to follow by hand. AI Radar collects the last 24 hours of AI news
from public sources, lets Claude pick the 10 that matter, and publishes them as a
static website and a daily email, every link pointing to the original source.

## How it works

1. **Collect** - ~35 sources plus optional web-search discovery: lab and tech blogs (OpenAI, Google, DeepMind, Hugging Face,
   NVIDIA, Microsoft, AWS, Anthropic news page, Cursor changelog, press), arXiv cs.AI/cs.CL,
   Reddit (r/LocalLLaMA, r/MachineLearning), GitHub releases (Claude Code, Codex, ollama,
   llama.cpp, vllm, LangChain, MCP...) and the Hacker News API. Per-source caps keep one
   noisy feed from crowding out the rest.
2. **Filter and dedupe** - only items from the last 24h (widened once on quiet days),
   duplicates removed by URL and normalised title.
3. **Curate** - Claude receives numbered candidates and returns only ids, a category, a 1-5 score
   and a short "why it matters" line for a reader profile you set. The text shown under each
   headline is the start of the article's own feed text (cut at a word boundary), not a model
   rewrite - cheaper, and it cannot misstate the article.
4. **Publish** - `site/index.html` (GitHub Pages) and an HTML email.
5. **Schedule** - a GitHub Actions cron runs it every morning.

### Safeguards
- Claude returns **ids only**; titles and URLs come from the fetched feeds, so links cannot be invented.
- Time-window filter and dedupe before the model sees anything.
- One failing source never stops a run.
- Pages state which parts are AI-generated (picks, scores, why-it-matters) and which are the article's own words.

## Features
- A **More stories** list (up to 100 extra headlines, with Load more) below the curated top 10; clearly labelled as not AI-curated
- Top-10 stories of the last 24h, newspaper-style cards with the article's opening lines and a short "why it matters"
- Category filters, search, a highlighted top story
- 30-day archive (the workflow commits `archive/<date>.json`) with a day picker
- Source health in the footer

## Web search discovery (optional)

Besides the fixed feeds, each run asks Claude to run a handful of web searches (new models, developer tools,
agent/MCP/RAG news, open-source LLMs, safety and regulation). It mirrors the "query plan -> web search agent"
pattern of larger internal tools. The search results' own URLs and titles join the same candidate pool, so links
are never written by the model. It uses Claude's web search tool (at most 10 searches a run), which must be
enabled for your organisation in the Anthropic Console. If it is off or fails, the run carries on with the feeds
and the page footer lists "Web search" under failed sources. Turn it off with the repo variable
`RADAR_WEB_SEARCH` = `0`. Searches are billed per search on top of token costs.

## Ask Radar (optional chat on every story)

Each story gets an **Ask Radar** button. It opens a side panel that reads the article and gives a brief
summary, then lets you ask follow-up questions about that story. Because the Anthropic key must stay
secret, the chat runs through a tiny backend in `worker/worker.js` (a Cloudflare Worker). Without it the
site works normally and the buttons are simply hidden.

Safeguards in the Worker: only this site's origin may call it; the story URL must appear in the site's own
published `news.json` (it is not an open proxy); article text is treated as untrusted data; request, article,
history and output sizes are capped; errors are generic; and there is a best-effort per-IP rate limit.
It cannot read what's behind paywalls or on JavaScript-only pages - then it says it used the feed excerpt.

Setup (about 10 minutes, free tier):
1. Cloudflare dashboard -> Workers & Pages -> Create -> Worker. Name it `ai-radar-chat`, Deploy, then Edit code
   and paste in `worker/worker.js`. Deploy again.
2. Worker -> Settings -> Variables and Secrets: add secret `ANTHROPIC_API_KEY`; text variables
   `ALLOWED_ORIGIN` (e.g. `https://aravandisreenija.github.io`, origin only) and `NEWS_URL`
   (e.g. `https://aravandisreenija.github.io/ai-radar/news.json`). Optional: `MODEL` (default `claude-haiku-5-5`).
3. Copy the Worker URL (`https://ai-radar-chat.<you>.workers.dev`).
4. GitHub repo -> Settings -> Secrets and variables -> Actions -> **Variables** tab -> New variable
   `RADAR_CHAT_URL` = the Worker URL.
5. Run the **Daily AI Radar** workflow. Set a small monthly spend limit on your Anthropic account as a backstop.

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
- Ask across the whole archive ("what shipped this week?"), not just one story
- Public subscribe form with unsubscribe
