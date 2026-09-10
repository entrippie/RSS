#!/usr/bin/env python3
"""
AI & ML combined RSS feed.

Pulls a set of RSS/Atom feeds, filters and caps them, and publishes ONE
RSS feed (docs/feed.xml) that you can subscribe to in any RSS reader.
Items are kept on a rolling window, so your reader won't miss anything
even if it only syncs occasionally.

Built to run on a schedule via GitHub Actions + GitHub Pages. No secrets needed.

Optional environment variables:
  LOOKBACK_HOURS  how far back to look on each run (default 48)
  KEEP_DAYS       how long items stay in the feed (default 7)
  MAX_ITEMS       hard cap on feed size (default 400)
  ARXIV_KEYWORDS  comma-separated terms; if set, only arXiv papers whose title
                  or abstract contains one of them are included
  FEED_URL        public URL of the feed (auto-detected on GitHub Actions)
"""

import calendar
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

import feedparser

# (section, source name, feed URL, max items per run)
# Add, remove, or reorder freely.
FEEDS = [
    ("Research labs", "Google Research", "https://research.google/blog/rss/", 5),
    ("Research labs", "Google DeepMind", "https://deepmind.google/blog/rss.xml", 5),
    ("Research labs", "OpenAI", "https://openai.com/news/rss.xml", 5),
    ("Research labs", "Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", 5),
    ("Research labs", "Berkeley AI Research", "https://bair.berkeley.edu/blog/feed.xml", 5),
    ("Research labs", "Hugging Face", "https://huggingface.co/blog/feed.xml", 5),
    ("News", "MIT Technology Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed", 6),
    ("News", "The Verge", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", 6),
    ("News", "TechCrunch", "https://techcrunch.com/category/artificial-intelligence/feed/", 6),
    ("Newsletters & blogs", "Import AI", "https://importai.substack.com/feed", 3),
    ("Newsletters & blogs", "Ahead of AI (Sebastian Raschka)", "https://magazine.sebastianraschka.com/feed", 3),
    ("Newsletters & blogs", "Simon Willison", "https://simonwillison.net/atom/everything/", 5),
    ("Newsletters & blogs", "Lil'Log (Lilian Weng)", "https://lilianweng.github.io/index.xml", 3),
    ("arXiv papers", "arXiv cs.LG", "https://rss.arxiv.org/rss/cs.LG", 10),
    ("arXiv papers", "arXiv cs.AI", "https://rss.arxiv.org/rss/cs.AI", 10),
    ("arXiv papers", "arXiv cs.CL", "https://rss.arxiv.org/rss/cs.CL", 10),
]

FEED_TITLE = "AI & ML News and Research"
FEED_DESCRIPTION = "Curated machine learning and AI news, lab blogs, newsletters, and arXiv papers."
FEED_PATH = "docs/feed.xml"
STATE_PATH = "feed_state.json"

USER_AGENT = "Mozilla/5.0 (compatible; ai-ml-feed/1.0)"
LOOKBACK_HOURS = float(os.environ.get("LOOKBACK_HOURS") or "48")
KEEP_DAYS = float(os.environ.get("KEEP_DAYS") or "7")
MAX_ITEMS = int(os.environ.get("MAX_ITEMS") or "400")
ARXIV_KEYWORDS = [
    k.strip().lower() for k in os.environ.get("ARXIV_KEYWORDS", "").split(",") if k.strip()
]

DC_NS = "http://purl.org/dc/elements/1.1/"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("dc", DC_NS)
ET.register_namespace("atom", ATOM_NS)


def clean_text(raw, limit=400):
    """Strip HTML, collapse whitespace, trim to a readable length."""
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    # arXiv abstracts start with "arXiv:XXXX Announce Type: new Abstract: ..."
    if "Abstract:" in text[:150]:
        text = text.split("Abstract:", 1)[1].strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def entry_timestamp(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return calendar.timegm(parsed) if parsed else None


def repo_urls():
    """Work out the GitHub Pages feed URL and repo URL when running in Actions."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return os.environ.get("FEED_URL", ""), ""
    owner, name = repo.split("/", 1)
    if name.lower() == f"{owner.lower()}.github.io":
        pages = f"https://{owner.lower()}.github.io/feed.xml"
    else:
        pages = f"https://{owner.lower()}.github.io/{name}/feed.xml"
    return os.environ.get("FEED_URL") or pages, f"https://github.com/{repo}"


def collect():
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    collected = []
    seen_links = set()

    for section, source, url, max_items in FEEDS:
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
        except Exception as exc:
            print(f"warning: {source} failed: {exc}", file=sys.stderr)
            continue

        status = feed.get("status", 200)
        if status >= 400 or (feed.get("bozo") and not feed.entries):
            print(f"warning: {source} returned status {status}", file=sys.stderr)
            continue

        is_arxiv = "arxiv.org" in url
        items = []
        for entry in feed.entries:
            link = entry.get("link", "")
            ts = entry_timestamp(entry)
            if not link or link in seen_links or ts is None or ts < cutoff:
                continue
            if is_arxiv:
                # Skip revised versions of old papers; keep new submissions and cross-lists
                if entry.get("arxiv_announce_type", "new") not in ("new", "cross"):
                    continue
                if ARXIV_KEYWORDS:
                    haystack = (entry.get("title", "") + " " + entry.get("summary", "")).lower()
                    if not any(k in haystack for k in ARXIV_KEYWORDS):
                        continue
            items.append({
                "title": clean_text(entry.get("title", "(untitled)"), 200),
                "link": link,
                "summary": clean_text(entry.get("summary", "")),
                "source": source,
                "section": section,
                "source_feed": url,
                "ts": ts,
            })

        items.sort(key=lambda i: i["ts"], reverse=True)
        items = items[:max_items]
        seen_links.update(i["link"] for i in items)
        collected.extend(items)
        print(f"{source}: {len(items)} items")

    return collected


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def merge(old_items, new_items):
    by_link = {i["link"]: i for i in old_items}
    for item in new_items:
        by_link.setdefault(item["link"], item)
    cutoff = time.time() - KEEP_DAYS * 86400
    items = [i for i in by_link.values() if i["ts"] >= cutoff]
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items[:MAX_ITEMS]


def build_rss(items, feed_url, site_url):
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = FEED_TITLE
    ET.SubElement(channel, "link").text = site_url or feed_url or "https://github.com"
    ET.SubElement(channel, "description").text = FEED_DESCRIPTION
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(channel, "ttl").text = "360"
    if feed_url:
        ET.SubElement(channel, f"{{{ATOM_NS}}}link",
                      {"href": feed_url, "rel": "self", "type": "application/rss+xml"})

    for it in items:
        el = ET.SubElement(channel, "item")
        ET.SubElement(el, "title").text = it["title"]
        ET.SubElement(el, "link").text = it["link"]
        ET.SubElement(el, "guid", {"isPermaLink": "true"}).text = it["link"]
        ET.SubElement(el, "pubDate").text = format_datetime(
            datetime.fromtimestamp(it["ts"], timezone.utc))
        # Most readers show dc:creator as the author, so you can see where each item came from
        ET.SubElement(el, f"{{{DC_NS}}}creator").text = it["source"]
        ET.SubElement(el, "category").text = it["section"]
        ET.SubElement(el, "source", {"url": it["source_feed"]}).text = it["source"]
        ET.SubElement(el, "description").text = (
            f"<p><em>{html.escape(it['source'])} · {html.escape(it['section'])}</em></p>"
            + (f"<p>{html.escape(it['summary'])}</p>" if it["summary"] else "")
        )

    ET.indent(rss)
    return ET.ElementTree(rss)


def main():
    feed_url, site_url = repo_urls()
    old_items = load_state()
    items = merge(old_items, collect())

    unchanged = [i["link"] for i in items] == [i["link"] for i in old_items]
    if unchanged and os.path.exists(FEED_PATH):
        print("No new items; feed unchanged.")
        return

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)

    os.makedirs(os.path.dirname(FEED_PATH), exist_ok=True)
    open(os.path.join(os.path.dirname(FEED_PATH), ".nojekyll"), "a").close()
    build_rss(items, feed_url, site_url).write(FEED_PATH, encoding="utf-8", xml_declaration=True)

    print(f"Wrote {FEED_PATH} with {len(items)} items.")
    if feed_url:
        print(f"Subscribe in your RSS app at: {feed_url}")


if __name__ == "__main__":
    main()
