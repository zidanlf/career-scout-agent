"""
AI Analyzer for Career Scout Agent
Uses OpenRouter API to analyze job-CV fit with multi-CV comparison.
Features three-tier model fallback and model tracing.
"""

import json
import httpx
import logging
import os
import re
import asyncio
from typing import Optional

# Configure module logger
logger = logging.getLogger(__name__)

# OpenRouter API endpoint
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Three-tier model priority list
MODEL_PRIORITY = [
    "openrouter/free",
    "google/gemma-3-27b-it:free",
    "google/gemma-3-12b-it:free",
]

# Retry config
MAX_RETRIES = 2
RETRY_DELAY_BASE = 5  # seconds, will be multiplied by attempt number


def _build_prompt(job_description: str, cvs: dict[str, str]) -> str:
    """Build the analysis prompt with job description and all CVs."""
    cv_section = "\n\n".join([
        f"=== CV Label: {label} ===\n{content}"
        for label, content in cvs.items()
    ])
    
    return f"""You are a strict career advisor. Analyze the job description and compare it against the provided CV(s).

CRITICAL RULES:
- ONLY mention strengths that are explicitly supported by BOTH the job description AND the CV
- ONLY mention gaps that are explicitly required in the job description but missing from the CV
- Do NOT invent or assume any skills, experience, or requirements not explicitly stated
- If the job description is too short or vague (less than 100 words), set score to 0 and add "Insufficient job description data" as the first gap

## Job Description:
{job_description}

## Available CVs:
{cv_section}

## Task:
1. Compare the job requirements against ALL provided CVs
2. Identify which CV is the BEST match
3. Calculate a match score from 0-100 based ONLY on verifiable matches
4. List key strengths (skills/experience from the CV that match specific job requirements)
5. List gaps (specific job requirements not found in the CV)

## Output Format:
Respond ONLY with valid JSON (no markdown, no explanation):
{{"best_cv": "LABEL", "score": 0-100, "justification": "brief reason", "strengths": ["strength1", "strength2"], "gaps": ["gap1", "gap2"]}}"""



def _extract_json(response_text: str) -> Optional[dict]:
    """
    Extract JSON from response with robust handling for:
    - Direct JSON
    - Markdown code blocks
    - DeepSeek's <think> tags (various formats)
    - Extra text around JSON
    """
    if not response_text:
        logger.warning("Empty response text received")
        return None
    
    logger.debug(f"Raw response length: {len(response_text)} chars")
    
    # Step 1: Remove ALL think-related tags (various formats DeepSeek might use)
    # Handles: <think>...</think>, <thinking>...</thinking>, <thought>...</thought>
    cleaned = re.sub(r'<think(?:ing)?(?:\s[^>]*)?>.*?</think(?:ing)?>', '', response_text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<thought(?:\s[^>]*)?>.*?</thought>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    
    # Also remove any remaining XML-like tags that might wrap the response
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    cleaned = cleaned.strip()
    
    logger.debug(f"Cleaned response length: {len(cleaned)} chars")
    
    # Step 2: Try direct parse
    try:
        result = json.loads(cleaned)
        logger.debug("Direct JSON parse successful")
        return result
    except json.JSONDecodeError:
        pass
    
    # Step 3: Try to extract JSON from markdown code block
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', cleaned, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group(1).strip())
            logger.debug("Markdown code block JSON parse successful")
            return result
        except json.JSONDecodeError:
            pass
    
    # Step 4: Find JSON objects - try multiple patterns
    # Pattern for nested JSON with arrays
    patterns = [
        r'\{[^{}]*"best_cv"[^{}]*"score"[^{}]*\}',  # Simple object with key fields
        r'\{(?:[^{}]|\{[^{}]*\}|\[[^\[\]]*\])*\}',   # Object with nested braces/brackets
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, cleaned, re.DOTALL)
        for match in matches:
            try:
                parsed = json.loads(match)
                if "best_cv" in parsed or "score" in parsed:
                    logger.debug(f"Pattern match JSON parse successful")
                    return parsed
            except json.JSONDecodeError:
                continue
    
    # Step 5: Last resort - find anything that looks like JSON with required keys
    # Extract from first { to last }
    first_brace = cleaned.find('{')
    last_brace = cleaned.rfind('}')
    
    if first_brace != -1 and last_brace > first_brace:
        potential_json = cleaned[first_brace:last_brace + 1]
        try:
            parsed = json.loads(potential_json)
            if isinstance(parsed, dict):
                logger.debug("Brace extraction JSON parse successful")
                return parsed
        except json.JSONDecodeError:
            pass
    
    # Log failure with sample of response for debugging
    sample = response_text[:500] if len(response_text) > 500 else response_text
    logger.error(f"Failed to extract JSON. Response sample: {sample}")
    return None


async def _call_openrouter(
    prompt: str, 
    api_key: str,
    model: str,
    timeout: float = 60.0
) -> Optional[str]:
    """Make API call to OpenRouter with specified model. Retries on 429."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/career-scout-agent",
        "X-Title": "Career Scout Agent"
    }
    
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 2000
    }
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    OPENROUTER_URL,
                    headers=headers,
                    json=payload,
                )
                
                # Handle rate limiting with retry
                if response.status_code == 429:
                    delay = RETRY_DELAY_BASE * attempt
                    logger.warning(f"Rate limited (429) on {model}, retrying in {delay}s (attempt {attempt}/{MAX_RETRIES})...")
                    await asyncio.sleep(delay)
                    continue
                
                response.raise_for_status()
                
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                if not content:
                    logger.warning(f"Empty content from {model}")
                    return None
                    
                logger.debug(f"Response from {model}: {len(content)} chars")
                return content
                
        except httpx.TimeoutException:
            logger.warning(f"Timeout with model {model} (>{timeout}s)")
            return None
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error with model {model}: {e.response.status_code}")
            return None
        except Exception as e:
            logger.warning(f"API call failed with model {model}: {e}")
            return None
    
    logger.warning(f"All {MAX_RETRIES} retries exhausted for {model}")
    return None


async def _call_with_fallback(
    prompt: str,
    api_key: str,
    timeout: float = 60.0
) -> tuple[Optional[str], Optional[str]]:
    """
    Call OpenRouter with three-tier fallback.
    Returns tuple of (response_content, model_used) or (None, None) if all fail.
    """
    for i, model in enumerate(MODEL_PRIORITY):
        logger.info(f"Trying model: {model}")
        response = await _call_openrouter(prompt, api_key, model, timeout)
        
        if response:
            # Validate that we can extract JSON before declaring success
            test_result = _extract_json(response)
            if test_result:
                logger.info(f"Successfully analyzed using {model}")
                return response, model
            else:
                logger.warning(f"Model {model} returned unparseable response, trying next...")
        else:
            logger.warning(f"Model {model} failed, trying next...")
        
        # Delay between model switches to avoid burst rate limiting
        if i < len(MODEL_PRIORITY) - 1:
            await asyncio.sleep(3)
    
    logger.error("All models failed")
    return None, None


async def analyze_job_fit(
    job_description: str, 
    cvs: dict[str, str],
    api_key: Optional[str] = None
) -> Optional[dict]:
    """
    Analyze job-CV fit using AI with three-tier model fallback.
    
    Args:
        job_description: The job posting description
        cvs: Dictionary of {label: cv_content}
        api_key: OpenRouter API key (uses env var if not provided)
    
    Returns:
        Dict with keys: best_cv, score, justification, strengths, gaps, model_used
        Or None if analysis fails
    """
    if not cvs:
        logger.warning("No CVs provided for analysis")
        return None
    
    # Get API key
    key = api_key or os.getenv("OPENROUTER_API_KEY")
    if not key:
        logger.error("OpenRouter API key not configured")
        return None
    
    # Build and send prompt
    prompt = _build_prompt(job_description, cvs)
    
    logger.info(f"Analyzing job against {len(cvs)} CV(s)...")
    
    # Call with three-tier fallback
    response, model_used = await _call_with_fallback(prompt, key, timeout=60.0)
    
    if not response:
        logger.error("No response from any AI model")
        return None
    
    # Parse response
    result = _extract_json(response)
    
    if result:
        # Validate required fields
        required = ["best_cv", "score", "justification"]
        if all(k in result for k in required):
            # Ensure score is integer
            result["score"] = int(result.get("score", 0))
            # Ensure arrays exist
            result.setdefault("strengths", [])
            result.setdefault("gaps", [])
            # Add model tracing
            result["model_used"] = model_used
            
            logger.info(f"Analysis complete: CV '{result['best_cv']}' scored {result['score']} (via {model_used})")
            return result
        else:
            logger.error(f"Missing required fields in response: {result}")
            return None
    
    return None


async def analyze_single_job(
    job: dict,
    cvs: dict[str, str],
    api_key: Optional[str] = None
) -> Optional[dict]:
    """
    Convenience function to analyze a single job dict.
    
    Args:
        job: Job dict with 'title', 'description', etc.
        cvs: Dictionary of {label: cv_content}
        api_key: Optional API key override
    
    Returns:
        Analysis result dict with model_used field, or None
    """
    # Combine title and description for better context
    job_text = f"Title: {job.get('title', '')}\n\n{job.get('description', '')}"
    return await analyze_job_fit(job_text, cvs, api_key)


# ============== BATCH ANALYSIS ==============

BATCH_SIZE = 5  # Jobs per AI call


def _build_batch_prompt(jobs: list[dict], cvs: dict[str, str]) -> str:
    """Build a prompt that asks AI to analyze multiple jobs at once."""
    cv_section = "\n\n".join([
        f"=== CV Label: {label} ===\n{content}"
        for label, content in cvs.items()
    ])
    
    jobs_section = "\n\n".join([
        f"--- JOB #{i} ---\nTitle: {job.get('title', '')}\nCompany: {job.get('company', 'Unknown')}\nDescription:\n{job.get('description', '')[:1200]}"
        for i, job in enumerate(jobs)
    ])
    
    return f"""You are a strict career advisor. Analyze EACH job below against the provided CV(s).

CRITICAL RULES:
- ONLY mention strengths explicitly supported by BOTH the job description AND the CV
- ONLY mention gaps explicitly required in the job description but missing from the CV
- Do NOT invent or assume any skills, experience, or requirements not explicitly stated
- If a job description is too short (less than 100 words), set score to 0

## Available CVs:
{cv_section}

## Jobs to Analyze:
{jobs_section}

## Task:
For EACH job above, determine the best matching CV and calculate a score from 0-100.

## Output Format:
Respond ONLY with a valid JSON ARRAY (no markdown, no explanation):
[{{"job_index": 0, "best_cv": "LABEL", "score": 0-100, "justification": "brief reason", "strengths": ["s1"], "gaps": ["g1"]}}, {{"job_index": 1, ...}}, ...]"""


def _extract_batch_json(response_text: str) -> Optional[list]:
    """Extract JSON array from batch response."""
    if not response_text:
        return None
    
    # Remove think tags
    cleaned = re.sub(r'<think(?:ing)?(?:\s[^>]*)?>.*?</think(?:ing)?>', '', response_text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<thought(?:\s[^>]*)?>.*?</thought>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    cleaned = cleaned.strip()
    
    # Try direct parse
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass
    
    # Try markdown code block
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', cleaned, re.DOTALL)
    if json_match:
        try:
            result = json.loads(json_match.group(1).strip())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
    
    # Find array brackets
    first_bracket = cleaned.find('[')
    last_bracket = cleaned.rfind(']')
    if first_bracket != -1 and last_bracket > first_bracket:
        try:
            result = json.loads(cleaned[first_bracket:last_bracket + 1])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass
    
    logger.warning(f"Failed to extract batch JSON. Sample: {response_text[:300]}")
    return None


async def analyze_batch_jobs(
    jobs: list[dict],
    cvs: dict[str, str],
    api_key: Optional[str] = None
) -> dict[int, dict]:
    """
    Analyze multiple jobs in batches of BATCH_SIZE.
    
    Args:
        jobs: List of job dicts with 'title', 'description', etc.
        cvs: Dictionary of {label: cv_content}
        api_key: Optional API key override
    
    Returns:
        Dict mapping job list index -> analysis result dict
    """
    key = api_key or os.getenv("OPENROUTER_API_KEY")
    if not key:
        logger.error("OpenRouter API key not configured")
        return {}
    
    if not cvs:
        logger.warning("No CVs provided for batch analysis")
        return {}
    
    results = {}
    total_batches = (len(jobs) + BATCH_SIZE - 1) // BATCH_SIZE
    
    for batch_num in range(total_batches):
        start = batch_num * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(jobs))
        batch = jobs[start:end]
        
        logger.info(f"Batch {batch_num + 1}/{total_batches}: analyzing {len(batch)} jobs...")
        
        prompt = _build_batch_prompt(batch, cvs)
        response, model_used = await _call_with_fallback(prompt, key, timeout=90.0)
        
        if response:
            batch_results = _extract_batch_json(response)
            
            if batch_results and isinstance(batch_results, list):
                for item in batch_results:
                    if not isinstance(item, dict):
                        continue
                    job_idx = item.get("job_index", -1)
                    if 0 <= job_idx < len(batch):
                        item["score"] = int(item.get("score", 0))
                        item.setdefault("strengths", [])
                        item.setdefault("gaps", [])
                        item["model_used"] = model_used
                        results[start + job_idx] = item
                
                logger.info(f"Batch {batch_num + 1}: parsed {len([r for r in batch_results if isinstance(r, dict)])} results (via {model_used})")
            else:
                logger.warning(f"Batch {batch_num + 1}: failed to parse, falling back to single analysis")
                # Fallback: analyze individually
                for i, job in enumerate(batch):
                    job_text = f"Title: {job.get('title', '')}\n\n{job.get('description', '')}"
                    result = await analyze_job_fit(job_text, cvs, key)
                    if result:
                        results[start + i] = result
                    await asyncio.sleep(3)
        else:
            logger.error(f"Batch {batch_num + 1}: all models failed")
        
        # Delay between batches
        if batch_num < total_batches - 1:
            await asyncio.sleep(3)
    
    logger.info(f"Batch analysis complete: {len(results)}/{len(jobs)} jobs analyzed")
    return results

