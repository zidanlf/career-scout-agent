"""
Web Scraper for Career Scout Agent
Scrapes job listings from job search result pages.
Supports Kalibrr, LinkedIn, Glints, and Dealls.
"""

import hashlib
import json
import logging
import re
import asyncio
from typing import Optional
from urllib.parse import urlparse, parse_qs

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

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
    
    if "kalibrr" in domain:
        return "kalibrr"
    elif "linkedin" in domain:
        return "linkedin"
    elif "glints" in domain:
        return "glints"
    elif "dealls" in domain:
        return "dealls"
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


async def _scrape_kalibrr_api(url: str) -> list[dict]:
    """
    Scrape Kalibrr using their internal JSON API.
    Kalibrr is a fully client-side rendered SPA, so HTML scraping returns nothing.
    API endpoint: /kjs/job_board/search
    """
    jobs = []
    
    # Extract search keyword from URL
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")
    
    # URL format: /id-ID/home/l/Central-Jakarta/l/East-Jakarta/l/South-Jakarta/l/Bekasi/te/Data-Engineer
    # We look for the part after '/te/'
    keyword = ""
    try:
        if "/te/" in parsed.path:
            keyword = parsed.path.split("/te/")[-1].replace("-", " ")
        elif len(path_parts) >= 3:
            keyword = path_parts[2].replace("+", " ").replace("-", " ")
    except Exception:
        pass
    
    if not keyword:
        # Try query params
        params = parse_qs(parsed.query)
        keyword = params.get("keyword", params.get("q", [""]))[0]
    
    if not keyword:
        logger.warning(f"Could not extract keyword from Kalibrr URL: {url}")
        keyword = "engineer"
    
    # Use the same domain as the input URL for the API
    base_domain = f"{parsed.scheme}://{parsed.netloc}"
    api_url = f"{base_domain}/kjs/job_board/search?keyword={keyword}&limit=15&offset=0"
    
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(api_url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept": "application/json",
            })
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        logger.error(f"Kalibrr API request failed: {e}")
        return []
    
    job_list = data.get("jobs", [])
    logger.info(f"Kalibrr API returned {len(job_list)} jobs (total: {data.get('count', '?')})")
    
    for job_data in job_list:
        try:
            title = job_data.get("name", "")
            
            if not _is_valid_title(title):
                continue
            
            company = job_data.get("company_name", "Unknown Company")
            
            # Build link from company code + job id + slug
            company_data = job_data.get("company", {})
            company_code = company_data.get("code", "") if isinstance(company_data, dict) else ""
            job_id = job_data.get("id", "")
            slug = job_data.get("slug", "")
            
            if company_code and job_id:
                link = f"https://www.kalibrr.com/c/{company_code}/jobs/{job_id}/{slug}"
            elif job_id:
                link = f"https://www.kalibrr.com/c/jobs/{job_id}"
            else:
                continue
            
            # Strip HTML from description
            desc_raw = job_data.get("description", "")
            if desc_raw and "<" in desc_raw:
                desc_soup = BeautifulSoup(desc_raw, "lxml")
                description = desc_soup.get_text(strip=True)[:500]
            else:
                description = str(desc_raw)[:500]
            
            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": description,
                "hash": _generate_job_hash(link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing Kalibrr job: {e}")
            continue
    
    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Kalibrr: Found {len(jobs)} valid jobs")
    return jobs


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


async def _scrape_glints_browser(url: str) -> list[dict]:
    """
    Scrape Glints using Playwright browser automation.
    Glints blocks static requests (httpx) with a firewall.
    """
    jobs = []
    logger.info(f"Glints Browser: scraping URL: {url}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"]
        )
        page = await context.new_page()
        
        try:
            # Navigate to URL
            await page.goto(url, wait_until="networkidle", timeout=60000)
            
            # Wait for job cards to appear
            await page.wait_for_selector("div[data-testid='job-card']", timeout=60000)
            
            # Extract content
            content = await page.content()
            soup = BeautifulSoup(content, "lxml")
            
            job_cards = soup.select("div[data-testid='job-card'], .job-card, a[href*='/opportunities/']")
            
            for card in job_cards:
                try:
                    title_elem = card.select_one("h2, h3, .job-title, [data-testid='job-title']")
                    title = title_elem.get_text(strip=True) if title_elem else ""
                    
                    if not title and card.name == "a":
                        title = card.get_text(strip=True)[:100]
                    
                    if not _is_valid_title(title):
                        continue
                    
                    company = "Unknown Company"
                    for sel in ["[data-testid='company-name']", ".company-name", "span"]:
                        elem = card.select_one(sel)
                        if elem:
                            text = elem.get_text(strip=True)
                            if text and text != title and len(text) < 80:
                                company = text
                                break
                    
                    link_elem = card if card.name == "a" else card.select_one("a[href*='/opportunities/']")
                    link = link_elem.get("href", "") if link_elem else ""
                    
                    if link and not link.startswith("http"):
                        link = f"https://glints.com{link}"
                    
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
                    logger.debug(f"Error parsing Glints card in browser: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Glints Browser scraping failed: {e}")
        finally:
            await browser.close()
            
    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Glints Browser: Found {len(jobs)} jobs")
    return jobs


async def _scrape_glints_api(url: str) -> list[dict]:
    """
    Scrape Glints using their internal API.
    Glints blocks all non-browser requests with a firewall,
    but their API may be accessible with proper headers.
    """
    jobs = []
    
    # Extract keyword from URL
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    keyword = params.get("keyword", params.get("q", [""]))[0]
    
    if not keyword:
        # Try extracting from path
        path = parsed.path
        if "/explore" in path:
            keyword = "engineer"
    
    logger.info(f"Glints API: searching for '{keyword}'")
    
    # Glints uses a GraphQL-like API internally
    api_url = "https://glints.com/api/v2/opportunities"
    api_params = {
        "keyword": keyword,
        "limit": "15",
        "offset": "0",
        "country": "ID",
    }
    
    # Try multiple API strategies
    api_attempts = [
        ("v2", api_url, api_params),
        ("explore", f"https://glints.com/api/v1/opportunities", api_params),
    ]
    
    for attempt_name, api_ep, api_p in api_attempts:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(api_ep, params=api_p, headers={
                    "User-Agent": HEADERS["User-Agent"],
                    "Accept": "application/json",
                    "Referer": "https://glints.com/id/opportunities/jobs/explore",
                    "Origin": "https://glints.com",
                })
                
                if response.status_code == 403:
                    logger.warning(f"Glints API ({attempt_name}): blocked by firewall (403)")
                    continue
                
                response.raise_for_status()
                data = response.json()
                
                # Parse response
                if isinstance(data, dict):
                    job_list = data.get("data", data.get("jobs", data.get("opportunities", [])))
                elif isinstance(data, list):
                    job_list = data
                else:
                    continue
                
                for job_data in job_list:
                    try:
                        title = job_data.get("title") or job_data.get("name", "")
                        if not _is_valid_title(title):
                            continue
                        
                        company_data = job_data.get("company", {})
                        if isinstance(company_data, dict):
                            company = company_data.get("name", "Unknown Company")
                        else:
                            company = job_data.get("companyName", str(company_data) if company_data else "Unknown Company")
                        
                        link = job_data.get("url") or job_data.get("link", "")
                        if not link:
                            slug = job_data.get("slug") or job_data.get("id", "")
                            if slug:
                                link = f"https://glints.com/id/opportunities/jobs/{slug}"
                        
                        if link and not link.startswith("http"):
                            link = f"https://glints.com{link}"
                        
                        if not link:
                            continue
                        
                        description = str(job_data.get("description", ""))[:500]
                        
                        jobs.append({
                            "title": title,
                            "company": company,
                            "link": link,
                            "description": description,
                            "hash": _generate_job_hash(link)
                        })
                    except Exception as e:
                        logger.debug(f"Error parsing Glints job: {e}")
                        continue
                
                if jobs:
                    break
                    
        except Exception as e:
            logger.warning(f"Glints API ({attempt_name}) failed: {e}")
            continue
    
    if not jobs:
        logger.warning(
            "Glints: All API attempts failed. "
            "Glints blocks non-browser requests with a firewall. "
            "Consider using Selenium/Playwright for Glints scraping."
        )
    
    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Glints: Found {len(jobs)} jobs")
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


async def scrape_job_listings(url: str) -> list[dict]:
    """
    Scrape job listings from a search results page.
    
    Args:
        url: Job search results URL (Kalibrr, LinkedIn, Glints, Dealls, or other)
    
    Returns:
        List of job dicts: {title, company, link, description, hash}
    """
    logger.info(f"Scraping URL: {url}")
    
    # Detect platform
    platform = _detect_platform(url)
    
    # SPA platforms: use API or Browser for dynamic content
    if platform == "kalibrr":
        return await _scrape_kalibrr_api(url)
    elif platform == "glints":
        # Glints firewall is very aggressive, use browser
        return await _scrape_glints_browser(url)
    
    # HTML-based platforms: fetch page and parse
    html = await _fetch_page(url)
    if not html:
        logger.error(f"Failed to fetch page: {url}")
        return []
    
    if platform == "linkedin":
        jobs = _parse_linkedin(html, url)
    elif platform == "dealls":
        jobs = _parse_dealls(html, url)
    else:
        logger.warning(f"Unknown platform, using generic parser for: {url}")
        jobs = _parse_generic(html, url)
    
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
