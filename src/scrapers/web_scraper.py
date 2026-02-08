"""
Web Scraper for Career Scout Agent
Scrapes job listings from job search result pages.
Supports Kalibrr, LinkedIn, and Glints.
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


def _generate_job_hash(title: str, company: str, link: str) -> str:
    """Generate unique hash for a job listing."""
    content = f"{title}|{company}|{link}"
    return hashlib.md5(content.encode()).hexdigest()


def _detect_platform(url: str) -> str:
    """Detect job platform from URL."""
    domain = urlparse(url).netloc.lower()
    
    if "kalibrr" in domain:
        return "kalibrr"
    elif "linkedin" in domain:
        return "linkedin"
    elif "glints" in domain:
        return "glints"
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


def _parse_kalibrr(html: str, base_url: str) -> list[dict]:
    """Parse Kalibrr job listings."""
    jobs = []
    soup = BeautifulSoup(html, "lxml")
    
    # Kalibrr job cards are in article elements or divs with specific classes
    job_cards = soup.select("div[data-testid='job-card'], .k-job-card, article.job-card")
    
    # Fallback: look for links containing job details
    if not job_cards:
        job_cards = soup.select("a[href*='/c/'][href*='/jobs/']")
    
    for card in job_cards:
        try:
            # Try to extract title
            title_elem = card.select_one("h2, h3, .job-title, [data-testid='job-title']")
            title = title_elem.get_text(strip=True) if title_elem else None
            
            # If card is a link itself
            if card.name == "a" and not title:
                title = card.get_text(strip=True)[:100]
            
            if not title:
                continue
            
            # Extract company
            company_elem = card.select_one(".company-name, [data-testid='company-name'], span.k-text-gray-darker")
            company = company_elem.get_text(strip=True) if company_elem else "Unknown Company"
            
            # Extract link
            link_elem = card if card.name == "a" else card.select_one("a[href*='/jobs/']")
            link = link_elem.get("href", "") if link_elem else ""
            
            if link and not link.startswith("http"):
                link = f"https://www.kalibrr.com{link}"
            
            if not link:
                continue
            
            # Extract description if available
            desc_elem = card.select_one(".job-description, p")
            description = desc_elem.get_text(strip=True)[:500] if desc_elem else ""
            
            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": description,
                "hash": _generate_job_hash(title, company, link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing Kalibrr card: {e}")
            continue
    
    logger.info(f"Kalibrr: Found {len(jobs)} jobs")
    return jobs


def _parse_linkedin(html: str, base_url: str) -> list[dict]:
    """Parse LinkedIn job listings."""
    jobs = []
    soup = BeautifulSoup(html, "lxml")
    
    # LinkedIn job cards
    job_cards = soup.select(".jobs-search__results-list li, .job-search-card, .base-card")
    
    for card in job_cards:
        try:
            # Title
            title_elem = card.select_one("h3, .base-search-card__title, .job-card-list__title")
            title = title_elem.get_text(strip=True) if title_elem else None
            
            if not title:
                continue
            
            # Company
            company_elem = card.select_one("h4, .base-search-card__subtitle, .job-card-container__company-name")
            company = company_elem.get_text(strip=True) if company_elem else "Unknown Company"
            
            # Link
            link_elem = card.select_one("a.base-card__full-link, a[href*='/jobs/']")
            link = link_elem.get("href", "") if link_elem else ""
            
            if not link:
                continue
            
            # Clean LinkedIn tracking params
            if "?" in link:
                link = link.split("?")[0]
            
            jobs.append({
                "title": title,
                "company": company,
                "link": link,
                "description": "",
                "hash": _generate_job_hash(title, company, link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing LinkedIn card: {e}")
            continue
    
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
            
            # If card is a link
            if card.name == "a" and not title:
                title = card.get_text(strip=True)[:100]
            
            if not title:
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
                "hash": _generate_job_hash(title, company, link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing Glints card: {e}")
            continue
    
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
            # Find title (usually in h2/h3)
            title_elem = card.select_one("h2, h3, h4")
            title = title_elem.get_text(strip=True) if title_elem else None
            
            if not title or len(title) < 5:
                continue
            
            # Find link
            link_elem = card.select_one("a")
            link = link_elem.get("href", "") if link_elem else ""
            
            if not link:
                continue
            
            if not link.startswith("http"):
                parsed = urlparse(base_url)
                link = f"{parsed.scheme}://{parsed.netloc}{link}"
            
            # Company (try various selectors)
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
                "hash": _generate_job_hash(title, company, link)
            })
            
        except Exception as e:
            logger.debug(f"Error parsing generic card: {e}")
            continue
    
    logger.info(f"Generic parser: Found {len(jobs)} jobs")
    return jobs


async def scrape_job_listings(url: str) -> list[dict]:
    """
    Scrape job listings from a search results page.
    
    Args:
        url: Job search results URL (Kalibrr, LinkedIn, Glints, or other)
    
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
