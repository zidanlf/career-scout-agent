"""
RSS Parser for Career Scout Agent
Fetches and parses job postings from RSS-Bridge with 24-hour rolling filter.
"""

import hashlib
import httpx
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from lxml import etree
from email.utils import parsedate_to_datetime

from src.database.db_manager import check_job_processed

# Configure module logger
logger = logging.getLogger(__name__)


def _generate_job_hash(title: str, link: str) -> str:
    """Generate MD5 hash for job deduplication."""
    content = f"{title}|{link}"
    return hashlib.md5(content.encode()).hexdigest()


def _parse_pub_date(date_str: str) -> Optional[datetime]:
    """Parse RSS pubDate string to datetime."""
    try:
        # RFC 2822 format (standard RSS)
        return parsedate_to_datetime(date_str)
    except Exception:
        try:
            # Fallback: ISO format
            return datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        except Exception as e:
            logger.warning(f"Failed to parse date '{date_str}': {e}")
            return None


async def fetch_rss(url: str, timeout: float = 30.0) -> Optional[str]:
    """Fetch RSS XML content from URL."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=timeout)
            response.raise_for_status()
            logger.info(f"RSS fetched successfully from {url}")
            return response.text
    except httpx.TimeoutException:
        logger.error(f"Timeout fetching RSS from {url}")
        return None
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching RSS: {e.response.status_code}")
        return None
    except Exception as e:
        logger.error(f"Failed to fetch RSS: {e}")
        return None


def parse_jobs(xml_content: str) -> list[dict]:
    """Parse RSS XML and extract job items."""
    jobs = []
    
    try:
        root = etree.fromstring(xml_content.encode())
        
        # Handle both RSS 2.0 (<item>) and Atom (<entry>) formats
        items = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
        
        for item in items:
            # Extract fields (RSS 2.0 format)
            title_elem = item.find('title')
            link_elem = item.find('link')
            desc_elem = item.find('description')
            date_elem = item.find('pubDate')
            
            # Fallback to Atom format if needed
            if link_elem is None:
                link_elem = item.find('{http://www.w3.org/2005/Atom}link')
                if link_elem is not None:
                    link = link_elem.get('href', '')
                else:
                    link = ''
            else:
                link = link_elem.text or ''
            
            if date_elem is None:
                date_elem = item.find('{http://www.w3.org/2005/Atom}published')
            
            title = title_elem.text if title_elem is not None else 'No Title'
            description = desc_elem.text if desc_elem is not None else ''
            pub_date_str = date_elem.text if date_elem is not None else ''
            
            # Parse publication date
            published = _parse_pub_date(pub_date_str) if pub_date_str else None
            
            # Extract company name (often in title like "Job Title at Company")
            company = ''
            if ' at ' in title:
                company = title.split(' at ')[-1].strip()
            elif ' - ' in title:
                company = title.split(' - ')[-1].strip()
            
            job = {
                'title': title.strip(),
                'company': company,
                'link': link.strip(),
                'description': description.strip() if description else '',
                'published': published,
                'hash': _generate_job_hash(title, link)
            }
            jobs.append(job)
        
        logger.info(f"Parsed {len(jobs)} jobs from RSS")
        return jobs
        
    except etree.XMLSyntaxError as e:
        logger.error(f"XML parsing error: {e}")
        return []
    except Exception as e:
        logger.error(f"Failed to parse RSS: {e}")
        return []


def filter_recent_jobs(jobs: list[dict], hours: int = 24) -> list[dict]:
    """Filter jobs published within the specified hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    
    filtered = []
    for job in jobs:
        pub_date = job.get('published')
        if pub_date is None:
            # Include jobs without date (conservative approach)
            filtered.append(job)
            continue
        
        # Ensure timezone aware comparison
        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)
        
        if pub_date >= cutoff:
            filtered.append(job)
    
    logger.info(f"Filtered to {len(filtered)} jobs within {hours}h window")
    return filtered


async def get_fresh_jobs(rss_url: str, user_id: int) -> list[dict]:
    """
    Main entry point: Fetch RSS, parse, filter by 24h, and deduplicate.
    Returns list of new jobs not yet processed for this user.
    """
    # Fetch RSS content
    xml_content = await fetch_rss(rss_url)
    if not xml_content:
        return []
    
    # Parse jobs from XML
    all_jobs = parse_jobs(xml_content)
    if not all_jobs:
        return []
    
    # Filter by 24-hour window
    recent_jobs = filter_recent_jobs(all_jobs, hours=24)
    
    # Deduplicate against processed_jobs table
    new_jobs = []
    for job in recent_jobs:
        is_processed = await check_job_processed(job['hash'], user_id)
        if not is_processed:
            new_jobs.append(job)
        else:
            logger.debug(f"Skipping already processed job: {job['title'][:50]}...")
    
    logger.info(f"Found {len(new_jobs)} new jobs for user {user_id}")
    return new_jobs
