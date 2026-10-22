"""Job Radar: polls company hiring-system feeds and pings Telegram about new SWE jobs.

Usage:
  python radar.py              # normal run: alert on jobs not seen before
  python radar.py --list       # print every job that currently matches your filters
  python radar.py --check acme # test one company slug against all hiring systems
  python radar.py --test-alert # send a test message to Telegram

Env vars (optional; without them alerts are printed to the console):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import base64
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


def fetch_json(url, tries=2):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            # retry once on rate limits and timeouts; other errors (404 etc.) fail fast
            retryable = getattr(e, "code", 429) in (429, 500, 502, 503, 504)
            if attempt == tries - 1 or not retryable:
                raise
            time.sleep(5)


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


def recruiterflow(slug):
    # No JSON feed; the careers page embeds the job list as `window.jobsList = {...};`
    req = urllib.request.Request(f"https://recruiterflow.com/{slug}/jobs", headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "replace")
    m = re.search(r"window\.jobsList\s*=\s*(\{.*?\});\s*$", page, re.M)
    if not m:
        raise ValueError("jobsList not found on page")
    out = {}
    for location, jobs in json.loads(m.group(1)).get("location", []):
        for j in jobs:
            loc = location + (" (Remote)" if j.get("remote_type") else "")
            out[j["job_id"]] = {
                "id": f"rf:{slug}:{j['job_id']}",
                "title": j["job_name"],
                "location": loc,
                "url": f"https://recruiterflow.com/{j['apply_link']}",
                "posted": iso_to_ts((j.get("last_opened") or "").replace("+0000", "+00:00")),
            }
    return list(out.values())


def mynexthire(slug):
    body = json.dumps({"source": "careers", "code": "", "filterByBuId": -1}).encode()
    req = urllib.request.Request(f"https://{slug}.mynexthire.com/employer/careers/reqlist/get",
                                 data=body, headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    out = []
    for j in d.get("reqDetailsBOList") or []:
        page = {"pageType": "jd", "cvSource": "careers", "reqId": int(j["reqId"]),
                "requester": {"id": "", "code": "", "name": ""}, "page": "careers",
                "bufilter": -1, "customFields": {}}
        p = base64.b64encode(json.dumps(page, separators=(",", ":")).encode()).decode()
        out.append({
            "id": f"mnh:{slug}:{j['reqId']}",
            "title": j["reqTitle"],
            "location": j.get("location") or "",
            # the site's own "share" link; it redirects to the job description
            "url": f"https://{slug}.mynexthire.com/employer/jobs/sociallink?src%3Dcareers%26p%3D{p}",
            "posted": iso_to_ts((j.get("approvedOn") or "").replace("+0000", "+00:00")),
            "exp_min": j.get("expMin"),
        })
    return out


def workday_posted(text):
    # Workday only gives "Posted Today" / "Posted 3 Days Ago" / "Posted 30+ Days Ago"
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)", t)
        days = int(m.group(1)) if m else None
    return None if days is None else time.time() - days * 86400


def workday(slug):
    # slug looks like "nvidia.wd5/NVIDIAExternalCareerSite". Results come newest first, so
    # the latest 60 postings always include anything posted since the previous run.
    host, site = slug.split("/")
    tenant = host.split(".")[0]
    base = f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    hdrs = {**UA, "Content-Type": "application/json", "Accept": "application/json"}
    out = []
    for offset in range(0, 60, 20):
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}).encode()
        req = urllib.request.Request(f"{base}/jobs", data=body, headers=hdrs)
        with urllib.request.urlopen(req, timeout=20) as r:
            page = json.load(r).get("jobPostings", [])
        for j in page:
            path = j.get("externalPath", "")
            job = {
                "id": f"wd:{tenant}:{(j.get('bulletFields') or [path])[0]}",
                "title": j.get("title", ""),
                "location": j.get("locationsText", ""),
                "url": f"https://{host}.myworkdayjobs.com/en-US/{site}{path}",
                "posted": workday_posted(j.get("postedOn")),
                "posted_text": (j.get("postedOn") or "").replace("Posted ", "").lower(),
            }
            if re.fullmatch(r"\d+ Locations", job["location"]):
                # Multi-location jobs need a detail call to see where they are; matches()
                # only makes it for jobs whose title already passed the filters.
                job["resolve"] = lambda path=path: _workday_locations(base, path)
            out.append(job)
        if len(page) < 20:
            break
    return out


def _workday_locations(base, path):
    info = fetch_json(f"{base}{path}").get("jobPostingInfo", {})
    return " / ".join([info.get("location", "")] + info.get("additionalLocations", []))


def amazon(slug):
    url = ("https://www.amazon.jobs/en/search.json?normalized_country_code%5B%5D=IND"
           "&sort=recent&result_limit=100&offset=0")
    out = []
    for j in fetch_json(url).get("jobs", []):
        try:
            posted = datetime.strptime(j["posted_date"], "%B %d, %Y").replace(tzinfo=timezone.utc).timestamp()
        except (KeyError, ValueError):
            posted = None
        out.append({
            "id": f"amzn:{j['id_icims']}",
            "title": j["title"],
            "location": j.get("normalized_location") or j.get("location", ""),
            "url": "https://www.amazon.jobs" + j["job_path"],
            "posted": posted,
        })
    return out


def microsoft(slug):
    out = []
    for start in range(0, 30, 10):  # newest 30; their API rate-limits fast paging
        if start:
            time.sleep(1.5)
        d = fetch_json("https://apply.careers.microsoft.com/api/pcsx/search?domain=microsoft.com"
                       f"&query=&location=India&start={start}&sort_by=timestamp")
        page = (d.get("data") or {}).get("positions") or []
        for j in page:
            loc = " / ".join(j.get("locations") or [])
            if j.get("workLocationOption") == "remote":
                loc += " (Remote)"
            out.append({
                "id": f"msft:{j['id']}",
                "title": j["name"],
                "location": loc,
                "url": f"https://apply.careers.microsoft.com/careers/job/{j['id']}",
                "posted": j.get("postedTs"),
            })
        if len(page) < 10:
            break
    return out


def atlassian(slug):
    return [{
        "id": f"atl:{j['id']}",
        "title": j["title"],
        "location": " / ".join(j.get("locations") or []),
        "url": j.get("applyUrl") or "https://www.atlassian.com/company/careers/all-jobs",
        "posted": None,
    } for j in fetch_json("https://www.atlassian.com/endpoint/careers/listings")]


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
            "recruiterflow": recruiterflow, "mynexthire": mynexthire, "workday": workday,
            "amazon": amazon, "microsoft": microsoft, "atlassian": atlassian}


def load_config():
    cfg = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    f = dict(cfg["filters"])
    max_exp = f.pop("max_experience", None)
    rx = {k: re.compile(v.strip(), re.I) for k, v in f.items()}
    rx["max_experience"] = max_exp
    # "slug" = "ats"  or  "slug" = ["ats1", "ats2"]  ->  list of (slug, ats)
    pairs = [(slug, a) for slug, ats in cfg["companies"].items()
             for a in ([ats] if isinstance(ats, str) else ats)]
    return pairs, rx


def matches(job, rx):
    title, loc = job["title"], job["location"]
    if not rx["role"].search(title) or rx["exclude"].search(title):
        return False
    if "resolve" in job:
        try:
            job["location"] = job.pop("resolve")()
        except Exception:
            return False
    # Only some hiring systems publish required experience; skip the check when they don't.
    if rx["max_experience"] is not None and job.get("exp_min") is not None             and float(job["exp_min"]) > rx["max_experience"]:
        return False
    if rx["india"].search(loc):
        return True
    if not rx["remote"].search(loc):
        return False
    parts = [p.strip() for p in re.split(r"[,/()\-–|;]+|\bor\b", loc)]
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

    with cf.ThreadPoolExecutor(24) as ex:
        for slug, found, err in ex.map(one, companies):
            if err:
                errors.append(err)
            for j in found:
                if matches(j, rx):
                    j["company"] = slug.split(".")[0].split("/")[0]
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


def when(job):
    return job.get("posted_text") or age(job["posted"])


def is_intern(title):
    return bool(re.search(r"intern|trainee|apprentice", title, re.I))


def format_job(j):
    tag = "🎓 INTERN" if is_intern(j["title"]) else "💼"
    q = urllib.parse.quote(f"{j['company']} software engineer")
    referral = f"https://www.linkedin.com/search/results/people/?keywords={q}"
    e = html.escape
    return (f"{tag} <b>{e(j['company'].upper())}</b> · {e(j['title'])}\n"
            f"📍 {e(j['location'] or '—')}  ⏱ {when(j)}\n"
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
            if ats == "mynexthire" and not re.fullmatch(r"[a-z0-9-]+", slug):
                continue
            if (ats == "workday") != ("/" in slug) or (ats in ("amazon", "microsoft", "atlassian") and ats != slug):
                continue
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
            print(f"{when(j):>10}  {j['company']:14} {j['title'][:60]:60}  {j['location'][:40]}")
        print(f"\n{len(jobs)} matching jobs across {len({s for s, _ in companies})} companies")
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
        send_telegram(f"🛰 Job Radar started. Watching {len({s for s, _ in companies})} companies; "
                      f"{len(jobs)} jobs already open match your filters (run --list to see them). "
                      f"From now on you'll only hear about new ones.")
        return

    new.sort(key=lambda j: -(j["posted"] or 0))
    if new:
        notify(new)
    print(f"{len(new)} new / {len(jobs)} matching / {len({s for s, _ in companies})} companies / {len(errors)} errors")


if __name__ == "__main__":
    main()
