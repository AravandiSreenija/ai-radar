#!/usr/bin/env python3
"""AI Radar - collect the last 24h of AI news, let Claude pick the top 10,
write a static website and (optionally) send a daily email.

Safeguards:
  * Claude only sees numbered items and returns ids. Every URL shown to the
    reader comes from the fetched feed, so links can't be invented.
  * Only items from the last HOURS hours are used.
  * Duplicates (same URL or near-identical title) are dropped.
"""
import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    from defusedxml import ElementTree as ET  # safe against hostile XML
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET

ROOT = Path(__file__).parent
SITE = ROOT / "site"
HOURS = int(os.environ.get("RADAR_HOURS", "24"))
TOP_N = int(os.environ.get("RADAR_TOP_N", "10"))
MODEL = os.environ.get("RADAR_MODEL", "claude-sonnet-5-5")
PROFILE = os.environ.get(
    "RADAR_PROFILE",
    "A software/data professional who wants to stay current on new models, "
    "AI tools, agents and practical applied AI.",
)
UA = {"User-Agent": "ai-radar/1.0 (personal AI news digest)"}

# (name, kind, url)  kind: feed | hn
SOURCES = [
    ("OpenAI", "feed", "https://openai.com/news/rss.xml"),
    ("Google AI", "feed", "https://blog.google/technology/ai/rss/"),
    ("Google DeepMind", "feed", "https://deepmind.google/blog/rss.xml"),
    ("Hugging Face", "feed", "https://huggingface.co/blog/feed.xml"),
    ("NVIDIA", "feed", "https://blogs.nvidia.com/feed/"),
    ("TechCrunch AI", "feed", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "feed", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("GitHub: ollama", "feed", "https://github.com/ollama/ollama/releases.atom"),
    ("GitHub: llama.cpp", "feed", "https://github.com/ggml-org/llama.cpp/releases.atom"),
    ("GitHub: transformers", "feed", "https://github.com/huggingface/transformers/releases.atom"),
    ("GitHub: langchain", "feed", "https://github.com/langchain-ai/langchain/releases.atom"),
    ("GitHub: vllm", "feed", "https://github.com/vllm-project/vllm/releases.atom"),
    ("Claude Code releases", "feed", "https://github.com/anthropics/claude-code/releases.atom"),
    ("Anthropic Python SDK", "feed", "https://github.com/anthropics/anthropic-sdk-python/releases.atom"),
    ("OpenAI Python SDK", "feed", "https://github.com/openai/openai-python/releases.atom"),
    ("OpenAI Codex releases", "feed", "https://github.com/openai/codex/releases.atom"),
    ("Gemini CLI releases", "feed", "https://github.com/google-gemini/gemini-cli/releases.atom"),
    ("MCP spec", "feed", "https://github.com/modelcontextprotocol/modelcontextprotocol/releases.atom"),
    ("Hacker News", "hn", "https://hn.algolia.com/api/v1/search_by_date"),
]
HN_QUERIES = ["LLM", "Claude", "OpenAI", "Gemini", "AI agent", "Anthropic",
              "Cursor", "Claude Code", "Codex", "DeepSeek", "Mistral", "Llama"]


# ----------------------------------------------------------------- fetching
def _clean(text, limit=400):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit]


def _parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _safe_url(url):
    return url if urlparse(url or "").scheme in ("http", "https") else None


def fetch_feed(name, url):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        fields = {c.tag.split("}")[-1]: c for c in el}
        title = _clean(fields["title"].text if "title" in fields else "", 200)
        link = None
        if "link" in fields:
            link = fields["link"].get("href") or (fields["link"].text or "").strip()
        date = _parse_date(
            next((fields[k].text for k in ("pubDate", "published", "updated", "date")
                  if k in fields), None)
        )
        desc = ""
        for k in ("description", "summary", "content", "encoded"):
            if k in fields and fields[k].text:
                desc = _clean(fields[k].text)
                break
        link = _safe_url(link)
        if title and link and date:
            items.append({"source": name, "title": title, "url": link,
                          "published": date, "snippet": desc})
    return items


def fetch_hn(name, url, since):
    items = []
    for q in HN_QUERIES:
        params = {"query": q, "tags": "story",
                  "numericFilters": f"created_at_i>{int(since.timestamp())},points>15",
                  "hitsPerPage": 15}
        r = requests.get(url, params=params, headers=UA, timeout=20)
        r.raise_for_status()
        for h in r.json().get("hits", []):
            link = _safe_url(h.get("url")) or f"https://news.ycombinator.com/item?id={h['objectID']}"
            items.append({
                "source": f"{name} ({h.get('points', 0)} pts)",
                "title": _clean(h.get("title"), 200),
                "url": link,
                "published": datetime.fromtimestamp(h["created_at_i"], timezone.utc),
                "snippet": "",
            })
    return items


def _meta(page, *names):
    """First matching <meta property|name=... content=...> value."""
    for n in names:
        m = (re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]*content=["\']([^"\']+)', page, re.I)
             or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{re.escape(n)}["\']', page, re.I))
        if m:
            return html.unescape(m.group(1))
    return None


def fetch_page(name, index_url, link_pattern, max_links=8):
    """For sites without RSS (Anthropic news, Cursor changelog): read the index page,
    follow the newest links, and take title/date from each article's own metadata.
    Articles without a readable publish date are skipped - they can't pass the 24h filter."""
    base = "{0.scheme}://{0.netloc}".format(urlparse(index_url))
    r = requests.get(index_url, headers=UA, timeout=20)
    r.raise_for_status()
    links, seen = [], set()
    for m in re.finditer(link_pattern, r.text):
        href = m.group(0).split("#")[0]
        full = href if href.startswith("http") else base + href
        if full.rstrip("/") != index_url.rstrip("/") and full not in seen:
            seen.add(full)
            links.append(full)
    items = []
    for link in links[:max_links]:
        try:
            p = requests.get(link, headers=UA, timeout=20)
            p.raise_for_status()
            date = _parse_date(
                _meta(p.text, "article:published_time", "datePublished", "og:article:published_time")
                or (re.search(r'"datePublished"\s*:\s*"([^"]+)"', p.text) or [None, None])[1]
                or (re.search(r'<time[^>]+datetime=["\']([^"\']+)', p.text) or [None, None])[1]
            )
            title = _clean(_meta(p.text, "og:title", "twitter:title") or "", 200)
            desc = _clean(_meta(p.text, "og:description", "description") or "")
            if date and title and _safe_url(link):
                items.append({"source": name, "title": title, "url": link,
                              "published": date, "snippet": desc})
        except Exception:
            continue  # one bad article page shouldn't drop the whole source
    return items


PAGE_SOURCES = [
    # name, index page, regex that matches article links on that page
    ("Anthropic News", "https://www.anthropic.com/news", r'(?:https://www\.anthropic\.com)?/news/[a-z0-9][a-z0-9\-]+'),
    ("Cursor Changelog", "https://cursor.com/changelog", r'(?:https://cursor\.com)?/changelog/[a-z0-9][a-z0-9\-\.]+'),
]


def collect(hours):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    found, failed = [], []
    for name, index_url, pattern in PAGE_SOURCES:
        try:
            found += [i for i in fetch_page(name, index_url, pattern) if i["published"] >= since]
        except Exception as exc:
            failed.append(f"{name}: {type(exc).__name__}")
    for name, kind, url in SOURCES:
        try:
            got = fetch_hn(name, url, since) if kind == "hn" else fetch_feed(name, url)
            found += [i for i in got if i["published"] >= since]
        except Exception as exc:  # one broken source must not kill the run
            failed.append(f"{name}: {type(exc).__name__}")
    return dedupe(found), failed


def dedupe(items):
    seen_url, seen_title, out = set(), set(), []
    for it in sorted(items, key=lambda i: i["published"], reverse=True):
        key_u = it["url"].split("#")[0].rstrip("/")
        key_t = re.sub(r"[^a-z0-9]+", " ", it["title"].lower()).strip()
        if key_u in seen_url or key_t in seen_title:
            continue
        seen_url.add(key_u)
        seen_title.add(key_t)
        out.append(it)
    return out


# ------------------------------------------------------------------- ranking
def _score(v):
    """Clamp whatever the model returned to an int 1-5 (default 3)."""
    try:
        return max(1, min(5, int(v)))
    except (TypeError, ValueError):
        return 3


def rank_with_claude(items, top_n):
    import anthropic

    pool = items[:80]
    listing = "\n".join(
        f"[{i}] ({it['source']}) {it['title']} - {it['snippet'][:200]}"
        for i, it in enumerate(pool)
    )
    prompt = f"""You curate a daily AI news digest for this reader:
{PROFILE}

Below are candidate items from the last {HOURS} hours. Choose the {top_n} most
important and distinct ones (new models, tools, agent/RAG techniques, major
industry or safety news). Skip duplicates of the same story, marketing fluff and
minor patch releases. Use ONLY the information given. Never invent facts.

Return ONLY a JSON array of up to {top_n} objects, best first:
{{"id": <number from brackets>, "category": "Models|Tools|Techniques|Industry|Security|Research",
 "summary": "1-2 plain sentences, factual", "why_it_matters": "1 sentence for this reader",
 "score": <1-5 relevance to the reader>}}

Candidates:
{listing}"""
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    start, end = text.find("["), text.rfind("]")
    picks = json.loads(text[start:end + 1])
    out, used = [], set()
    for p in picks:
        i = p.get("id")
        if not isinstance(i, int) or not 0 <= i < len(pool) or i in used:
            continue
        used.add(i)
        it = dict(pool[i])  # URL/title/source come from the feed, not the model
        it.update(category=str(p.get("category", "Industry"))[:20],
                  summary=str(p.get("summary", ""))[:400],
                  why_it_matters=str(p.get("why_it_matters", ""))[:300],
                  score=_score(p.get("score")))
        out.append(it)
    return out[:top_n]


def rank_fallback(items, top_n):
    """No API key: newest items, one per source first, so the site still builds."""
    out, per_source = [], {}
    for it in items:
        s = it["source"].split(" (")[0]
        if per_source.get(s, 0) >= 2:
            continue
        per_source[s] = per_source.get(s, 0) + 1
        out.append(dict(it, category="News", summary=it["snippet"][:240] or it["title"],
                        why_it_matters="", score=3))
        if len(out) == top_n:
            break
    return out


# ------------------------------------------------------------------ rendering
def esc(s):
    return html.escape(str(s), quote=True)


ARCHIVE = ROOT / "archive"
KEEP_DAYS = 30

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Radar</title>
<style>
 :root{--bg:#f5f6fa;--card:#fff;--ink:#1c1e26;--mute:#6a7080;--line:#e3e6ee;--accent:#3b4bd8}
 body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--ink);margin:0}
 header{background:#1c1e26;color:#fff;padding:26px 20px;text-align:center}
 header h1{margin:0 0 4px;font-size:28px} header p{margin:0;opacity:.75}
 main{max-width:780px;margin:20px auto;padding:0 16px}
 .bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
 .bar select,.bar input{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#fff}
 .bar input{flex:1;min-width:160px}
 .chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
 .chips button{font:inherit;font-size:13px;border:1px solid var(--line);background:#fff;border-radius:999px;padding:4px 12px;cursor:pointer}
 .chips button.on{background:var(--accent);border-color:var(--accent);color:#fff}
 .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 20px;margin-bottom:14px}
 .card.top{border:2px solid var(--accent)}
 .meta{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--mute);align-items:center}
 .chip{background:#e8ecff;color:var(--accent);border-radius:999px;padding:2px 10px;font-weight:600}
 .badge{background:#fff3d6;color:#8a5a00;border-radius:999px;padding:2px 10px;font-weight:600}
 .star{background:var(--accent);color:#fff;border-radius:999px;padding:2px 10px;font-weight:600}
 h2{font-size:18px;margin:10px 0 6px} h2 a{color:inherit;text-decoration:none} h2 a:hover{text-decoration:underline}
 p{margin:6px 0;line-height:1.5} .why{color:#3a3f4f;font-size:14px}
 .read{font-size:14px;color:var(--accent);text-decoration:none;font-weight:600}
 .empty{color:var(--mute);text-align:center;padding:30px}
 footer{text-align:center;font-size:12px;color:#8a90a0;padding:20px;max-width:780px;margin:0 auto}
</style></head><body>
<header><h1>AI Radar</h1><p id="sub"></p></header>
<main>
 <div class="bar"><select id="day" aria-label="Day"></select>
  <input id="q" type="search" placeholder="Search stories, sources..." aria-label="Search"></div>
 <div class="chips" id="chips"></div>
 <div id="list"></div>
</main>
<footer id="foot"></footer>
<noscript><p class="empty">Please enable JavaScript to view AI Radar.</p></noscript>
<script id="data" type="application/json">__DATA__</script>
<script>
const DAYS = JSON.parse(document.getElementById('data').textContent);
let di = 0, cat = 'All', q = '';
const $ = id => document.getElementById(id);
function el(tag, cls, text){const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e;}
const safe = u => /^https?:\\/\\//.test(u) ? u : '#';
function fmt(iso){return new Date(iso).toLocaleString('en-GB',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'UTC'})+' UTC';}
function render(){
  const day = DAYS[di];
  const base = day.items.slice().sort((a,b) => b.score - a.score).slice(0, 10);
  $('sub').textContent = day.label + ' - ' + base.length + ' stories';
  const cats = ['All', ...new Set(base.map(i => i.category))];
  if(!cats.includes(cat)) cat = 'All';
  const chips = $('chips'); chips.replaceChildren();
  cats.forEach(c => { const b = el('button', c===cat?'on':'', c); b.onclick = () => {cat=c; render();}; chips.appendChild(b); });
  const top = base[0];
  const needle = q.trim().toLowerCase();
  const shown = base
    .filter(i => cat==='All' || i.category===cat)
    .filter(i => !needle || (i.title+' '+i.summary+' '+i.source+' '+i.why_it_matters).toLowerCase().includes(needle))
    ;
  const list = $('list'); list.replaceChildren();
  if(!shown.length){ list.appendChild(el('div','empty','No stories match.')); }
  shown.forEach(i => {
    const c = el('article', 'card' + (i===top ? ' top' : ''));
    const m = el('div','meta');
    if(i===top) m.appendChild(el('span','star','Top story'));
    m.appendChild(el('span','chip',i.category));
    m.appendChild(el('span','badge','Relevance ' + i.score + '/5'));
    m.appendChild(el('span','',i.source));
    m.appendChild(el('span','',fmt(i.published)));
    c.appendChild(m);
    const h = el('h2'); const a = el('a','',i.title); a.href=safe(i.url); a.target='_blank'; a.rel='noopener noreferrer'; h.appendChild(a); c.appendChild(h);
    c.appendChild(el('p','',i.summary));
    const why = i.why_it_matters;
    if(why){ const w = el('p','why'); w.appendChild(el('b','','Why it matters: ')); w.appendChild(document.createTextNode(why)); c.appendChild(w); }
    const r = el('a','read','Read source \\u2192'); r.href=safe(i.url); r.target='_blank'; r.rel='noopener noreferrer'; c.appendChild(r);
    list.appendChild(c);
  });
  const f = $('foot'); f.replaceChildren();
  f.appendChild(el('div','',day.note));
  f.appendChild(el('div','','Sources: ' + day.sources_ok + ' OK' + (day.sources_failed.length ? ', failed: ' + day.sources_failed.join(', ') : ', none failed')));
  f.appendChild(el('div','','Summaries are AI-generated from the linked sources - always check the source.'));
}
DAYS.forEach((d,i) => { const o = el('option','',d.label); o.value = i; $('day').appendChild(o); });
$('day').onchange = e => { di = +e.target.value; cat='All'; render(); };
$('q').oninput = e => { q = e.target.value; render(); };
render();
</script></body></html>"""


def day_record(top, date_iso, label, note, sources_ok, failed):
    return {
        "date": date_iso, "label": label, "note": note,
        "sources_ok": sources_ok, "sources_failed": failed,
        "items": [dict(i, published=i["published"].isoformat()) for i in top],
    }


def load_archive():
    days = []
    for f in sorted(ARCHIVE.glob("*.json"), reverse=True)[:KEEP_DAYS]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("items"):
                days.append(d)
        except (json.JSONDecodeError, OSError):
            continue  # a corrupt archive file must not break the site
    return days


def render_site(days):
    data = json.dumps(days).replace("</", "<\\/")  # keep data from closing the script tag
    return PAGE.replace("__DATA__", data)


def render_email(items, date_str):
    rows = "".join(
        f"<li style='margin-bottom:14px'><b>{esc(it['title'])}</b>"
        f" <span style='color:#6a7080'>({esc(it['source'])})</span><br>"
        f"{esc(it['summary'])}<br>"
        f"<a href='{esc(it['url'])}'>Read source</a></li>" for it in items)
    return f"<h2>AI Radar - {esc(date_str)}</h2><ol>{rows}</ol>"


def send_email(items, date_str):
    user, pwd, to = (os.environ.get(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "EMAIL_TO"))
    if not (user and pwd and to):
        print("Email skipped (GMAIL_USER / GMAIL_APP_PASSWORD / EMAIL_TO not set).")
        return
    msg = EmailMessage()
    msg["Subject"] = f"AI Radar: top {len(items)} AI stories - {date_str}"
    msg["From"], msg["To"] = user, to
    msg.set_content("\n\n".join(f"{n}. {i['title']}\n{i['summary']}\n{i['url']}"
                                for n, i in enumerate(items, 1)))
    msg.add_alternative(render_email(items, date_str), subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pwd)
        s.send_message(msg)
    print(f"Email sent to {to}.")


# ----------------------------------------------------------------------- main
def main():
    items, failed = collect(HOURS)
    window = HOURS
    if len(items) < TOP_N:  # quiet day: widen once so the page isn't empty
        items, failed = collect(HOURS * 3)
        window = HOURS * 3
    print(f"{len(items)} unique items in last {window}h; failed sources: {failed or 'none'}")
    if not items:
        sys.exit("No items collected - check network/sources.")

    if os.environ.get("ANTHROPIC_API_KEY"):
        top = rank_with_claude(items, TOP_N)
        note = f"Curated by {MODEL}. Window: last {window}h."
    else:
        print("ANTHROPIC_API_KEY not set - using simple fallback ranking.")
        top = rank_fallback(items, TOP_N)
        note = f"Fallback ranking (no AI curation). Window: last {window}h."

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %d %B %Y")
    sources_ok = len(SOURCES) + len(PAGE_SOURCES) - len(failed)
    ARCHIVE.mkdir(exist_ok=True)
    SITE.mkdir(exist_ok=True)
    record = day_record(top, now.strftime("%Y-%m-%d"), date_str, note, sources_ok,
                        [f.split(":")[0] for f in failed])
    (ARCHIVE / f"{record['date']}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    days = load_archive()
    (SITE / "index.html").write_text(render_site(days), encoding="utf-8")
    (SITE / "news.json").write_text(json.dumps(days, indent=2), encoding="utf-8")
    print(f"Wrote site/index.html: {len(days)} day(s), today has {len(top)} items.")
    if "--no-email" not in sys.argv:
        send_email(top, date_str)


if __name__ == "__main__":
    main()
