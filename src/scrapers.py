"""
Scrapers for company career pages.

DESIGN NOTE — why this is fast:
Every function here calls the SAME JSON endpoint that the company's own
careers page calls when it loads. We are not crawling HTML and we are not
waiting for an aggregator to index anything. When a recruiter hits
"publish", the posting is in these API responses immediately. That is the
entire latency advantage over LinkedIn/Indeed/Simplify, which have to
discover, ingest, and batch postings on their own crawl schedule.

Each function returns list[Job]. `Job.id` must be STABLE across runs — it's
what main.py diffs against state.json to decide "is this new?". If an id
changes between runs you get duplicate alerts; if two jobs collide on an id
you silently miss one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

# A generic browser UA. Some ATS endpoints (notably Workday, behind Akamai
# bot management) reject obviously-scripted clients.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 25
MAX_RETRIES = 3
BACKOFF_BASE = 2.0


@dataclass
class Job:
    id: str
    title: str
    url: str
    company: str
    location: str = ""
    posted_at: str = ""
    description: str = ""
    flags: list[str] = field(default_factory=list)


class ScrapeError(Exception):
    """Raised when a company's board can't be read after retries."""


def _request(method: str, url: str, **kwargs) -> requests.Response:
    """
    HTTP with retry + exponential backoff.

    Backing off rather than hammering matters twice over: it's polite against
    someone else's infrastructure, and Workday will block your IP if you
    retry aggressively — which silently kills the whole tool.
    """
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE ** attempt)

    raise ScrapeError(f"{method} {url} failed after {MAX_RETRIES} attempts: {last_exc}")


def _dig(obj: Any, dotted_path: str) -> Any:
    """Walk a dotted path ('location.name') through nested dicts. None if missing."""
    if not dotted_path:
        return obj
    cur = obj
    for part in dotted_path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _strip_html(html: str) -> str:
    """Crude tag stripper — good enough for keyword matching on descriptions."""
    import html as html_mod
    import re

    text = re.sub(r"<[^>]+>", " ", html or "")
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# Greenhouse — GET, no auth, everything in one call.
# Databricks, Stripe, Cloudflare, Airbnb, Figma, Coinbase, Anthropic,
# Robinhood, Lyft, Brex, Twilio, Notion, Discord + ~8000 others.
# --------------------------------------------------------------------------
def scrape_greenhouse(company_name: str, slug: str, fetch_descriptions: bool = True) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    # content=true returns full descriptions inline — one request instead of
    # one per job. Always prefer this over per-job detail fetches.
    params = {"content": "true"} if fetch_descriptions else {}
    data = _request("GET", url, params=params).json()

    jobs = []
    for j in data.get("jobs", []):
        jobs.append(
            Job(
                id=f"greenhouse:{slug}:{j['id']}",
                title=j.get("title", ""),
                location=(j.get("location") or {}).get("name", ""),
                url=j.get("absolute_url", ""),
                company=company_name,
                posted_at=j.get("updated_at", "") or j.get("first_published", ""),
                description=_strip_html(j.get("content", "")) if fetch_descriptions else "",
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Lever — GET, no auth. Descriptions inline.
# --------------------------------------------------------------------------
def scrape_lever(company_name: str, slug: str, fetch_descriptions: bool = True) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{slug}"
    data = _request("GET", url, params={"mode": "json", "limit": 100}).json()

    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        desc = ""
        if fetch_descriptions:
            desc = _strip_html(j.get("description", "")) + " " + (j.get("additionalPlain") or "")
        jobs.append(
            Job(
                id=f"lever:{slug}:{j['id']}",
                title=j.get("text", ""),
                location=cats.get("location", ""),
                url=j.get("hostedUrl", ""),
                company=company_name,
                # Lever gives epoch MILLISECONDS. Divide by 1000 before ever
                # treating this as a real timestamp.
                posted_at=str(j.get("createdAt", "")),
                description=desc.strip(),
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Workday — the important one, and the awkward one.
#
# Three gotchas that cost people hours:
#   1. POST with a JSON body. A GET returns the HTML shell of the careers
#      page, which is why people conclude "there's no API" and reach for
#      Selenium.
#   2. You cannot guess the URL. {tenant}, the wd{N} shard, and {site} are
#      all company-specific with no derivable rule. NVIDIA is
#      wd5/NVIDIAExternalCareerSite; Salesforce wd12/External_Career_Site;
#      Adobe wd5/external_experienced. Read them off the real careers URL.
#   3. Akamai bot management — send a browser UA and a matching Referer,
#      and pace yourself.
#
# Covers NVIDIA, Salesforce, Adobe, Cisco, Intel and most large enterprises.
# --------------------------------------------------------------------------
def parse_workday_url(careers_url: str) -> tuple[str, str, str]:
    """
    Turn a careers URL into (tenant, wd_shard, site).

    https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite
        -> ("nvidia", "wd5", "NVIDIAExternalCareerSite")
    """
    parsed = urlparse(careers_url)
    if "myworkdayjobs" not in parsed.netloc:
        raise ValueError(f"Not a Workday careers URL: {careers_url}")

    host_parts = parsed.netloc.split(".")
    if len(host_parts) < 3:
        raise ValueError(f"Unexpected Workday host: {parsed.netloc}")

    tenant, shard = host_parts[0], host_parts[1]

    segments = [s for s in parsed.path.split("/") if s]
    # Drop a locale segment like "en-US" if present.
    segments = [s for s in segments if not (len(s) == 5 and s[2] == "-")]
    if not segments:
        raise ValueError(f"Could not find site name in: {careers_url}")

    return tenant, shard, segments[0]


def scrape_workday(
    company_name: str,
    careers_url: str,
    fetch_descriptions: bool = False,
    max_pages: int = 25,
) -> list[Job]:
    tenant, shard, site = parse_workday_url(careers_url)
    api = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    origin = f"https://{tenant}.{shard}.myworkdayjobs.com"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "en-US",
        "Referer": f"{origin}/en-US/{site}",
        "Origin": origin,
    }

    jobs: list[Job] = []
    limit = 20  # Workday's own UI paginates at 20; matching it looks normal.
    offset = 0

    for _ in range(max_pages):
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        data = _request("POST", api, headers=headers, json=payload).json()
        postings = data.get("jobPostings", [])
        if not postings:
            break

        for p in postings:
            external_path = p.get("externalPath", "")
            bullets = p.get("bulletFields") or []
            stable = bullets[0] if bullets else external_path
            jobs.append(
                Job(
                    id=f"workday:{tenant}:{stable}",
                    title=p.get("title", ""),
                    location=p.get("locationsText", ""),
                    url=urljoin(f"{origin}/en-US/{site}/", str(external_path).lstrip("/")),
                    company=company_name,
                    posted_at=p.get("postedOn", ""),
                )
            )

        offset += limit
        if offset >= data.get("total", 0):
            break
        time.sleep(0.5)  # polite pacing between pages

    return jobs


# --------------------------------------------------------------------------
# Ashby — GET, public posting API.
# --------------------------------------------------------------------------
def scrape_ashby(company_name: str, slug: str, fetch_descriptions: bool = True) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    data = _request("GET", url, params={"includeCompensation": "true"}).json()

    jobs = []
    for j in data.get("jobs", []):
        jobs.append(
            Job(
                id=f"ashby:{slug}:{j.get('id')}",
                title=j.get("title", ""),
                location=j.get("location", ""),
                url=j.get("jobUrl", "") or j.get("applyUrl", ""),
                company=company_name,
                posted_at=j.get("publishedAt", ""),
                description=_strip_html(j.get("descriptionHtml", "")) if fetch_descriptions else "",
            )
        )
    return jobs


# --------------------------------------------------------------------------
# SmartRecruiters — GET, public REST.
# --------------------------------------------------------------------------
def scrape_smartrecruiters(company_name: str, slug: str, **_kwargs) -> list[Job]:
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    jobs: list[Job] = []
    offset = 0

    while True:
        data = _request("GET", url, params={"limit": 100, "offset": offset}).json()
        content = data.get("content", [])
        if not content:
            break

        for j in content:
            loc = j.get("location") or {}
            jobs.append(
                Job(
                    id=f"smartrecruiters:{slug}:{j.get('id')}",
                    title=j.get("name", ""),
                    location=f"{loc.get('city', '')}, {loc.get('country', '')}".strip(", "),
                    url=j.get("ref", "") or j.get("applyUrl", ""),
                    company=company_name,
                    posted_at=j.get("releasedDate", ""),
                )
            )

        offset += 100
        if offset >= data.get("totalFound", 0):
            break
        time.sleep(0.3)

    return jobs


# --------------------------------------------------------------------------
# Custom JSON — Google/Amazon/Meta/Apple/Microsoft/Netflix run their own
# careers infrastructure. Find the endpoint via devtools (see README) and
# describe its shape in companies.yaml.
# --------------------------------------------------------------------------
def scrape_custom_json(
    company_name: str,
    url: str,
    json_path: str = "",
    fields: dict[str, str] | None = None,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict | None = None,
    url_prefix: str = "",
    **_kwargs,
) -> list[Job]:
    fields = fields or {}
    kwargs: dict[str, Any] = {"headers": headers or {}}
    if method.upper() == "POST":
        kwargs["json"] = payload or {}

    data = _request(method.upper(), url, **kwargs).json()
    items = _dig(data, json_path) or []

    jobs = []
    for item in items:
        job_id = _dig(item, fields.get("id", "id"))
        title = _dig(item, fields.get("title", "title")) or ""
        job_url = _dig(item, fields.get("url", "url")) or ""
        location = _dig(item, fields.get("location", "location")) or ""
        desc = _dig(item, fields.get("description", "")) if fields.get("description") else ""
        posted = _dig(item, fields.get("posted_at", "")) if fields.get("posted_at") else ""

        if url_prefix and job_url and not str(job_url).startswith("http"):
            job_url = urljoin(url_prefix, str(job_url).lstrip("/"))

        if job_id is None:
            job_id = f"{title}|{job_url}"

        if isinstance(location, list):
            location = ", ".join(map(str, location))

        jobs.append(
            Job(
                id=f"custom:{company_name}:{job_id}",
                title=str(title),
                url=str(job_url),
                company=company_name,
                location=str(location),
                posted_at=str(posted or ""),
                description=_strip_html(str(desc or "")),
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Playwright — genuine last resort. Slower, fragile, breaks on markup
# changes. Almost every "custom" careers site is really a JS app calling a
# JSON API, so try custom_json first.
# --------------------------------------------------------------------------
def scrape_playwright(
    company_name: str,
    url: str,
    item_selector: str,
    title_selector: str,
    link_selector: str | None = None,
    location_selector: str | None = None,
    **_kwargs,
) -> list[Job]:
    from playwright.sync_api import sync_playwright

    link_selector = link_selector or title_selector
    jobs: list[Job] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        page.goto(url, wait_until="networkidle", timeout=45000)

        for item in page.query_selector_all(item_selector):
            title_el = item.query_selector(title_selector)
            link_el = item.query_selector(link_selector) or title_el
            loc_el = item.query_selector(location_selector) if location_selector else None

            title = title_el.inner_text().strip() if title_el else ""
            href = link_el.get_attribute("href") if link_el else ""
            if href:
                href = urljoin(url, href)
            location = loc_el.inner_text().strip() if loc_el else ""

            jobs.append(
                Job(
                    id=f"playwright:{company_name}:{title}:{href}",
                    title=title,
                    url=href,
                    company=company_name,
                    location=location,
                )
            )

        browser.close()

    return jobs


SCRAPERS = {
    "greenhouse": lambda c: scrape_greenhouse(
        c["name"], c["slug"], c.get("fetch_descriptions", True)
    ),
    "lever": lambda c: scrape_lever(c["name"], c["slug"], c.get("fetch_descriptions", True)),
    "workday": lambda c: scrape_workday(
        c["name"], c["careers_url"], c.get("fetch_descriptions", False)
    ),
    "ashby": lambda c: scrape_ashby(c["name"], c["slug"], c.get("fetch_descriptions", True)),
    "smartrecruiters": lambda c: scrape_smartrecruiters(c["name"], c["slug"]),
    "custom_json": lambda c: scrape_custom_json(
        c["name"],
        c["url"],
        c.get("json_path", ""),
        c.get("fields", {}),
        c.get("http_method", "GET"),
        c.get("payload"),
        c.get("headers"),
        c.get("url_prefix", ""),
    ),
    "playwright": lambda c: scrape_playwright(
        c["name"],
        c["url"],
        c["item_selector"],
        c["title_selector"],
        c.get("link_selector"),
        c.get("location_selector"),
    ),
}
