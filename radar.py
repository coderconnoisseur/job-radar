"""Job Radar: polls company hiring-system feeds and pings Telegram about new SWE jobs.

Usage:
  python radar.py              # normal run: alert on jobs not seen before
  python radar.py --list       # print every job that currently matches your filters
  python radar.py --check acme # test one company slug against all hiring systems
  python radar.py --test-alert # send a test message to Telegram

Env vars (optional; without them alerts are printed to the console):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import concurrent.futures as cf
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
CONFIG = HERE / "config.toml"
SEEN = HERE / "seen.json"
UA = {"User-Agent": "Mozilla/5.0 (job-radar; personal use)"}
SEEN_TTL_DAYS = 60  # forget closed jobs after this long so seen.json stays small


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def iso_to_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Each fetcher returns a list of dicts: id, title, location, url, posted (unix ts or None)

def greenhouse(slug):
    d = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [{
        "id": f"gh:{slug}:{j['id']}",
        "title": j["title"],
        "location": (j.get("location") or {}).get("name", ""),
        "url": j["absolute_url"],
        "posted": iso_to_ts(j.get("first_published") or j.get("updated_at")),
    } for j in d.get("jobs", [])]


def lever(slug):
    d = fetch_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in d:
        c = j.get("categories") or {}
        locs = c.get("allLocations") or [c.get("location", "")]
        loc = " / ".join(x for x in locs if x)
        if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc += " (Remote)"
        out.append({
            "id": f"lv:{slug}:{j['id']}",
            "title": j["text"],
            "location": loc,
            "url": j["hostedUrl"],
            "posted": j["createdAt"] / 1000 if j.get("createdAt") else None,
        })
    return out


def ashby(slug):
    d = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        loc = " / ".join(x for x in locs if x)
        if j.get("isRemote") and "remote" not in loc.lower():
            loc += " (Remote)"
        out.append({
            "id": f"ab:{slug}:{j['id']}",
            "title": j["title"],
            "location": loc,
            "url": j["jobUrl"],
            "posted": iso_to_ts(j.get("publishedAt")),
        })
    return out


def smartrecruiters(slug):
    out, offset = [], 0
    while True:
        d = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}")
        for j in d.get("content", []):
            l = j.get("location") or {}
            country = (l.get("country") or "").lower()
            country = "India" if country == "in" else country.upper()
            loc = ", ".join(x for x in [l.get("city"), l.get("region"), country] if x)
            if l.get("remote"):
                loc += " (Remote)"
            out.append({
                "id": f"sr:{slug}:{j['id']}",
                "title": j["name"],
                "location": loc,
                "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                "posted": iso_to_ts(j.get("releasedDate")),
            })
        offset += 100
        if offset >= d.get("totalFound", 0) or offset >= 1000:
            return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters}


def load_config():
    cfg = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    f = cfg["filters"]
    rx = {k: re.compile(v.strip(), re.I) for k, v in f.items()}
    return cfg["companies"], rx


def matches(job, rx):
    title, loc = job["title"], job["location"]
    if not rx["role"].search(title) or rx["exclude"].search(title):
        return False
    if rx["india"].search(loc):
        return True
    if not rx["remote"].search(loc):
        return False
    parts = [p.strip() for p in re.split(r"[,/()\-–|;]+|or", loc)]
    return all(not p or rx["remote"].fullmatch(p) or rx["global_region"].fullmatch(p) for p in parts)


def scan(companies, rx):
    """Fetch all companies in parallel. Returns (matching jobs, list of errors)."""
    jobs, errors = [], []

    def one(item):
        slug, ats = item
        try:
            return slug, FETCHERS[ats](slug), None
        except Exception as e:  # one broken feed shouldn't kill the run
            return slug, [], f"{slug} ({ats}): {e}"

    with cf.ThreadPoolExecutor(12) as ex:
        for slug, found, err in ex.map(one, companies.items()):
            if err:
                errors.append(err)
            for j in found:
                if matches(j, rx):
                    j["company"] = slug
                    jobs.append(j)
    return jobs, errors


def age(ts):
    if not ts:
        return ""
    mins = int((time.time() - ts) / 60)
    if mins < 60:
        return f"{mins}m ago"
    if mins < 48 * 60:
        return f"{mins // 60}h ago"
    return f"{mins // 1440}d ago"


def is_intern(title):
    return bool(re.search(r"intern|trainee|apprentice", title, re.I))


def format_job(j):
    tag = "🎓 INTERN" if is_intern(j["title"]) else "💼"
    q = urllib.parse.quote(f"{j['company']} software engineer")
    referral = f"https://www.linkedin.com/search/results/people/?keywords={q}"
    e = html.escape
    return (f"{tag} <b>{e(j['company'].upper())}</b> · {e(j['title'])}\n"
            f"📍 {e(j['location'] or '—')}  ⏱ {age(j['posted'])}\n"
            f"<a href=\"{e(j['url'])}\">Apply</a> · <a href=\"{e(referral)}\">Find referrers</a>")


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(re.sub(r"<[^>]+>", "", text), "\n")
        return
    body = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)


def notify(jobs):
    # Telegram caps messages at 4096 chars, so pack jobs into chunks.
    chunk = f"🚨 <b>{len(jobs)} new job(s)</b>\n\n"
    for j in jobs:
        block = format_job(j) + "\n\n"
        if len(chunk) + len(block) > 3800:
            send_telegram(chunk)
            chunk = ""
        chunk += block
    if chunk.strip():
        send_telegram(chunk)


def main():
    args = sys.argv[1:]
    companies, rx = load_config()

    if args[:1] == ["--test-alert"]:
        send_telegram("✅ Job Radar is connected. You'll get pinged here when new jobs drop.")
        return

    if args[:1] == ["--check"]:
        slug = args[1]
        for ats, fn in FETCHERS.items():
            try:
                found = fn(slug)
                good = [j for j in found if matches(j, rx)]
                print(f"{ats:16} {len(found):4} jobs, {len(good)} match your filters")
            except Exception as e:
                print(f"{ats:16} no ({type(e).__name__})")
        return

    jobs, errors = scan(companies, rx)
    for e in errors:
        print("WARN", e, file=sys.stderr)

    if args[:1] == ["--list"]:
        for j in sorted(jobs, key=lambda j: -(j["posted"] or 0)):
            print(f"{age(j['posted']):>8}  {j['company']:14} {j['title'][:60]:60}  {j['location'][:40]}")
        print(f"\n{len(jobs)} matching jobs across {len(companies)} companies")
        return

    now = time.time()
    first_run = not SEEN.exists()
    seen = {} if first_run else json.loads(SEEN.read_text())

    new = [j for j in jobs if j["id"] not in seen]
    for j in jobs:
        seen[j["id"]] = now  # refresh last-seen time for jobs still open
    seen = {k: v for k, v in seen.items() if now - v < SEEN_TTL_DAYS * 86400}
    SEEN.write_text(json.dumps(seen, indent=0, sort_keys=True))

    if first_run:
        # Don't flood you with every existing opening on day one.
        send_telegram(f"🛰 Job Radar started. Watching {len(companies)} companies; "
                      f"{len(jobs)} jobs already open match your filters (run --list to see them). "
                      f"From now on you'll only hear about new ones.")
        return

    new.sort(key=lambda j: -(j["posted"] or 0))
    if new:
        notify(new)
    print(f"{len(new)} new / {len(jobs)} matching / {len(companies)} companies / {len(errors)} errors")


if __name__ == "__main__":
    main()
