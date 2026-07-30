#!/usr/bin/env python3
"""byte-brief: a daily email digest of the top Hacker News stories.

Pulls the ranked front page from the official HN Firebase API, fetches a short
excerpt for each linked article, and emails the result as formatted HTML.

Standard library only -- nothing to install, nothing to break when Task
Scheduler runs this headless months from now.

    python hn_brief.py                 send the brief
    python hn_brief.py --dry-run       write a preview HTML file, send nothing
    python hn_brief.py --count 5       override story count
"""

from __future__ import annotations

import argparse
import gzip
import html
import json
import logging
import logging.handlers
import os
import re
import smtplib
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
LOG_PATH = HERE / "logs" / "byte-brief.log"
OUT_DIR = HERE / "out"

HN_API = "https://hacker-news.firebaseio.com/v0"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={}"

# Pretend to be a normal browser. Plenty of publishers return 403 to anything
# that looks scripted, and we only ever read the page's own metadata.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

ARTICLE_TIMEOUT = 15
API_TIMEOUT = 20
MAX_ARTICLE_BYTES = 600_000  # metadata lives in <head>; no need for the whole page
EXCERPT_CHARS = 320

log = logging.getLogger("byte-brief")


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "sender": "",
    "app_password": "",
    "recipients": [],
    "story_count": 10,
    "subject_prefix": "Byte Brief",
}


ENV_PREFIX = "BYTE_BRIEF_"


def load_config() -> dict:
    """Config comes from defaults, then config.json, then the environment.

    Later sources win. Running locally you use config.json; in CI there is no
    config.json (it is gitignored), so everything arrives as env vars fed from
    repository secrets.
    """
    cfg = dict(DEFAULT_CONFIG)

    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as fh:
            cfg.update(json.load(fh))

    for key in DEFAULT_CONFIG:
        value = os.environ.get(ENV_PREFIX + key.upper())
        if value:
            cfg[key] = value

    # Env vars are strings; recipients may arrive comma-separated.
    if isinstance(cfg["recipients"], str):
        cfg["recipients"] = [r.strip() for r in cfg["recipients"].split(",") if r.strip()]

    source = "config.json" if CONFIG_PATH.exists() else "environment"
    if not cfg["sender"]:
        raise SystemExit(f"'sender' is not set (checked {source} and {ENV_PREFIX}SENDER)")
    if not cfg["recipients"]:
        raise SystemExit(
            f"'recipients' is not set (checked {source} and {ENV_PREFIX}RECIPIENTS)"
        )
    return cfg


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


def _read_body(resp) -> bytes:
    """Read a response, transparently un-gzipping it."""
    raw = resp.read(MAX_ARTICLE_BYTES)
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in encoding:
            # A truncated gzip stream raises; decompressobj gives us what it can.
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        if "deflate" in encoding:
            return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw)
    except zlib.error:
        try:
            return gzip.decompress(raw)
        except Exception:
            return b""
    return raw


def get_json(url: str, attempts: int = 3):
    """GET JSON with a short backoff. The scheduler may fire before the network
    is fully up after a wake, so a couple of retries matter here."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "byte-brief/1.0"})
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                return json.loads(_read_body(resp).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retry on anything transient
            last = exc
            if i < attempts - 1:
                wait = 2 ** i * 3
                log.warning("API call failed (%s), retrying in %ss: %s", url, wait, exc)
                time.sleep(wait)
    raise RuntimeError(f"could not reach {url}: {last}")


class PageParser(HTMLParser):
    """Pulls the description metadata and body paragraphs out of an HTML page."""

    SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.paragraphs: list[str] = []
        self._skip_depth = 0
        self._in_p = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = (a.get("content") or "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "p":
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "p" and self._in_p:
            text = clean_text("".join(self._buf))
            if text:
                self.paragraphs.append(text)
            self._in_p = False
            self._buf = []

    def handle_data(self, data):
        if self._skip_depth == 0 and self._in_p:
            self._buf.append(data)


BOILERPLATE = re.compile(
    r"cookie|subscribe|sign up|newsletter|javascript|enable js|privacy policy|"
    r"all rights reserved|advertisement",
    re.I,
)


def clean_text(raw: str) -> str:
    # NFKC folds typographic ligatures ("ﬁve" -> "five") and other presentation
    # forms that PDF-ish publisher pages leak into their metadata.
    text = unicodedata.normalize("NFKC", html.unescape(raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int = EXCERPT_CHARS) -> str:
    text = clean_text(text)
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def strip_tags(raw: str) -> str:
    """HN's own `text` field is a small subset of HTML (<p>, <i>, <a>)."""
    with_breaks = re.sub(r"<\s*/?\s*p\s*>", " ", raw or "", flags=re.I)
    return clean_text(re.sub(r"<[^>]+>", "", with_breaks))


def fetch_excerpt(url: str) -> tuple[str, str]:
    """Return (excerpt, source_label). Never raises -- a failure is just an
    empty excerpt, because one dead link shouldn't cost us the whole email."""
    try:
        req = urllib.request.Request(url, headers=BROWSER_HEADERS)
        with urllib.request.urlopen(req, timeout=ARTICLE_TIMEOUT) as resp:
            ctype = (resp.headers.get_content_type() or "").lower()
            if "html" not in ctype and "xml" not in ctype:
                return "", f"unsupported content type: {ctype or 'unknown'}"
            charset = resp.headers.get_content_charset() or "utf-8"
            body = _read_body(resp)
    except urllib.error.HTTPError as exc:
        return "", f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return "", type(exc).__name__

    if not body:
        return "", "empty response"

    try:
        text = body.decode(charset, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")

    parser = PageParser()
    try:
        parser.feed(text)
    except Exception:  # noqa: BLE001 - malformed markup is common; use what we got
        pass

    for key in ("og:description", "twitter:description", "description"):
        value = clean_text(parser.meta.get(key, ""))
        if len(value) >= 60:
            return truncate(value), "meta"

    for para in parser.paragraphs:
        if len(para) >= 90 and not BOILERPLATE.search(para[:120]):
            return truncate(para), "body"

    # Last resort: a short meta description beats nothing.
    for key in ("og:description", "twitter:description", "description"):
        value = clean_text(parser.meta.get(key, ""))
        if value:
            return truncate(value), "meta"

    return "", "no summary text found"


def top_stories(count: int) -> list[dict]:
    """Top `count` stories in HN's own front-page order.

    topstories.json mixes in job posts and polls, so we over-fetch and filter
    down to actual stories while preserving rank.
    """
    ids = get_json(f"{HN_API}/topstories.json")
    if not isinstance(ids, list) or not ids:
        raise RuntimeError("topstories.json returned no ids")

    candidates = ids[: count + 15]
    with ThreadPoolExecutor(max_workers=8) as pool:
        items = list(pool.map(_safe_item, candidates))

    stories = [it for it in items if it and it.get("type") == "story" and it.get("title")]
    return stories[:count]


def _safe_item(item_id: int) -> dict | None:
    try:
        return get_json(f"{HN_API}/item/{item_id}.json", attempts=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch item %s: %s", item_id, exc)
        return None


def enrich(stories: list[dict]) -> list[dict]:
    """Attach an excerpt and display fields to each story."""

    def work(story: dict) -> dict:
        url = story.get("url")
        if not url:
            # Ask HN / Show HN text posts carry their body in `text`.
            body = strip_tags(story.get("text", ""))
            story["excerpt"] = truncate(body) if body else ""
            story["excerpt_note"] = "" if body else "discussion post"
            story["link"] = HN_ITEM_URL.format(story["id"])
            story["domain"] = "news.ycombinator.com"
            return story

        excerpt, reason = fetch_excerpt(url)
        story["excerpt"] = excerpt
        if excerpt:
            story["excerpt_note"] = ""
        else:
            # Keep the technical reason in the log; the email gets plain English.
            log.info("no excerpt for %s -- %s", url, reason)
            story["excerpt_note"] = "No preview available — open the link to read."
        story["link"] = url
        story["domain"] = (urlparse(url).netloc or "").removeprefix("www.")
        return story

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(work, stories))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def age(story: dict) -> str:
    posted = story.get("time")
    if not posted:
        return ""
    hours = (datetime.now(timezone.utc).timestamp() - posted) / 3600
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def render_html(stories: list[dict], date_label: str) -> str:
    e = html.escape
    rows = []
    for i, s in enumerate(stories, 1):
        hn_link = HN_ITEM_URL.format(s["id"])
        meta_bits = [f"{s.get('score', 0)} points"]
        if s.get("domain"):
            meta_bits.append(e(s["domain"]))
        if age(s):
            meta_bits.append(age(s))

        if s.get("excerpt"):
            body = (
                '<p style="margin:10px 0 0;font-size:15px;line-height:1.55;'
                f'color:#3d3d3d;">{e(s["excerpt"])}</p>'
            )
        else:
            body = (
                '<p style="margin:10px 0 0;font-size:14px;line-height:1.5;'
                f'color:#9a9a9a;font-style:italic;">{e(s.get("excerpt_note", ""))}</p>'
            )

        rows.append(
            f"""
      <div style="padding:20px 0;border-bottom:1px solid #ececec;">
        <div style="font-size:12px;font-weight:700;color:#ff6600;
                    letter-spacing:.08em;margin-bottom:6px;">{i:02d}</div>
        <a href="{e(s["link"])}"
           style="font-size:19px;font-weight:600;line-height:1.35;color:#111;
                  text-decoration:none;">{e(s["title"])}</a>
        <div style="margin-top:7px;font-size:13px;color:#8a8a8a;">
          {' &middot; '.join(meta_bits)}
          &middot; <a href="{e(hn_link)}" style="color:#ff6600;
             text-decoration:none;">{s.get('descendants', 0)} comments</a>
        </div>
        {body}
      </div>"""
        )

    return f"""<!doctype html>
<html>
<body style="margin:0;padding:0;background:#f6f6ef;">
  <div style="max-width:640px;margin:0 auto;padding:28px 20px 40px;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
    <div style="background:#ffffff;border-radius:10px;padding:26px 28px 8px;
                box-shadow:0 1px 3px rgba(0,0,0,.07);">
      <div style="border-bottom:3px solid #ff6600;padding-bottom:14px;">
        <div style="font-size:22px;font-weight:700;color:#111;">Byte Brief</div>
        <div style="margin-top:4px;font-size:13px;color:#8a8a8a;">
          Top {len(stories)} on Hacker News &middot; {e(date_label)}
        </div>
      </div>
      {''.join(rows)}
      <div style="padding:22px 0 18px;font-size:12px;color:#a8a8a8;
                  text-align:center;line-height:1.6;">
        Headlines and rankings via the
        <a href="https://github.com/HackerNews/API"
           style="color:#a8a8a8;">Hacker News API</a>.<br>
        Excerpts are pulled from each article's own page.
      </div>
    </div>
  </div>
</body>
</html>"""


def render_text(stories: list[dict], date_label: str) -> str:
    lines = [f"BYTE BRIEF - Top {len(stories)} on Hacker News", date_label, ""]
    for i, s in enumerate(stories, 1):
        lines.append(f"{i:02d}. {s['title']}")
        bits = [f"{s.get('score', 0)} points", f"{s.get('descendants', 0)} comments"]
        if s.get("domain"):
            bits.insert(0, s["domain"])
        lines.append("    " + " | ".join(bits))
        lines.append(f"    {s['link']}")
        lines.append(f"    discussion: {HN_ITEM_URL.format(s['id'])}")
        if s.get("excerpt"):
            lines.append(f"    {s['excerpt']}")
        elif s.get("excerpt_note"):
            lines.append(f"    {s['excerpt_note']}")
        lines.append("")
    lines.append("Headlines via the Hacker News API: https://github.com/HackerNews/API")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


def send_email(cfg: dict, subject: str, html_body: str, text_body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=cfg["sender"].split("@")[-1])
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if not cfg["app_password"]:
        raise SystemExit(
            "No app password. Put it in config.json or set BYTE_BRIEF_APP_PASSWORD."
        )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["smtp_host"], int(cfg["smtp_port"]), context=context,
                          timeout=45) as smtp:
        smtp.login(cfg["sender"], cfg["app_password"])
        smtp.send_message(msg)
    log.info("sent %r to %s", subject, ", ".join(cfg["recipients"]))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def setup_logging(verbose: bool) -> None:
    # The Windows console defaults to cp1252, which blows up on the typographic
    # characters that turn up constantly in article text.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")

    # Capped and rotated so an unattended daily job can't grow this forever.
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)


def main() -> int:
    ap = argparse.ArgumentParser(description="Email a daily Hacker News digest.")
    ap.add_argument("--dry-run", action="store_true",
                    help="build the digest and save a preview instead of emailing")
    ap.add_argument("--count", type=int, help="number of stories (default from config)")
    ap.add_argument("--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    setup_logging(args.verbose)

    try:
        cfg = load_config()
        count = args.count or int(cfg["story_count"])
        log.info("fetching top %d stories", count)

        stories = top_stories(count)
        if not stories:
            log.error("no stories returned; nothing to send")
            return 1
        stories = enrich(stories)

        got = sum(1 for s in stories if s.get("excerpt"))
        log.info("built %d stories, %d with excerpts", len(stories), got)

        date_label = datetime.now().strftime("%A, %B %d, %Y")
        html_body = render_html(stories, date_label)
        text_body = render_text(stories, date_label)
        subject = f"{cfg['subject_prefix']}: {stories[0]['title'][:70]}"

        if args.dry_run:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            preview = OUT_DIR / "preview.html"
            preview.write_text(html_body, encoding="utf-8")
            print(text_body)
            print(f"\n[dry run] HTML preview written to {preview}")
            return 0

        send_email(cfg, subject, html_body, text_body)
        return 0

    except SystemExit:
        raise
    except Exception:
        log.exception("run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
