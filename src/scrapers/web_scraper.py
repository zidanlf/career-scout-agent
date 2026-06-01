"""
Web Scraper for Career Scout Agent
Scrapes job listings from job search result pages.
Supports LinkedIn, Jobstreet, Dealls, Glints, and Kalibrr.
"""

import hashlib
import json
import logging
import re
import asyncio
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode

import httpx
from bs4 import BeautifulSoup

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# Configure module logger
logger = logging.getLogger(__name__)

# HTTP headers to avoid blocking
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Titles to reject (button text, nav items, etc.)
INVALID_TITLES = {
    "view post", "view job", "apply now", "apply", "see more",
    "learn more", "sign in", "log in", "register", "view all",
    "load more", "show more", "see all jobs", "next", "previous",
}


def _generate_job_hash(link: str) -> str:
    """Generate unique hash based on link only to prevent duplicates."""
    # Normalize link: strip tracking params and trailing slashes
    clean_link = link.split("?")[0].rstrip("/").lower()
    return hashlib.md5(clean_link.encode()).hexdigest()


def _is_valid_title(title: str) -> bool:
    """Check if a title is a real job title (not button text or noise)."""
    if not title or len(title) < 3:
        return False
    if title.lower().strip() in INVALID_TITLES:
        return False
    # Reject very short generic text
    if len(title) < 5 and not any(c.isupper() for c in title):
        return False
    return True


def _detect_platform(url: str) -> str:
    """Detect job platform from URL."""
    domain = urlparse(url).netloc.lower()
    
    if "linkedin" in domain:
        return "linkedin"
    elif "jobstreet" in domain:
        return "jobstreet"
    elif "dealls" in domain:
        return "dealls"
    elif "glints" in domain:
        return "glints"
    elif "kalibrr" in domain:
        return "kalibrr"
    else:
        return "unknown"


async def _fetch_page(url: str, timeout: float = 30.0) -> Optional[str]:
    """Fetch page HTML with error handling."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers=HEADERS)
            response.raise_for_status()
            return response.text
    except httpx.TimeoutException:
        logger.warning(f"Timeout fetching URL: {url}")
        return None
    except httpx.HTTPStatusError as e:
        logger.warning(f"HTTP error {e.response.status_code} for URL: {url}")
        return None
    except Exception as e:
        logger.error(f"Error fetching URL {url}: {e}")
        return None


def _deduplicate_by_link(jobs: list[dict]) -> list[dict]:
    """
    Remove duplicate jobs based on link.
    Keeps the entry with the longest/best title.
    """
    link_map: dict[str, dict] = {}
    
    for job in jobs:
        clean_link = job["link"].split("?")[0].rstrip("/").lower()
        
        if clean_link in link_map:
            existing = link_map[clean_link]
            # Keep whichever has the longer (more descriptive) title
            if len(job["title"]) > len(existing["title"]):
                link_map[clean_link] = job
        else:
            link_map[clean_link] = job
    
    return list(link_map.values())


# ============== PLATFORM SCRAPERS ==============


def _parse_linkedin(html: str, base_url: str) -> list[dict]:
    """Parse LinkedIn job listings."""
    jobs = []
    soup = BeautifulSoup(html, "lxml")
    
    # LinkedIn job cards
    job_cards = soup.select(".jobs-search__results-list li, .job-search-card, .base-card")
    
    for card in job_cards:
        try:
            # Title - be specific to avoid picking up "View Post"
            title_elem = card.select_one(
                ".base-search-card__title, "
                ".job-card-list__title, "
                "h3.base-search-card__title"
            )
            title = title_elem.get_text(strip=True) if title_elem else None
            
            if not _is_valid_title(title):
                continue
            
            # Company
            company_elem = card.select_one(
                ".base-search-card__subtitle, "
                ".job-card-container__company-name, "
                "h4.base-search-card__subtitle"
            )
            company = company_elem.get_text(strip=True) if company_elem else "Unknown Company"
            
            # Link
            link_elem = card.select_one("a.base-card__full-link, a[href*='/jobs/']")
            link = link_elem.get("href", "") if link_elem else ""
            
            if not link:
                continue
            
            # Clean tracking params
            if "?" in link:
                link = link.split("?")[0]
            
            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": "",
                "hash": _generate_job_hash(link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing LinkedIn card: {e}")
            continue
    
    jobs = _deduplicate_by_link(jobs)
    logger.info(f"LinkedIn: Found {len(jobs)} jobs")
    return jobs


async def _scrape_jobstreet_html(url: str) -> list[dict]:
    """
    Scrape Jobstreet by fetching HTML with TLS fingerprint impersonation.
    Uses curl_cffi to impersonate Chrome's TLS fingerprint, bypassing
    Cloudflare anti-bot. Falls back to httpx if curl_cffi unavailable.
    """
    logger.info(f"Jobstreet: scraping URL: {url}")
    html = None
    
    if HAS_CURL_CFFI:
        # curl_cffi impersonates Chrome TLS fingerprint
        try:
            async with CurlAsyncSession(impersonate="chrome") as s:
                response = await s.get(url, timeout=30)
                if response.status_code == 200:
                    html = response.text
                    logger.info(f"Jobstreet: fetched via curl_cffi (status {response.status_code})")
                else:
                    logger.warning(f"Jobstreet curl_cffi: status {response.status_code}")
        except Exception as e:
            logger.warning(f"Jobstreet curl_cffi error: {e}")
    else:
        logger.warning("curl_cffi not installed, trying httpx (may get 403)")
    
    # Fallback: httpx with browser-like headers
    if not html:
        jobstreet_headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(url, headers=jobstreet_headers)
                response.raise_for_status()
                html = response.text
        except Exception as e:
            logger.warning(f"Jobstreet httpx fallback error: {e}")
    
    if not html:
        logger.warning(f"Jobstreet: all fetch methods failed for {url}")
        return []
    
    jobs = _parse_jobstreet_html(html, url)
    
    if not jobs:
        logger.warning(f"Jobstreet: no jobs parsed from {url}")
    
    return jobs


def _parse_jobstreet_html(html: str, base_url: str) -> list[dict]:
    """
    Parse Jobstreet HTML page.
    Tries JSON-LD, __NEXT_DATA__, and direct HTML card parsing.
    """
    jobs = []
    soup = BeautifulSoup(html, "lxml")
    
    # Extract base domain from URL (e.g. https://id.jobstreet.com)
    parsed_base = urlparse(base_url)
    base_origin = f"{parsed_base.scheme}://{parsed_base.netloc}"
    
    # Strategy 1: Parse JSON-LD structured data
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                for item in data.get("itemListElement", []):
                    job_data = item.get("item", item)
                    title = job_data.get("title", "")
                    if not _is_valid_title(title):
                        continue
                    
                    org = job_data.get("hiringOrganization", {})
                    company = org.get("name", "Unknown Company") if isinstance(org, dict) else "Unknown Company"
                    link = job_data.get("url", "")
                    
                    if link and not link.startswith("http"):
                        link = f"{base_origin}{link}"
                    
                    if not link:
                        continue
                    
                    description = job_data.get("description", "")[:500]
                    
                    jobs.append({
                        "title": title,
                        "company": company,
                        "link": link,
                        "description": description,
                        "hash": _generate_job_hash(link)
                    })
            elif isinstance(data, list):
                for job_data in data:
                    if job_data.get("@type") == "JobPosting":
                        title = job_data.get("title", "")
                        if not _is_valid_title(title):
                            continue
                        
                        org = job_data.get("hiringOrganization", {})
                        company = org.get("name", "Unknown Company") if isinstance(org, dict) else "Unknown Company"
                        link = job_data.get("url", "")
                        
                        if link and not link.startswith("http"):
                            link = f"{base_origin}{link}"
                        
                        if not link:
                            continue
                        
                        jobs.append({
                            "title": title,
                            "company": company,
                            "link": link,
                            "description": job_data.get("description", "")[:500],
                            "hash": _generate_job_hash(link)
                        })
        except (json.JSONDecodeError, AttributeError):
            continue
    
    if jobs:
        jobs = _deduplicate_by_link(jobs)
        logger.info(f"Jobstreet HTML (JSON-LD): Found {len(jobs)} jobs")
        return jobs
    
    # Strategy 2: Parse embedded __NEXT_DATA__ (Next.js)
    next_data_script = soup.select_one('script#__NEXT_DATA__')
    if next_data_script and next_data_script.string:
        try:
            next_data = json.loads(next_data_script.string)
            # Navigate the Next.js data structure
            props = next_data.get("props", {}).get("pageProps", {})
            search_results = props.get("searchResults", props.get("jobs", props.get("data", {})))
            
            if isinstance(search_results, dict):
                job_list = search_results.get("data", search_results.get("jobs", []))
            elif isinstance(search_results, list):
                job_list = search_results
            else:
                job_list = []
            
            for job_data in job_list:
                try:
                    title = job_data.get("title", job_data.get("jobTitle", ""))
                    if not _is_valid_title(title):
                        continue
                    
                    advertiser = job_data.get("advertiser", {})
                    company = advertiser.get("description", "Unknown Company") if isinstance(advertiser, dict) else "Unknown Company"
                    if company == "Unknown Company":
                        company = job_data.get("companyName", job_data.get("company", "Unknown Company"))
                    
                    job_id = job_data.get("id", job_data.get("jobId", ""))
                    link = job_data.get("listingUrl", job_data.get("url", ""))
                    if not link and job_id:
                        link = f"{base_origin}/id/job/{job_id}"
                    if link and not link.startswith("http"):
                        link = f"{base_origin}{link}"
                    
                    if not link:
                        continue
                    
                    jobs.append({
                        "title": title,
                        "company": company,
                        "link": link,
                        "description": job_data.get("teaser", "")[:500],
                        "hash": _generate_job_hash(link)
                    })
                except Exception as e:
                    logger.debug(f"Error parsing Jobstreet __NEXT_DATA__ job: {e}")
                    continue
        except (json.JSONDecodeError, AttributeError):
            pass
    
    if jobs:
        jobs = _deduplicate_by_link(jobs)
        logger.info(f"Jobstreet HTML (__NEXT_DATA__): Found {len(jobs)} jobs")
        return jobs
    
    # Strategy 3: Direct HTML card parsing
    job_cards = soup.select(
        "article[data-testid*='job-card'], "
        "[data-automation='jobListing'], "
        "a[data-automation='jobTitle'], "
        "a[href*='/id/job/'], "
        "a[href*='/job/']"
    )
    
    seen_links = set()
    for card in job_cards:
        try:
            if card.name == "a":
                link = card.get("href", "")
                title = card.get_text(strip=True)
            else:
                title_elem = card.select_one(
                    "[data-automation='jobTitle'], h3, h2, "
                    "a[href*='/job/']"
                )
                title = title_elem.get_text(strip=True) if title_elem else ""
                link_elem = card.select_one("a[href*='/job/']")
                link = link_elem.get("href", "") if link_elem else ""
            
            if not _is_valid_title(title):
                continue
            
            if link and not link.startswith("http"):
                link = f"{base_origin}{link}"
            
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            
            # Company
            company = "Unknown Company"
            parent = card.parent if card.name == "a" else card
            for _ in range(3):
                if parent is None:
                    break
                comp_elem = parent.select_one(
                    "[data-automation='jobCompany'], "
                    "[class*='company'], span"
                )
                if comp_elem:
                    text = comp_elem.get_text(strip=True)
                    if text and text != title and 2 < len(text) < 80:
                        company = text
                        break
                parent = parent.parent
            
            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": "",
                "hash": _generate_job_hash(link)
            })
        except Exception as e:
            logger.debug(f"Error parsing Jobstreet HTML card: {e}")
            continue
    
    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Jobstreet HTML: Found {len(jobs)} jobs")
    return jobs


def _parse_dealls(html: str, base_url: str) -> list[dict]:
    """Parse Dealls (dealls.com) job listings."""
    jobs = []
    soup = BeautifulSoup(html, "lxml")
    
    # Dealls job cards - try multiple selectors
    job_cards = soup.select(
        "a[href*='/job/'], "
        "a[href*='/jobs/'], "
        "div[class*='JobCard'], "
        "div[class*='job-card'], "
        "div[class*='jobCard']"
    )
    
    for card in job_cards:
        try:
            # Title
            title_elem = card.select_one("h2, h3, h4, [class*='title'], [class*='Title']")
            title = title_elem.get_text(strip=True) if title_elem else None
            
            # If card is a link itself, get text
            if card.name == "a" and not title:
                # Try to get the first meaningful text
                for elem in card.find_all(["h2", "h3", "h4", "span", "p"]):
                    text = elem.get_text(strip=True)
                    if _is_valid_title(text) and len(text) > 5:
                        title = text
                        break
            
            if not _is_valid_title(title):
                continue
            
            # Company
            company = "Unknown Company"
            company_selectors = [
                "[class*='company']", "[class*='Company']",
                "p", "span"
            ]
            for sel in company_selectors:
                elem = card.select_one(sel)
                if elem:
                    text = elem.get_text(strip=True)
                    if text and text != title and len(text) < 80 and _is_valid_title(text):
                        company = text
                        break
            
            # Link
            if card.name == "a":
                link = card.get("href", "")
            else:
                link_elem = card.select_one("a[href*='/job']")
                link = link_elem.get("href", "") if link_elem else ""
            
            if link and not link.startswith("http"):
                link = f"https://dealls.com{link}"
            
            if not link:
                continue
            
            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": "",
                "hash": _generate_job_hash(link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing Dealls card: {e}")
            continue
    
    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Dealls: Found {len(jobs)} jobs")
    return jobs


def _parse_kalibrr(html: str, base_url: str) -> list[dict]:
    """
    Parse Kalibrr (kalibrr.com) job listings.
    Kalibrr is SSR — content is in the HTML directly.
    Job cards: h2 > a[href*='/jobs/'] for title+link, sibling a for company.
    """
    jobs = []
    soup = BeautifulSoup(html, "lxml")

    # Strategy 1: JSON-LD
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string)
            items = []
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                items = data.get("itemListElement", [])
            elif isinstance(data, list):
                items = [d for d in data if d.get("@type") == "JobPosting"]

            for item in items:
                job_data = item.get("item", item)
                title = job_data.get("title", "")
                if not _is_valid_title(title):
                    continue
                org = job_data.get("hiringOrganization", {})
                company = org.get("name", "Unknown Company") if isinstance(org, dict) else "Unknown Company"
                link = job_data.get("url", "")
                if link and not link.startswith("http"):
                    link = f"https://www.kalibrr.com{link}"
                if not link:
                    continue
                jobs.append({
                    "title": title,
                    "company": company,
                    "link": link,
                    "description": job_data.get("description", "")[:500],
                    "hash": _generate_job_hash(link),
                })
        except (json.JSONDecodeError, AttributeError):
            continue

    if jobs:
        jobs = _deduplicate_by_link(jobs)
        logger.info(f"Kalibrr (JSON-LD): Found {len(jobs)} jobs")
        return jobs

    # Strategy 2: HTML card parsing — h2 links to job pages
    seen_links: set[str] = set()
    for heading in soup.select("h2"):
        try:
            link_elem = heading.select_one("a[href*='/jobs/']")
            if not link_elem:
                link_elem = heading.find_parent("a", href=re.compile(r"/jobs/"))
            if not link_elem:
                continue

            title = link_elem.get_text(strip=True)
            if not _is_valid_title(title):
                continue

            link = link_elem.get("href", "")
            if link and not link.startswith("http"):
                link = f"https://www.kalibrr.com{link}"
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            # Company: look for sibling span or link with company name
            company = "Unknown Company"
            parent = heading.parent
            for _ in range(3):
                if parent is None:
                    break
                # First try: span with company text (direct sibling of heading)
                for span in parent.find_all("span", recursive=False):
                    text = span.get_text(strip=True)
                    if text and text != title and 2 < len(text) < 80:
                        company = text
                        break
                if company != "Unknown Company":
                    break
                # Second try: link whose href matches /c/<slug>/jobs
                comp_link = parent.find("a", href=re.compile(r"/c/[^/]+/jobs"))
                if comp_link and comp_link != link_elem:
                    text = comp_link.get_text(strip=True)
                    if text and text != title and 2 < len(text) < 80:
                        company = text
                        break
                parent = parent.parent

            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": "",
                "hash": _generate_job_hash(link),
            })
        except Exception as e:
            logger.debug(f"Error parsing Kalibrr card: {e}")
            continue

    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Kalibrr: Found {len(jobs)} jobs")
    return jobs


async def _scrape_glints_html(url: str) -> list[dict]:
    """
    Scrape Glints by fetching HTML with TLS fingerprint impersonation.
    Glints blocks plain requests with 403 — needs curl_cffi.
    """
    logger.info(f"Glints: scraping URL: {url}")
    html = None

    if HAS_CURL_CFFI:
        try:
            async with CurlAsyncSession(impersonate="chrome") as s:
                response = await s.get(url, timeout=30)
                if response.status_code == 200:
                    html = response.text
                    logger.info(f"Glints: fetched via curl_cffi (status {response.status_code})")
                else:
                    logger.warning(f"Glints curl_cffi: status {response.status_code}")
        except Exception as e:
            logger.warning(f"Glints curl_cffi error: {e}")
    else:
        logger.warning("curl_cffi not installed, trying httpx for Glints (may get 403)")

    if not html:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(url, headers=HEADERS)
                response.raise_for_status()
                html = response.text
        except Exception as e:
            logger.warning(f"Glints httpx fallback error: {e}")

    if not html:
        logger.warning(f"Glints: all fetch methods failed for {url}")
        return []

    jobs = _parse_glints(html, url)
    if not jobs:
        logger.warning(f"Glints: no jobs parsed from {url}")
    return jobs


def _parse_glints(html: str, base_url: str) -> list[dict]:
    """
    Parse Glints HTML page.
    Tries JSON-LD first, then HTML cards with partial class matching.
    """
    jobs = []
    soup = BeautifulSoup(html, "lxml")

    # Strategy 1: JSON-LD structured data
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string)
            items = []
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                items = data.get("itemListElement", [])
            elif isinstance(data, list):
                items = [d for d in data if d.get("@type") == "JobPosting"]

            for item in items:
                job_data = item.get("item", item)
                title = job_data.get("title", "")
                if not _is_valid_title(title):
                    continue
                org = job_data.get("hiringOrganization", {})
                company = org.get("name", "Unknown Company") if isinstance(org, dict) else "Unknown Company"
                link = job_data.get("url", "")
                if link and not link.startswith("http"):
                    link = f"https://glints.com{link}"
                if not link:
                    continue
                jobs.append({
                    "title": title,
                    "company": company,
                    "link": link,
                    "description": job_data.get("description", "")[:500],
                    "hash": _generate_job_hash(link),
                })
        except (json.JSONDecodeError, AttributeError):
            continue

    if jobs:
        jobs = _deduplicate_by_link(jobs)
        logger.info(f"Glints (JSON-LD): Found {len(jobs)} jobs")
        return jobs

    # Strategy 2: __NEXT_DATA__ (Next.js) — Glints specific path
    next_data_script = soup.select_one('script#__NEXT_DATA__')
    if next_data_script and next_data_script.string:
        try:
            next_data = json.loads(next_data_script.string)
            props = next_data.get("props", {}).get("pageProps", {})
            # Glints stores jobs in initialJobs.jobsInPage
            initial_jobs = props.get("initialJobs", {})
            job_list = initial_jobs.get("jobsInPage", [])
            # Fallback: try generic paths
            if not job_list:
                for key in ("data", "opportunities", "jobs", "jobList"):
                    candidate = props.get(key)
                    if isinstance(candidate, dict):
                        candidate = candidate.get("data", candidate.get("jobs", []))
                    if isinstance(candidate, list) and candidate:
                        job_list = candidate
                        break

            for job_data in job_list:
                try:
                    title = job_data.get("title", job_data.get("name", ""))
                    if not _is_valid_title(title):
                        continue
                    company_obj = job_data.get("company", {})
                    company = company_obj.get("name", "Unknown Company") if isinstance(company_obj, dict) else "Unknown Company"
                    job_id = job_data.get("id", "")
                    link = job_data.get("url", "")
                    if not link and job_id:
                        link = f"https://glints.com/id/opportunities/jobs/{job_id}"
                    if link and not link.startswith("http"):
                        link = f"https://glints.com{link}"
                    if not link:
                        continue
                    jobs.append({
                        "title": title,
                        "company": company,
                        "link": link,
                        "description": job_data.get("description", "")[:500],
                        "hash": _generate_job_hash(link),
                    })
                except Exception:
                    continue
        except (json.JSONDecodeError, AttributeError):
            pass

    if jobs:
        jobs = _deduplicate_by_link(jobs)
        logger.info(f"Glints (__NEXT_DATA__): Found {len(jobs)} jobs")
        return jobs

    # Strategy 3: HTML card parsing — links to /opportunities/jobs/
    seen_links: set[str] = set()
    job_links = soup.select("a[href*='/opportunities/jobs/']")
    for link_elem in job_links:
        try:
            link = link_elem.get("href", "")
            # Skip navigation/filter links
            if "/explore" in link or not re.search(r"/jobs/[a-zA-Z0-9]", link):
                continue

            title = ""
            # Title from heading inside card
            title_el = link_elem.select_one("h2, h3, h4, [class*='Title'], [class*='title']")
            if title_el:
                title = title_el.get_text(strip=True)
            if not title:
                title = link_elem.get_text(strip=True)

            if not _is_valid_title(title):
                continue
            if link and not link.startswith("http"):
                link = f"https://glints.com{link}"
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            # Company: look for /companies/ link in parent hierarchy
            company = "Unknown Company"
            parent = link_elem.parent
            for _ in range(6):
                if parent is None:
                    break
                # Best signal: link to /companies/ page
                comp_link = parent.find("a", href=re.compile(r"/companies/"))
                if comp_link and comp_link != link_elem:
                    text = comp_link.get_text(strip=True)
                    if text and text != title and 2 < len(text) < 80:
                        company = text
                        break
                parent = parent.parent

            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": "",
                "hash": _generate_job_hash(link),
            })
        except Exception as e:
            logger.debug(f"Error parsing Glints card: {e}")
            continue

    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Glints: Found {len(jobs)} jobs")
    return jobs





def _parse_generic(html: str, base_url: str) -> list[dict]:
    """Generic parser for unknown job sites."""
    jobs = []
    soup = BeautifulSoup(html, "lxml")
    
    # Look for common job listing patterns
    potential_cards = soup.select("article, .job, .vacancy, .listing, li[class*='job']")
    
    for card in potential_cards:
        try:
            # Title
            title_elem = card.select_one("h2, h3, h4")
            title = title_elem.get_text(strip=True) if title_elem else None
            
            if not _is_valid_title(title):
                continue
            
            # Link
            link_elem = card.select_one("a")
            link = link_elem.get("href", "") if link_elem else ""
            
            if not link:
                continue
            
            if not link.startswith("http"):
                parsed = urlparse(base_url)
                link = f"{parsed.scheme}://{parsed.netloc}{link}"
            
            # Company
            company = "Unknown Company"
            for selector in [".company", ".employer", "span", "p"]:
                elem = card.select_one(selector)
                if elem:
                    text = elem.get_text(strip=True)
                    if text and text != title and len(text) < 100:
                        company = text
                        break
            
            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": "",
                "hash": _generate_job_hash(link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing generic card: {e}")
            continue
    
    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Generic parser: Found {len(jobs)} jobs")
    return jobs



# ============== JOB DETAIL PAGE FETCHING ==============

async def _fetch_linkedin_detail(job: dict) -> str:
    """Fetch full job description from LinkedIn detail page."""
    url = job.get("link", "")
    if not url:
        return ""
    
    try:
        html = await _fetch_page(url)
        if not html:
            return ""
        
        soup = BeautifulSoup(html, "lxml")
        
        # LinkedIn job detail selectors (public/guest view)
        desc_selectors = [
            ".show-more-less-html__markup",
            ".description__text",
            ".decorated-job-posting__details",
            "section.description",
            ".core-section-container__content",
        ]
        
        for selector in desc_selectors:
            elem = soup.select_one(selector)
            if elem:
                text = elem.get_text(separator="\n", strip=True)
                if len(text) > 50:  # Must be meaningful content
                    return text[:2000]
        
        # Fallback: try meta description
        meta = soup.select_one("meta[name='description']")
        if meta:
            content = meta.get("content", "")
            if len(content) > 50:
                return content[:2000]
        
        return ""
        
    except Exception as e:
        logger.debug(f"Error fetching LinkedIn detail for {url[:50]}: {e}")
        return ""


async def _fetch_jobstreet_detail(job: dict) -> str:
    """Fetch full job description from Jobstreet detail page."""
    url = job.get("link", "")
    if not url:
        return ""
    
    try:
        html = await _fetch_page(url)
        if not html:
            return ""
        
        soup = BeautifulSoup(html, "lxml")
        
        # Try JSON-LD first
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    desc = data.get("description", "")
                    if desc and len(desc) > 50:
                        # Strip HTML tags from description
                        desc_soup = BeautifulSoup(desc, "lxml")
                        return desc_soup.get_text(separator="\n", strip=True)[:2000]
            except (json.JSONDecodeError, AttributeError):
                continue
        
        # Try HTML selectors
        desc_selectors = [
            "[data-automation='jobDescription']",
            "[data-automation='jobAdDetails']",
            ".job-description",
            "[class*='jobDescription']",
            "section[aria-label*='description']",
        ]
        
        for selector in desc_selectors:
            elem = soup.select_one(selector)
            if elem:
                text = elem.get_text(separator="\n", strip=True)
                if len(text) > 50:
                    return text[:2000]
        
        # Fallback: meta description
        meta = soup.select_one("meta[name='description']")
        if meta:
            content = meta.get("content", "")
            if len(content) > 50:
                return content[:2000]
        
        return ""
        
    except Exception as e:
        logger.debug(f"Error fetching Jobstreet detail for {url[:50]}: {e}")
        return ""


async def _fetch_kalibrr_detail(job: dict) -> str:
    """Fetch full job description from Kalibrr detail page."""
    url = job.get("link", "")
    if not url:
        return ""
    try:
        html = await _fetch_page(url)
        if not html:
            return ""
        soup = BeautifulSoup(html, "lxml")
        # JSON-LD first
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    desc = data.get("description", "")
                    if desc and len(desc) > 50:
                        desc_soup = BeautifulSoup(desc, "lxml")
                        return desc_soup.get_text(separator="\n", strip=True)[:2000]
            except (json.JSONDecodeError, AttributeError):
                continue
        # HTML fallback
        for selector in ["[class*='description']", "[class*='Description']", "article", "main"]:
            elem = soup.select_one(selector)
            if elem:
                text = elem.get_text(separator="\n", strip=True)
                if len(text) > 50:
                    return text[:2000]
        meta = soup.select_one("meta[name='description']")
        if meta:
            content = meta.get("content", "")
            if len(content) > 50:
                return content[:2000]
        return ""
    except Exception as e:
        logger.debug(f"Error fetching Kalibrr detail for {url[:50]}: {e}")
        return ""


async def _fetch_glints_detail(job: dict) -> str:
    """Fetch full job description from Glints detail page."""
    url = job.get("link", "")
    if not url:
        return ""
    try:
        # Glints may need curl_cffi
        html = None
        if HAS_CURL_CFFI:
            try:
                async with CurlAsyncSession(impersonate="chrome") as s:
                    response = await s.get(url, timeout=30)
                    if response.status_code == 200:
                        html = response.text
            except Exception:
                pass
        if not html:
            html = await _fetch_page(url)
        if not html:
            return ""
        soup = BeautifulSoup(html, "lxml")
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    desc = data.get("description", "")
                    if desc and len(desc) > 50:
                        desc_soup = BeautifulSoup(desc, "lxml")
                        return desc_soup.get_text(separator="\n", strip=True)[:2000]
            except (json.JSONDecodeError, AttributeError):
                continue
        for selector in ["[class*='Description']", "[class*='description']", "article", "main"]:
            elem = soup.select_one(selector)
            if elem:
                text = elem.get_text(separator="\n", strip=True)
                if len(text) > 50:
                    return text[:2000]
        meta = soup.select_one("meta[name='description']")
        if meta:
            content = meta.get("content", "")
            if len(content) > 50:
                return content[:2000]
        return ""
    except Exception as e:
        logger.debug(f"Error fetching Glints detail for {url[:50]}: {e}")
        return ""



async def _enrich_jobs_with_details(jobs: list[dict], platform: str) -> list[dict]:
    """
    Enrich job listings with full descriptions from detail pages.
    Only fetches details for jobs that have empty descriptions.
    Adds 1-second delay between requests to avoid rate limiting.
    """
    detail_fetchers = {
        "linkedin": _fetch_linkedin_detail,
        "jobstreet": _fetch_jobstreet_detail,
        "kalibrr": _fetch_kalibrr_detail,
        "glints": _fetch_glints_detail,
    }
    
    fetcher = detail_fetchers.get(platform)
    if not fetcher:
        return jobs
    
    enriched_count = 0
    
    for job in jobs:
        # Skip if already has a meaningful description
        if job.get("description") and len(job["description"]) > 100:
            continue
        
        description = await fetcher(job)
        if description:
            job["description"] = description
            enriched_count += 1
            logger.debug(f"Enriched: {job['title'][:40]}... ({len(description)} chars)")
        
        # Rate limit: 1 second between requests
        await asyncio.sleep(1)
    
    logger.info(f"Enriched {enriched_count}/{len(jobs)} jobs with full descriptions ({platform})")
    return jobs


async def scrape_job_listings(url: str) -> list[dict]:
    """
    Scrape job listings from a search results page.
    After listing scrape, fetches individual detail pages for full descriptions.
    
    Args:
        url: Job search results URL (LinkedIn, Jobstreet, Dealls, Glints, Kalibrr, or other)
    
    Returns:
        List of job dicts: {title, company, link, description, hash, platform}
    """
    # Normalize Jobstreet URLs to id.jobstreet.com to prevent home-page redirection loops on old domain
    if "jobstreet.co.id" in url:
        original_url = url
        url = url.replace("www.jobstreet.co.id", "id.jobstreet.com").replace("jobstreet.co.id", "id.jobstreet.com")
        logger.info(f"Normalized Jobstreet URL: {original_url} -> {url}")

    logger.info(f"Scraping URL: {url}")
    
    # Detect platform
    platform = _detect_platform(url)
    
    if platform == "jobstreet":
        jobs = await _scrape_jobstreet_html(url)
    elif platform == "glints":
        jobs = await _scrape_glints_html(url)
    else:
        # HTML-based platforms: fetch page and parse
        html = await _fetch_page(url)
        if not html:
            logger.error(f"Failed to fetch page: {url}")
            return []
        
        if platform == "linkedin":
            jobs = _parse_linkedin(html, url)
        elif platform == "dealls":
            jobs = _parse_dealls(html, url)
        elif platform == "kalibrr":
            jobs = _parse_kalibrr(html, url)
        else:
            logger.warning(f"Unknown platform, using generic parser for: {url}")
            jobs = _parse_generic(html, url)
    
    # Enrich jobs with full descriptions from detail pages
    if jobs:
        jobs = await _enrich_jobs_with_details(jobs, platform)
    
    # Add platform to each job
    for job in jobs:
        job["platform"] = platform
    
    return jobs


async def scrape_with_deduplication(
    url: str, 
    processed_hashes: set[str]
) -> list[dict]:
    """
    Scrape jobs and filter out already processed ones.
    
    Args:
        url: Job search results URL
        processed_hashes: Set of job hashes already processed
    
    Returns:
        List of NEW job dicts only
    """
    all_jobs = await scrape_job_listings(url)
    
    new_jobs = [job for job in all_jobs if job["hash"] not in processed_hashes]
    
    logger.info(f"Found {len(all_jobs)} total, {len(new_jobs)} new jobs")
    return new_jobs
