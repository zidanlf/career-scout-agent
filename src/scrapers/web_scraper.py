"""
Web Scraper for Career Scout Agent
Scrapes job listings from job search result pages.
Supports Kalibrr, LinkedIn, Glints, and Dealls.
"""

import hashlib
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

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


def _parse_kalibrr(html: str, base_url: str) -> list[dict]:
    """
    Parse Kalibrr job listings.
    Primary: Extract from __NEXT_DATA__ JSON (Next.js SSR data).
    Fallback: HTML selectors.
    """
    jobs = []
    soup = BeautifulSoup(html, "lxml")
    
    # === Strategy 1: Extract from __NEXT_DATA__ JSON ===
    next_data_tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if next_data_tag:
        try:
            import json
            data = json.loads(next_data_tag.string)
            
            # Navigate the Next.js data structure
            props = data.get("props", {}).get("pageProps", {})
            
            # Try multiple possible paths for job listings
            job_list = (
                props.get("jobs", []) or
                props.get("initialJobs", []) or
                props.get("data", {}).get("jobs", []) or
                props.get("searchResults", {}).get("jobs", []) or
                []
            )
            
            for job_data in job_list:
                try:
                    title = (
                        job_data.get("name") or 
                        job_data.get("title") or 
                        job_data.get("job_title", "")
                    )
                    
                    if not _is_valid_title(title):
                        continue
                    
                    # Company can be nested
                    company_data = job_data.get("company", {})
                    if isinstance(company_data, dict):
                        company = company_data.get("name", "Unknown Company")
                    elif isinstance(company_data, str):
                        company = company_data
                    else:
                        company = job_data.get("company_name", "Unknown Company")
                    
                    # Build link - prioritize direct URL from JSON
                    link = (
                        job_data.get("url") or
                        job_data.get("link") or
                        job_data.get("apply_url") or
                        ""
                    )
                    
                    # If no direct link, construct from id/slug
                    if not link:
                        job_id = job_data.get("id", "")
                        slug = job_data.get("slug", "")
                        company_slug = ""
                        if isinstance(company_data, dict):
                            company_slug = (
                                company_data.get("code") or
                                company_data.get("slug") or
                                company_data.get("company_code") or
                                ""
                            )
                        
                        # Kalibrr URL format: /c/{company_code}/jobs/{job_id}/{slug}
                        if company_slug and job_id:
                            link = f"https://www.kalibrr.com/c/{company_slug}/jobs/{job_id}"
                            if slug:
                                link += f"/{slug}"
                        elif job_id:
                            link = f"https://www.kalibrr.com/c/jobs/{job_id}"
                    
                    # Ensure full URL
                    if link and not link.startswith("http"):
                        link = f"https://www.kalibrr.com{link}"
                    
                    if not link:
                        continue
                    
                    # Log for debugging
                    logger.debug(f"Kalibrr job: {title} @ {company} -> {link}")
                    
                    description = job_data.get("description", "")[:500]
                    
                    jobs.append({
                        "title": title,
                        "company": company,
                        "link": link,
                        "description": description,
                        "hash": _generate_job_hash(link)
                    })
                    
                except Exception as e:
                    logger.debug(f"Error parsing Kalibrr JSON job: {e}")
                    continue
            
            if jobs:
                jobs = _deduplicate_by_link(jobs)
                logger.info(f"Kalibrr (JSON): Found {len(jobs)} jobs")
                return jobs
            else:
                # Log the JSON keys for debugging
                logger.warning(f"Kalibrr JSON: No jobs found. pageProps keys: {list(props.keys())}")
                if job_list:
                    logger.warning(f"Kalibrr JSON: First item keys: {list(job_list[0].keys())}")
                
        except Exception as e:
            logger.warning(f"Failed to parse Kalibrr __NEXT_DATA__: {e}")
    
    # === Strategy 2: Fallback to HTML selectors ===
    job_cards = soup.select("div[data-testid='job-card'], .k-job-card, article.job-card")
    
    if not job_cards:
        job_cards = soup.select("a[href*='/c/'][href*='/jobs/']")
    
    for card in job_cards:
        try:
            title_elem = card.select_one("h2, h3, .job-title, [data-testid='job-title']")
            title = title_elem.get_text(strip=True) if title_elem else None
            
            if card.name == "a" and not title:
                title = card.get_text(strip=True)[:100]
            
            if not _is_valid_title(title):
                continue
            
            # Try multiple selectors for company
            company = "Unknown Company"
            for sel in [".company-name", "[data-testid='company-name']", "span.k-text-gray-darker", "h4", "span"]:
                elem = card.select_one(sel)
                if elem:
                    text = elem.get_text(strip=True)
                    if text and text != title and len(text) < 80:
                        company = text
                        break
            
            link_elem = card if card.name == "a" else card.select_one("a[href*='/jobs/']")
            link = link_elem.get("href", "") if link_elem else ""
            
            if link and not link.startswith("http"):
                link = f"https://www.kalibrr.com{link}"
            
            if not link:
                continue
            
            desc_elem = card.select_one(".job-description, p")
            description = desc_elem.get_text(strip=True)[:500] if desc_elem else ""
            
            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": description,
                "hash": _generate_job_hash(link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing Kalibrr card: {e}")
            continue
    
    jobs = _deduplicate_by_link(jobs)
    logger.info(f"Kalibrr (HTML): Found {len(jobs)} jobs")
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


def _parse_glints(html: str, base_url: str) -> list[dict]:
    """Parse Glints job listings."""
    jobs = []
    soup = BeautifulSoup(html, "lxml")
    
    # Glints job cards
    job_cards = soup.select("div[data-testid='job-card'], .job-card, .JobCardsc, a[href*='/opportunities/']")
    
    for card in job_cards:
        try:
            # Title
            title_elem = card.select_one("h2, h3, .job-title, [data-testid='job-title']")
            title = title_elem.get_text(strip=True) if title_elem else None
            
            if card.name == "a" and not title:
                title = card.get_text(strip=True)[:100]
            
            if not _is_valid_title(title):
                continue
            
            # Company
            company_elem = card.select_one(".company-name, [data-testid='company-name'], span")
            company = company_elem.get_text(strip=True) if company_elem else "Unknown Company"
            
            # Link
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
            logger.debug(f"Error parsing Glints card: {e}")
            continue
    
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
    
    # Fetch page
    html = await _fetch_page(url)
    if not html:
        logger.error(f"Failed to fetch page: {url}")
        return []
    
    # Detect platform and use appropriate parser
    platform = _detect_platform(url)
    
    if platform == "kalibrr":
        jobs = _parse_kalibrr(html, url)
    elif platform == "linkedin":
        jobs = _parse_linkedin(html, url)
    elif platform == "glints":
        jobs = _parse_glints(html, url)
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
