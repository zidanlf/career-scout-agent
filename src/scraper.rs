use reqwest::Client;
use scraper::{ElementRef, Html, Selector};
use md5;
use serde::Serialize;
use serde_json::Value;
use log::{info, warn, error};
use std::collections::HashSet;
use std::time::Duration;
use url::Url;
use regex::Regex;

#[derive(Debug, Clone, Serialize)]
pub struct ScrapedJob {
    pub title: String,
    pub company: String,
    pub location: String,
    pub link: String,
    pub description: String,
    pub hash: String,
    pub platform: String,
}

const INVALID_TITLES: &[&str] = &["load more", "show more", "see all jobs", "next", "previous"];

pub fn generate_job_hash(link: &str) -> String {
    let clean_link = link.split('?').next().unwrap_or("").trim_end_matches('/').to_lowercase();
    let digest = md5::compute(clean_link.as_bytes());
    format!("{:x}", digest)
}

pub fn is_valid_title(title: &str) -> bool {
    let trimmed = title.trim();
    if trimmed.len() < 3 {
        return false;
    }
    if INVALID_TITLES.contains(&trimmed.to_lowercase().as_str()) {
        return false;
    }
    if trimmed.len() < 5 && !trimmed.chars().any(|c| c.is_uppercase()) {
        return false;
    }
    true
}

pub fn detect_platform(url: &str) -> String {
    let domain = match Url::parse(url) {
        Ok(parsed) => parsed.host_str().unwrap_or("").to_lowercase(),
        Err(_) => return "unknown".to_string(),
    };

    if domain.contains("linkedin") {
        "linkedin".to_string()
    } else if domain.contains("jobstreet") {
        "jobstreet".to_string()
    } else if domain.contains("indeed") {
        "indeed".to_string()
    } else if domain.contains("loker.id") || domain.contains("loker") {
        "loker".to_string()
    } else if domain.contains("kitalulus") {
        "kitalulus".to_string()
    } else if domain.contains("glints") {
        "glints".to_string()
    } else if domain.contains("kalibrr") {
        "kalibrr".to_string()
    } else {
        "unknown".to_string()
    }
}

fn build_reqwest_client() -> Result<Client, reqwest::Error> {
    Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
}

async fn fetch_page(url: &str) -> Option<String> {
    let client = match build_reqwest_client() {
        Ok(c) => c,
        Err(e) => {
            error!("Failed to build reqwest client: {}", e);
            return None;
        }
    };

    // Note: Accept-Encoding is intentionally not set here. reqwest adds it automatically
    // (gzip/br/deflate features) and transparently decompresses the response body.
    let user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    match client.get(url)
        .header("User-Agent", user_agent)
        .header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8")
        .header("Accept-Language", "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7")
        .header("sec-ch-ua", "\"Chromium\";v=\"124\", \"Google Chrome\";v=\"124\", \"Not-A.Brand\";v=\"99\"")
        .header("sec-ch-ua-mobile", "?0")
        .header("sec-ch-ua-platform", "\"Windows\"")
        .header("sec-fetch-dest", "document")
        .header("sec-fetch-mode", "navigate")
        .header("sec-fetch-site", "none")
        .header("sec-fetch-user", "?1")
        .header("upgrade-insecure-requests", "1")
        .send()
        .await 
    {
        Ok(resp) => {
            if resp.status().is_success() {
                match resp.text().await {
                    Ok(text) => Some(text),
                    Err(e) => {
                        warn!("Failed to read response body for {}: {}", url, e);
                        None
                    }
                }
            } else {
                warn!("HTTP error {} for {}", resp.status(), url);
                None
            }
        }
        Err(e) => {
            error!("Error fetching {}: {}", url, e);
            None
        }
    }
}

fn deduplicate_by_link(jobs: Vec<ScrapedJob>) -> Vec<ScrapedJob> {
    let mut link_map = std::collections::HashMap::new();
    for job in jobs {
        let clean_link = job.link.split('?').next().unwrap_or("").trim_end_matches('/').to_lowercase();
        if let Some(existing) = link_map.get(&clean_link) {
            let existing_job: &ScrapedJob = existing;
            if job.title.len() > existing_job.title.len() {
                link_map.insert(clean_link, job);
            }
        } else {
            link_map.insert(clean_link, job);
        }
    }
    link_map.into_values().collect()
}

// ============== EXTRACTION HELPERS ==============

/// Collect an element's text with whitespace collapsed to single spaces
fn element_text(el: &ElementRef) -> String {
    el.text().flat_map(|t| t.split_whitespace()).collect::<Vec<_>>().join(" ")
}

/// First non-empty text matched by `selector` inside `scope`
fn select_first_text(scope: &ElementRef, selector: &Selector) -> Option<String> {
    scope.select(selector).map(|el| element_text(&el)).find(|t| !t.is_empty())
}

/// All distinct non-empty texts matched by `selector` inside `scope`, joined with ", "
fn select_all_text(scope: &ElementRef, selector: &Selector) -> String {
    let mut parts: Vec<String> = Vec::new();
    for text in scope.select(selector).map(|el| element_text(&el)) {
        if !text.is_empty() && !parts.contains(&text) {
            parts.push(text);
        }
    }
    parts.join(", ")
}

/// Truncate to at most `max` characters without splitting a multi-byte UTF-8 character
fn truncate_text(text: &str, max: usize) -> String {
    match text.char_indices().nth(max) {
        Some((idx, _)) => text[..idx].to_string(),
        None => text.to_string(),
    }
}

/// Strip HTML tags from a description and truncate it to 500 characters
fn clean_description(raw: &str) -> String {
    let text = Html::parse_fragment(raw).root_element().text().collect::<Vec<_>>().join("").trim().to_string();
    truncate_text(&text, 500)
}

/// Join non-empty, distinct location parts with ", "
fn join_location_parts<'a>(parts: impl IntoIterator<Item = &'a str>) -> String {
    let mut out: Vec<&str> = Vec::new();
    for part in parts {
        let part = part.trim();
        if !part.is_empty() && !out.contains(&part) {
            out.push(part);
        }
    }
    out.join(", ")
}

/// Append a work-arrangement tag (e.g. "Remote") to a location
fn with_work_tag(location: String, tag: Option<&str>) -> String {
    match tag {
        Some(t) if location.is_empty() => t.to_string(),
        Some(t) => format!("{} ({})", location, t),
        None => location,
    }
}

/// Read a location value that is either a plain string or an object with `label`/`name`
fn json_label(value: &Value) -> Option<String> {
    value.as_str()
        .or_else(|| value.get("label").and_then(|v| v.as_str()))
        .or_else(|| value.get("name").and_then(|v| v.as_str()))
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// Read the location of a schema.org JobPosting (`jobLocation` / `jobLocationType`)
fn json_ld_location(job: &Value) -> String {
    let places: Vec<&Value> = match job.get("jobLocation") {
        Some(Value::Array(arr)) => arr.iter().collect(),
        Some(v) => vec![v],
        None => Vec::new(),
    };

    let mut locations: Vec<String> = Vec::new();
    for place in places {
        let address = place.get("address").unwrap_or(place);
        let loc = match address.as_str() {
            Some(s) => s.trim().to_string(),
            None => join_location_parts(
                ["addressLocality", "addressRegion"].iter()
                    .filter_map(|k| address.get(*k).and_then(|v| v.as_str()))
            ),
        };
        if !loc.is_empty() && !locations.contains(&loc) {
            locations.push(loc);
        }
    }

    let remote = job.get("jobLocationType").and_then(|v| v.as_str()) == Some("TELECOMMUTE");
    with_work_tag(locations.join(" | "), remote.then_some("Remote"))
}

/// Parse schema.org JobPosting entries from JSON-LD scripts
/// (supports ItemList, a single JobPosting, or an array of JobPostings)
fn parse_json_ld_jobs(fragment: &Html, base_origin: &str, platform: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let script_selector = Selector::parse("script[type=\"application/ld+json\"]").unwrap();

    for script in fragment.select(&script_selector) {
        let json_text = script.text().collect::<Vec<_>>().join("");
        let data = match serde_json::from_str::<Value>(&json_text) {
            Ok(d) => d,
            Err(_) => continue,
        };

        let mut items: Vec<&Value> = Vec::new();
        if let Some(item_type) = data.get("@type").and_then(|t| t.as_str()) {
            if item_type == "ItemList" {
                if let Some(elements) = data.get("itemListElement").and_then(|e| e.as_array()) {
                    for e in elements {
                        items.push(e.get("item").unwrap_or(e));
                    }
                }
            } else if item_type == "JobPosting" {
                items.push(&data);
            }
        } else if let Some(arr) = data.as_array() {
            for job_data in arr {
                if job_data.get("@type").and_then(|t| t.as_str()) == Some("JobPosting") {
                    items.push(job_data);
                }
            }
        }

        for job_data in items {
            let title = job_data.get("title").and_then(|t| t.as_str()).unwrap_or("").trim().to_string();
            if !is_valid_title(&title) {
                continue;
            }
            let company = job_data.get("hiringOrganization").and_then(|o| o.get("name")).and_then(|n| n.as_str()).unwrap_or("Unknown Company").trim().to_string();
            let mut link = job_data.get("url").and_then(|u| u.as_str()).unwrap_or("").to_string();
            if !link.is_empty() && !link.starts_with("http") {
                link = format!("{}{}", base_origin, link);
            }
            if link.is_empty() {
                continue;
            }
            let description = clean_description(job_data.get("description").and_then(|d| d.as_str()).unwrap_or(""));

            jobs.push(ScrapedJob {
                title,
                company,
                location: json_ld_location(job_data),
                hash: generate_job_hash(&link),
                link,
                description,
                platform: platform.to_string(),
            });
        }
    }

    jobs
}

// ============== PLATFORM PARSERS ==============

fn parse_linkedin(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);
    let card_selector = Selector::parse(".jobs-search__results-list li, .job-search-card, .base-card").unwrap();
    let title_selector = Selector::parse(".base-search-card__title, .job-card-list__title, h3.base-search-card__title").unwrap();
    let company_selector = Selector::parse(".base-search-card__subtitle, .job-card-container__company-name, h4.base-search-card__subtitle").unwrap();
    let location_selector = Selector::parse(".job-search-card__location, .job-card-container__metadata-item").unwrap();
    let link_selector = Selector::parse("a.base-card__full-link, a[href*='/jobs/']").unwrap();

    for card in fragment.select(&card_selector) {
        let title = card.select(&title_selector)
            .next()
            .map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string())
            .unwrap_or_default();

        if !is_valid_title(&title) {
            continue;
        }

        let company = card.select(&company_selector)
            .next()
            .map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string())
            .unwrap_or_else(|| "Unknown Company".to_string());

        let location = select_first_text(&card, &location_selector).unwrap_or_default();

        let link = card.select(&link_selector)
            .next()
            .and_then(|el| el.value().attr("href"))
            .map(|l| l.split('?').next().unwrap_or("").to_string())
            .unwrap_or_default();

        if link.is_empty() {
            continue;
        }

        jobs.push(ScrapedJob {
            title,
            company,
            location,
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "linkedin".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

fn parse_jobstreet_html(html: &str, base_url: &str) -> Vec<ScrapedJob> {
    let fragment = Html::parse_fragment(html);
    let parsed_base = Url::parse(base_url).ok();
    let base_origin = parsed_base.map(|u| format!("{}://{}", u.scheme(), u.host_str().unwrap_or(""))).unwrap_or_else(|| "https://id.jobstreet.com".to_string());

    // Strategy 1: JSON-LD
    let jobs = parse_json_ld_jobs(&fragment, &base_origin, "jobstreet");
    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    let mut jobs = Vec::new();

    // Strategy 2: Next.js __NEXT_DATA__
    let next_selector = Selector::parse("script#__NEXT_DATA__").unwrap();
    if let Some(next_script) = fragment.select(&next_selector).next() {
        let json_text = next_script.text().collect::<Vec<_>>().join("");
        if let Ok(next_data) = serde_json::from_str::<Value>(&json_text) {
            let props = next_data.get("props").and_then(|p| p.get("pageProps")).unwrap_or(&Value::Null);
            let search_results = props.get("searchResults")
                .or_else(|| props.get("jobs"))
                .or_else(|| props.get("data"))
                .unwrap_or(&Value::Null);

            let job_list = if search_results.is_object() {
                search_results.get("data").or_else(|| search_results.get("jobs")).and_then(|j| j.as_array())
            } else {
                search_results.as_array()
            };

            if let Some(list) = job_list {
                for job_data in list {
                    let title = job_data.get("title").or_else(|| job_data.get("jobTitle")).and_then(|t| t.as_str()).unwrap_or("").trim().to_string();
                    if !is_valid_title(&title) {
                        continue;
                    }
                    let advertiser = job_data.get("advertiser");
                    let mut company = advertiser.and_then(|a| a.get("description")).and_then(|d| d.as_str()).unwrap_or("Unknown Company").trim().to_string();
                    if company == "Unknown Company" {
                        company = job_data.get("companyName").or_else(|| job_data.get("company")).and_then(|c| c.as_str()).unwrap_or("Unknown Company").trim().to_string();
                    }

                    let location = job_data.get("location").and_then(json_label)
                        .or_else(|| job_data.get("jobLocation").and_then(json_label))
                        .or_else(|| {
                            job_data.get("locations").and_then(|l| l.as_array()).map(|arr| {
                                let labels: Vec<String> = arr.iter().filter_map(json_label).collect();
                                join_location_parts(labels.iter().map(|s| s.as_str()))
                            })
                        })
                        .unwrap_or_default();

                    let job_id = job_data.get("id").or_else(|| job_data.get("jobId")).and_then(|i| i.as_str()).unwrap_or("");
                    let mut link = job_data.get("listingUrl").or_else(|| job_data.get("url")).and_then(|u| u.as_str()).unwrap_or("").to_string();
                    if link.is_empty() && !job_id.is_empty() {
                        link = format!("{}/id/job/{}", base_origin, job_id);
                    }
                    if !link.is_empty() && !link.starts_with("http") {
                        link = format!("{}{}", base_origin, link);
                    }
                    if link.is_empty() {
                        continue;
                    }
                    let teaser = job_data.get("teaser").and_then(|t| t.as_str()).unwrap_or("");

                    jobs.push(ScrapedJob {
                        title,
                        company,
                        location,
                        hash: generate_job_hash(&link),
                        link,
                        description: truncate_text(teaser, 500),
                        platform: "jobstreet".to_string(),
                    });
                }
            }
        }
    }

    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    // Strategy 3: Job cards. Each card carries data-automation attributes for
    // title, company and location, so read them all from the same card.
    let card_selector = Selector::parse("article[data-automation='normalJob'], article[data-automation='premiumJob'], article[data-card-type='JobCard'], article[data-testid*='job-card']").unwrap();
    let title_selector = Selector::parse("[data-automation='jobTitle'], h3, h2").unwrap();
    let link_selector = Selector::parse("a[data-automation='jobTitle'], a[href*='/job/']").unwrap();
    let company_selector = Selector::parse("[data-automation='jobCompany'], [data-automation='jobCardCompanyLink'], [data-automation='advertiser-name']").unwrap();
    let location_selector = Selector::parse("[data-automation='jobLocation'], [data-automation='jobCardLocation']").unwrap();

    let mut seen_links = HashSet::new();
    for card in fragment.select(&card_selector) {
        let title = select_first_text(&card, &title_selector).unwrap_or_default();
        if !is_valid_title(&title) {
            continue;
        }

        let mut link = card.select(&link_selector).next().and_then(|el| el.value().attr("href")).unwrap_or("").to_string();
        if !link.is_empty() && !link.starts_with("http") {
            link = format!("{}{}", base_origin, link);
        }
        if link.is_empty() || !seen_links.insert(link.clone()) {
            continue;
        }

        let company = select_first_text(&card, &company_selector).unwrap_or_else(|| "Unknown Company".to_string());
        let location = select_all_text(&card, &location_selector);

        jobs.push(ScrapedJob {
            title,
            company,
            location,
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "jobstreet".to_string(),
        });
    }

    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    // Strategy 4: Bare job links (last resort, no card context for company/location)
    let anchor_selector = Selector::parse("a[data-automation='jobTitle'], a[href*='/id/job/'], a[href*='/job/']").unwrap();
    for anchor in fragment.select(&anchor_selector) {
        let title = anchor.text().collect::<Vec<_>>().join("").trim().to_string();
        if !is_valid_title(&title) {
            continue;
        }

        let mut link = anchor.value().attr("href").unwrap_or("").to_string();
        if !link.is_empty() && !link.starts_with("http") {
            link = format!("{}{}", base_origin, link);
        }
        if link.is_empty() || !seen_links.insert(link.clone()) {
            continue;
        }

        jobs.push(ScrapedJob {
            title,
            company: "Unknown Company".to_string(),
            location: "".to_string(),
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "jobstreet".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

fn parse_indeed_html(html: &str, base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);
    let card_selector = Selector::parse("div.cardOutline, div.job_seen_beacon").unwrap();
    let link_selector = Selector::parse("a.jcs-JobTitle, h3.jobTitle a").unwrap();
    let company_selector = Selector::parse("[data-testid=\"company-name\"]").unwrap();
    let loc_selector = Selector::parse("[data-testid=\"text-location\"]").unwrap();

    let base_origin = if base_url.contains("id.indeed.com") { "https://id.indeed.com" } else { "https://www.indeed.com" };

    for card in fragment.select(&card_selector) {
        let link_elem = match card.select(&link_selector).next() {
            Some(el) => el,
            None => continue,
        };

        let title = link_elem.text().collect::<Vec<_>>().join("").trim().to_string();
        if !is_valid_title(&title) {
            continue;
        }

        let mut link = link_elem.value().attr("href").unwrap_or("").to_string();
        if !link.is_empty() && !link.starts_with("http") {
            link = format!("{}{}", base_origin, link);
        }
        if link.is_empty() {
            continue;
        }

        let company = card.select(&company_selector)
            .next()
            .map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string())
            .unwrap_or_else(|| "Unknown Company".to_string());

        let location = select_first_text(&card, &loc_selector).unwrap_or_default();

        jobs.push(ScrapedJob {
            title,
            company,
            location,
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "indeed".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

fn parse_loker_id_html(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);
    let card_selector = Selector::parse("article.card").unwrap();
    let title_selector = Selector::parse("h3").unwrap();
    let link_selector = Selector::parse("a[href$=\".html\"]").unwrap();
    // Company name is the small span above the title (class changed from -500 to -600)
    let comp_selector = Selector::parse("span.text-secondary-600, span.text-secondary-500").unwrap();
    // Location is the span next to the map-pin icon
    let loc_selector = Selector::parse("span[translate='no']").unwrap();

    for card in fragment.select(&card_selector) {
        let title_elem = card.select(&title_selector).next();
        let link_elem = card.select(&link_selector).next();
        if title_elem.is_none() || link_elem.is_none() {
            continue;
        }

        let title = title_elem.unwrap().text().collect::<Vec<_>>().join("").trim().to_string();
        if !is_valid_title(&title) {
            continue;
        }

        let mut link = link_elem.unwrap().value().attr("href").unwrap_or("").to_string();
        if !link.is_empty() && !link.starts_with("http") {
            link = format!("https://www.loker.id{}", link);
        }
        if link.is_empty() {
            continue;
        }

        let company = select_first_text(&card, &comp_selector).unwrap_or_else(|| "Unknown Company".to_string());
        let location = select_first_text(&card, &loc_selector).unwrap_or_default();

        jobs.push(ScrapedJob {
            title,
            company,
            location,
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "loker".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

fn parse_kitalulus_html(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);
    let card_selector = Selector::parse("a[href*=\"/lowongan/detail/\"]").unwrap();
    let title_selector = Selector::parse("h3").unwrap();
    let comp_selector = Selector::parse("p.text-neutral-700.truncate").unwrap();
    // First detail row (next to the map-pin icon) holds the location
    let loc_selector = Selector::parse("p.text-neutral-800.text-xs").unwrap();

    for card in fragment.select(&card_selector) {
        let title_elem = match card.select(&title_selector).next() {
            Some(el) => el,
            None => continue,
        };

        let title = title_elem.text().collect::<Vec<_>>().join("").trim().to_string();
        if !is_valid_title(&title) {
            continue;
        }

        let mut link = card.value().attr("href").unwrap_or("").to_string();
        if !link.is_empty() && !link.starts_with("http") {
            link = format!("https://kerja.kitalulus.com{}", link);
        }
        if link.is_empty() {
            continue;
        }

        let company = card.select(&comp_selector)
            .next()
            .map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string())
            .unwrap_or_else(|| "Unknown Company".to_string());

        let location = select_first_text(&card, &loc_selector).unwrap_or_default();

        jobs.push(ScrapedJob {
            title,
            company,
            location,
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "kitalulus".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

fn parse_kalibrr(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let fragment = Html::parse_fragment(html);

    // Strategy 1: JSON-LD
    let jobs = parse_json_ld_jobs(&fragment, "https://www.kalibrr.com", "kalibrr");
    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    let mut jobs = Vec::new();

    // Strategy 2: Next.js __NEXT_DATA__ (props.pageProps.jobs)
    let next_selector = Selector::parse("script#__NEXT_DATA__").unwrap();
    if let Some(next_script) = fragment.select(&next_selector).next() {
        let json_text = next_script.text().collect::<Vec<_>>().join("");
        if let Ok(next_data) = serde_json::from_str::<Value>(&json_text) {
            if let Some(job_list) = next_data.pointer("/props/pageProps/jobs").and_then(|j| j.as_array()) {
                for job_data in job_list {
                    let title = job_data.get("name").or_else(|| job_data.get("title")).and_then(|t| t.as_str()).unwrap_or("").trim().to_string();
                    if !is_valid_title(&title) {
                        continue;
                    }

                    let company = job_data.get("companyName").and_then(|c| c.as_str())
                        .or_else(|| job_data.pointer("/company/name").and_then(|c| c.as_str()))
                        .unwrap_or("Unknown Company").trim().to_string();

                    // Same link format as the job cards: /id-ID/c/{company}/jobs/{id}/{slug}
                    let company_code = job_data.pointer("/company/code").and_then(|c| c.as_str()).unwrap_or("");
                    let job_id = match job_data.get("id") {
                        Some(Value::Number(n)) => n.to_string(),
                        Some(Value::String(s)) => s.clone(),
                        _ => String::new(),
                    };
                    if company_code.is_empty() || job_id.is_empty() {
                        continue;
                    }
                    let slug = job_data.get("slug").and_then(|s| s.as_str()).unwrap_or("");
                    let link = if slug.is_empty() {
                        format!("https://www.kalibrr.com/id-ID/c/{}/jobs/{}", company_code, job_id)
                    } else {
                        format!("https://www.kalibrr.com/id-ID/c/{}/jobs/{}/{}", company_code, job_id, slug)
                    };

                    let location = job_data.pointer("/googleLocation/addressComponents")
                        .map(|a| join_location_parts(["city", "region"].iter().filter_map(|k| a.get(*k).and_then(|v| v.as_str()))))
                        .unwrap_or_default();
                    let work_tag = if job_data.get("isWorkFromHome").and_then(|v| v.as_bool()).unwrap_or(false) {
                        Some("Remote")
                    } else if job_data.get("isHybrid").and_then(|v| v.as_bool()).unwrap_or(false) {
                        Some("Hybrid")
                    } else {
                        None
                    };

                    jobs.push(ScrapedJob {
                        title,
                        company,
                        location: with_work_tag(location, work_tag),
                        hash: generate_job_hash(&link),
                        link,
                        description: clean_description(job_data.get("description").and_then(|d| d.as_str()).unwrap_or("")),
                        platform: "kalibrr".to_string(),
                    });
                }
            }
        }
    }

    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    // Strategy 3: Direct CSS
    let h2_selector = Selector::parse("h2").unwrap();
    let link_selector = Selector::parse("a[href*=\"/jobs/\"]").unwrap();
    let mut seen_links = HashSet::new();

    for heading in fragment.select(&h2_selector) {
        if let Some(link_elem) = heading.select(&link_selector).next() {
            let title = link_elem.text().collect::<Vec<_>>().join("").trim().to_string();
            if !is_valid_title(&title) {
                continue;
            }

            let mut link = link_elem.value().attr("href").unwrap_or("").to_string();
            if !link.is_empty() && !link.starts_with("http") {
                link = format!("https://www.kalibrr.com{}", link);
            }

            if link.is_empty() || seen_links.contains(&link) {
                continue;
            }
            seen_links.insert(link.clone());

            jobs.push(ScrapedJob {
                title,
                company: "Unknown Company".to_string(),
                location: "".to_string(),
                hash: generate_job_hash(&link),
                link,
                description: "".to_string(),
                platform: "kalibrr".to_string(),
            });
        }
    }

    deduplicate_by_link(jobs)
}

/// Build a Glints location such as "Kebon Jeruk, Jakarta Barat (Remote)"
fn glints_location(job: &Value) -> String {
    let mut parts: Vec<&str> = Vec::new();

    if let Some(loc) = job.get("location") {
        if let Some(name) = loc.get("formattedName").or_else(|| loc.get("name")).and_then(|v| v.as_str()) {
            parts.push(name);
        }
        // For district-level locations, also add the city (level 3 parent)
        if let Some(parents) = loc.get("parents").and_then(|p| p.as_array()) {
            if let Some(city) = parents.iter().find(|p| p.get("level").and_then(|l| l.as_i64()) == Some(3)) {
                if let Some(name) = city.get("formattedName").or_else(|| city.get("name")).and_then(|v| v.as_str()) {
                    parts.push(name);
                }
            }
        }
    }

    if parts.is_empty() {
        for key in ["citySubDivision", "city", "country"] {
            if let Some(name) = job.get(key).and_then(|c| c.get("name")).and_then(|v| v.as_str()) {
                parts.push(name);
            }
        }
    }

    let work_tag = match job.get("workArrangementOption").and_then(|v| v.as_str()) {
        Some("REMOTE") => Some("Remote"),
        Some("HYBRID") => Some("Hybrid"),
        _ => None,
    };

    with_work_tag(join_location_parts(parts), work_tag)
}

fn parse_glints(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let fragment = Html::parse_fragment(html);

    // Strategy 1: JSON-LD
    let jobs = parse_json_ld_jobs(&fragment, "https://glints.com", "glints");
    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    let mut jobs = Vec::new();

    // Strategy 2: Next.js __NEXT_DATA__
    let next_selector = Selector::parse("script#__NEXT_DATA__").unwrap();
    if let Some(next_script) = fragment.select(&next_selector).next() {
        let json_text = next_script.text().collect::<Vec<_>>().join("");
        if let Ok(next_data) = serde_json::from_str::<Value>(&json_text) {
            let props = next_data.get("props").and_then(|p| p.get("pageProps")).unwrap_or(&Value::Null);
            let initial_jobs = props.get("initialJobs").unwrap_or(&Value::Null);
            let mut job_list = initial_jobs.get("jobsInPage").and_then(|j| j.as_array()).cloned().unwrap_or_default();

            if job_list.is_empty() {
                for key in &["data", "opportunities", "jobs", "jobList"] {
                    if let Some(candidate) = props.get(*key) {
                        if let Some(arr) = candidate.as_array() {
                            job_list = arr.clone();
                            break;
                        } else if let Some(arr) = candidate.get("data").or_else(|| candidate.get("jobs")).and_then(|a| a.as_array()) {
                            job_list = arr.clone();
                            break;
                        }
                    }
                }
            }

            for job_data in job_list {
                let title = job_data.get("title").or_else(|| job_data.get("name")).and_then(|t| t.as_str()).unwrap_or("").trim().to_string();
                if !is_valid_title(&title) {
                    continue;
                }
                let company_obj = job_data.get("company");
                let company = company_obj.and_then(|c| c.get("name")).and_then(|n| n.as_str()).unwrap_or("Unknown Company").trim().to_string();
                let job_id = job_data.get("id").and_then(|i| i.as_str()).unwrap_or("");
                let mut link = job_data.get("url").and_then(|u| u.as_str()).unwrap_or("").to_string();
                if link.is_empty() && !job_id.is_empty() {
                    link = format!("https://glints.com/id/opportunities/jobs/{}", job_id);
                }
                if !link.is_empty() && !link.starts_with("http") {
                    link = format!("https://glints.com{}", link);
                }
                if link.is_empty() {
                    continue;
                }

                jobs.push(ScrapedJob {
                    title,
                    company,
                    location: glints_location(&job_data),
                    hash: generate_job_hash(&link),
                    link,
                    description: clean_description(job_data.get("description").and_then(|d| d.as_str()).unwrap_or("")),
                    platform: "glints".to_string(),
                });
            }
        }
    }

    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    // Strategy 3: Card Selection
    let link_selector = Selector::parse("a[href*='/opportunities/jobs/']").unwrap();
    let mut seen_links = HashSet::new();
    let re_jobs = Regex::new(r"/jobs/[a-zA-Z0-9]").unwrap();

    for link_elem in fragment.select(&link_selector) {
        let mut link = link_elem.value().attr("href").unwrap_or("").to_string();
        if link.contains("/explore") || !re_jobs.is_match(&link) {
            continue;
        }

        let title_selector = Selector::parse("h2, h3, h4, [class*='Title'], [class*='title']").unwrap();
        let title = link_elem.select(&title_selector).next()
            .map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string())
            .unwrap_or_else(|| link_elem.text().collect::<Vec<_>>().join("").trim().to_string());

        if !is_valid_title(&title) {
            continue;
        }

        if !link.starts_with("http") {
            link = format!("https://glints.com{}", link);
        }

        if seen_links.contains(&link) {
            continue;
        }
        seen_links.insert(link.clone());

        jobs.push(ScrapedJob {
            title,
            company: "Unknown Company".to_string(),
            location: "".to_string(),
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "glints".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

fn parse_generic(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);
    let card_selector = Selector::parse("article, .job, .vacancy, .listing, li[class*='job']").unwrap();
    let title_selector = Selector::parse("h2, h3, h4").unwrap();
    let link_selector = Selector::parse("a[href*='/job/'], a[href*='/vacancy/'], a[href*='/career/']").unwrap();
    let company_selector = Selector::parse("[class*='company'], [itemprop='hiringOrganization']").unwrap();
    let location_selector = Selector::parse("[class*='location'], [itemprop='jobLocation']").unwrap();

    for card in fragment.select(&card_selector) {
        let title = card.select(&title_selector)
            .next()
            .map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string())
            .unwrap_or_default();

        if !is_valid_title(&title) {
            continue;
        }

        let link = card.select(&link_selector)
            .next()
            .and_then(|el| el.value().attr("href"))
            .unwrap_or("")
            .to_string();

        if link.is_empty() {
            continue;
        }

        let company = select_first_text(&card, &company_selector).unwrap_or_else(|| "Unknown Company".to_string());
        let location = select_first_text(&card, &location_selector).unwrap_or_default();

        jobs.push(ScrapedJob {
            title,
            company,
            location,
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "unknown".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

// ============== PROXY DETECTION ==============

/// Check if a URL needs to be routed through ScrapingAnt proxy
/// (domains blocked by Cloudflare/Akamai on datacenter IPs)
pub fn needs_proxy(url: &str) -> bool {
    let domain = match Url::parse(url) {
        Ok(parsed) => parsed.host_str().unwrap_or("").to_lowercase(),
        Err(_) => return false,
    };
    domain.contains("jobstreet") || domain.contains("glints")
}

/// Check if a URL belongs to Indeed
pub fn is_indeed(url: &str) -> bool {
    let domain = match Url::parse(url) {
        Ok(parsed) => parsed.host_str().unwrap_or("").to_lowercase(),
        Err(_) => return false,
    };
    domain.contains("indeed")
}

/// Fetch a page through ScrapingAnt API proxy
async fn fetch_page_via_proxy(url: &str, api_key: &str) -> Option<String> {
    let client = match build_reqwest_client() {
        Ok(c) => c,
        Err(e) => {
            error!("Failed to build reqwest client for proxy: {}", e);
            return None;
        }
    };

    let encoded_url = urlencoding::encode(url);
    let proxy_url = format!(
        "https://api.scrapingant.com/v2/general?url={}&x-api-key={}&browser=false",
        encoded_url, api_key
    );

    info!("Fetching via ScrapingAnt proxy: {}", url);
    match client.get(&proxy_url)
        .timeout(Duration::from_secs(60))
        .send()
        .await
    {
        Ok(resp) => {
            let status = resp.status();
            if status.is_success() {
                match resp.text().await {
                    Ok(text) => {
                        info!("ScrapingAnt proxy success for: {}", url);
                        Some(text)
                    }
                    Err(e) => {
                        warn!("Failed to read proxy response body for {}: {}", url, e);
                        None
                    }
                }
            } else {
                let body = resp.text().await.unwrap_or_default();
                warn!("ScrapingAnt proxy error {} for {}: {}", status, url, body);
                None
            }
        }
        Err(e) => {
            error!("ScrapingAnt proxy request error for {}: {}", url, e);
            None
        }
    }
}

/// Fetch a page through ZenRows API proxy (with js_render and premium_proxy to bypass Cloudflare)
async fn fetch_page_via_zenrows(url: &str, api_key: &str) -> Option<String> {
    let client = match build_reqwest_client() {
        Ok(c) => c,
        Err(e) => {
            error!("Failed to build reqwest client for ZenRows: {}", e);
            return None;
        }
    };

    let encoded_url = urlencoding::encode(url);
    let zenrows_url = format!(
        "https://api.zenrows.com/v1/?apikey={}&url={}&js_render=true&premium_proxy=true",
        api_key, encoded_url
    );

    info!("Fetching via ZenRows proxy: {}", url);
    match client.get(&zenrows_url)
        .timeout(Duration::from_secs(60))
        .send()
        .await
    {
        Ok(resp) => {
            let status = resp.status();
            if status.is_success() {
                match resp.text().await {
                    Ok(text) => {
                        info!("ZenRows proxy success for: {}", url);
                        Some(text)
                    }
                    Err(e) => {
                        warn!("Failed to read ZenRows response body for {}: {}", url, e);
                        None
                    }
                }
            } else {
                let body = resp.text().await.unwrap_or_default();
                warn!("ZenRows proxy error {} for {}: {}", status, url, body);
                None
            }
        }
        Err(e) => {
            error!("ZenRows proxy request error for {}: {}", url, e);
            None
        }
    }
}

// ============== CRAWLER EXECUTION ==============

pub async fn scrape_job_listings(url: &str, proxy_api_key: Option<&str>, zenrows_api_key: Option<&str>) -> Vec<ScrapedJob> {
    let mut normalized_url = url.to_string();
    if url.contains("jobstreet.co.id") {
        normalized_url = url.replace("www.jobstreet.co.id", "id.jobstreet.com").replace("jobstreet.co.id", "id.jobstreet.com");
        info!("Normalized Jobstreet URL: {} -> {}", url, normalized_url);
    }

    info!("Scraping URL: {}", normalized_url);
    let platform = detect_platform(&normalized_url);

    let html = if is_indeed(&normalized_url) {
        if let Some(api_key) = zenrows_api_key {
            match fetch_page_via_zenrows(&normalized_url, api_key).await {
                Some(h) => h,
                None => {
                    error!("Failed to fetch page via ZenRows for: {}", normalized_url);
                    return Vec::new();
                }
            }
        } else {
            warn!("ZenRows needed for Indeed but no ZENROWS_API_KEY configured, trying direct...");
            match fetch_page(&normalized_url).await {
                Some(h) => h,
                None => {
                    error!("Failed to fetch page for: {}", normalized_url);
                    return Vec::new();
                }
            }
        }
    } else if needs_proxy(&normalized_url) {
        if let Some(api_key) = proxy_api_key {
            match fetch_page_via_proxy(&normalized_url, api_key).await {
                Some(h) => h,
                None => {
                    error!("Failed to fetch page via proxy for: {}", normalized_url);
                    return Vec::new();
                }
            }
        } else {
            warn!("Proxy needed for {} but no SCRAPINGANT_API_KEY configured, trying direct...", normalized_url);
            match fetch_page(&normalized_url).await {
                Some(h) => h,
                None => {
                    error!("Failed to fetch page for: {}", normalized_url);
                    return Vec::new();
                }
            }
        }
    } else {
        match fetch_page(&normalized_url).await {
            Some(h) => h,
            None => {
                error!("Failed to fetch page for: {}", normalized_url);
                return Vec::new();
            }
        }
    };

    let jobs = match platform.as_str() {
        "linkedin" => parse_linkedin(&html, &normalized_url),
        "jobstreet" => parse_jobstreet_html(&html, &normalized_url),
        "indeed" => parse_indeed_html(&html, &normalized_url),
        "loker" => parse_loker_id_html(&html, &normalized_url),
        "kitalulus" => parse_kitalulus_html(&html, &normalized_url),
        "glints" => parse_glints(&html, &normalized_url),
        "kalibrr" => parse_kalibrr(&html, &normalized_url),
        _ => {
            warn!("Unknown platform, using generic parser for: {}", normalized_url);
            parse_generic(&html, &normalized_url)
        }
    };

    // A successful fetch that yields zero jobs usually means the page layout changed,
    // the site served a block/captcha page, or the response could not be decoded.
    if jobs.is_empty() {
        warn!("[{}] parsed 0 jobs from {} ({} bytes of HTML)", platform, normalized_url, html.len());
    } else {
        info!("[{}] parsed {} jobs from {}", platform, jobs.len(), normalized_url);
    }

    jobs
}

// ============== RSS PARSER ==============

pub async fn parse_rss_jobs(rss_url: &str) -> Result<Vec<ScrapedJob>, String> {
    info!("Parsing RSS URL: {}", rss_url);
    let client = build_reqwest_client().map_err(|e| e.to_string())?;
    let resp = client.get(rss_url).send().await.map_err(|e| e.to_string())?;
    
    if !resp.status().is_success() {
        return Err(format!("HTTP error status: {}", resp.status()));
    }

    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    let feed = feed_rs::parser::parse(&bytes[..]).map_err(|e| e.to_string())?;

    let mut jobs = Vec::new();
    for entry in feed.entries {
        let title = entry.title.map(|t| t.content).unwrap_or_default().trim().to_string();
        if !is_valid_title(&title) {
            continue;
        }

        let link = entry.links.first().map(|l| l.href.clone()).unwrap_or_default();
        if link.is_empty() {
            continue;
        }

        let company = entry.source
            .or_else(|| feed.title.clone().map(|t| t.content))
            .unwrap_or_else(|| "RSS Feed Source".to_string());

        let description = entry.summary
            .map(|s| s.content)
            .or_else(|| entry.content.and_then(|c| c.body))
            .unwrap_or_default();

        let truncated_desc = clean_description(&description);

        jobs.push(ScrapedJob {
            title,
            company,
            // RSS feeds have no standard location field
            location: "".to_string(),
            hash: generate_job_hash(&link),
            link,
            description: truncated_desc,
            platform: "rss".to_string(),
        });
    }

    Ok(deduplicate_by_link(jobs))
}
