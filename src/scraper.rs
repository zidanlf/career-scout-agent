use reqwest::Client;
use scraper::{Html, Selector};
use md5;
use serde::Serialize;
use log::{info, warn, error};
use std::collections::HashSet;
use std::time::Duration;
use url::Url;
use regex::Regex;

#[derive(Debug, Clone, Serialize)]
pub struct ScrapedJob {
    pub title: String,
    pub company: String,
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

    let user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
    match client.get(url)
        .header("User-Agent", user_agent)
        .header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8")
        .header("Accept-Language", "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7")
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

// ============== PLATFORM PARSERS ==============

fn parse_linkedin(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);
    let card_selector = Selector::parse(".jobs-search__results-list li, .job-search-card, .base-card").unwrap();
    let title_selector = Selector::parse(".base-search-card__title, .job-card-list__title, h3.base-search-card__title").unwrap();
    let company_selector = Selector::parse(".base-search-card__subtitle, .job-card-container__company-name, h4.base-search-card__subtitle").unwrap();
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
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "linkedin".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

fn parse_jobstreet_html(html: &str, base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);
    let parsed_base = Url::parse(base_url).ok();
    let base_origin = parsed_base.map(|u| format!("{}://{}", u.scheme(), u.host_str().unwrap_or(""))).unwrap_or_else(|| "https://id.jobstreet.com".to_string());

    // Strategy 1: JSON-LD
    let script_selector = Selector::parse("script[type=\"application/ld+json\"]").unwrap();
    for script in fragment.select(&script_selector) {
        let json_text = script.text().collect::<Vec<_>>().join("");
        if let Ok(data) = serde_json::from_str::<serde_json::Value>(&json_text) {
            if let Some(item_type) = data.get("@type").and_then(|t| t.as_str()) {
                if item_type == "ItemList" {
                    if let Some(elements) = data.get("itemListElement").and_then(|e| e.as_array()) {
                        for item in elements {
                            let job_data = item.get("item").unwrap_or(item);
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
                            let description = job_data.get("description").and_then(|d| d.as_str()).unwrap_or("");
                            let clean_desc = Html::parse_fragment(description).root_element().text().collect::<Vec<_>>().join("").trim().to_string();
                            let truncated_desc = if clean_desc.len() > 500 { clean_desc[..500].to_string() } else { clean_desc };

                            jobs.push(ScrapedJob {
                                title,
                                company,
                                hash: generate_job_hash(&link),
                                link,
                                description: truncated_desc,
                                platform: "jobstreet".to_string(),
                            });
                        }
                    }
                } else if item_type == "JobPosting" {
                    let title = data.get("title").and_then(|t| t.as_str()).unwrap_or("").trim().to_string();
                    if is_valid_title(&title) {
                        let company = data.get("hiringOrganization").and_then(|o| o.get("name")).and_then(|n| n.as_str()).unwrap_or("Unknown Company").trim().to_string();
                        let mut link = data.get("url").and_then(|u| u.as_str()).unwrap_or("").to_string();
                        if !link.is_empty() && !link.starts_with("http") {
                            link = format!("{}{}", base_origin, link);
                        }
                        if !link.is_empty() {
                            let description = data.get("description").and_then(|d| d.as_str()).unwrap_or("");
                            let clean_desc = Html::parse_fragment(description).root_element().text().collect::<Vec<_>>().join("").trim().to_string();
                            let truncated_desc = if clean_desc.len() > 500 { clean_desc[..500].to_string() } else { clean_desc };

                            jobs.push(ScrapedJob {
                                title,
                                company,
                                hash: generate_job_hash(&link),
                                link,
                                description: truncated_desc,
                                platform: "jobstreet".to_string(),
                            });
                        }
                    }
                }
            } else if let Some(arr) = data.as_array() {
                for job_data in arr {
                    if job_data.get("@type").and_then(|t| t.as_str()) == Some("JobPosting") {
                        let title = job_data.get("title").and_then(|t| t.as_str()).unwrap_or("").trim().to_string();
                        if is_valid_title(&title) {
                            let company = job_data.get("hiringOrganization").and_then(|o| o.get("name")).and_then(|n| n.as_str()).unwrap_or("Unknown Company").trim().to_string();
                            let mut link = job_data.get("url").and_then(|u| u.as_str()).unwrap_or("").to_string();
                            if !link.is_empty() && !link.starts_with("http") {
                                link = format!("{}{}", base_origin, link);
                            }
                            if !link.is_empty() {
                                let description = job_data.get("description").and_then(|d| d.as_str()).unwrap_or("");
                                let clean_desc = Html::parse_fragment(description).root_element().text().collect::<Vec<_>>().join("").trim().to_string();
                                let truncated_desc = if clean_desc.len() > 500 { clean_desc[..500].to_string() } else { clean_desc };

                                jobs.push(ScrapedJob {
                                    title,
                                    company,
                                    hash: generate_job_hash(&link),
                                    link,
                                    description: truncated_desc,
                                    platform: "jobstreet".to_string(),
                                });
                            }
                        }
                    }
                }
            }
        }
    }

    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    // Strategy 2: Next.js __NEXT_DATA__
    let next_selector = Selector::parse("script#__NEXT_DATA__").unwrap();
    if let Some(next_script) = fragment.select(&next_selector).next() {
        let json_text = next_script.text().collect::<Vec<_>>().join("");
        if let Ok(next_data) = serde_json::from_str::<serde_json::Value>(&json_text) {
            let props = next_data.get("props").and_then(|p| p.get("pageProps")).unwrap_or(&serde_json::Value::Null);
            let search_results = props.get("searchResults")
                .or_else(|| props.get("jobs"))
                .or_else(|| props.get("data"))
                .unwrap_or(&serde_json::Value::Null);

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
                    let truncated_teaser = if teaser.len() > 500 { teaser[..500].to_string() } else { teaser.to_string() };

                    jobs.push(ScrapedJob {
                        title,
                        company,
                        hash: generate_job_hash(&link),
                        link,
                        description: truncated_teaser,
                        platform: "jobstreet".to_string(),
                    });
                }
            }
        }
    }

    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    // Strategy 3: Card Selectors
    let card_selector = Selector::parse("article[data-testid*='job-card'], [data-automation='jobListing'], a[data-automation='jobTitle'], a[href*='/id/job/'], a[href*='/job/']").unwrap();
    let title_selector = Selector::parse("[data-automation='jobTitle'], h3, h2").unwrap();
    let link_selector = Selector::parse("a[href*='/job/'], a[href*='/id/job/']").unwrap();

    let mut seen_links = HashSet::new();
    for card in fragment.select(&card_selector) {
        let (title, link) = if card.value().name() == "a" {
            let l = card.value().attr("href").unwrap_or("").to_string();
            let t = card.text().collect::<Vec<_>>().join("").trim().to_string();
            (t, l)
        } else {
            let t = card.select(&title_selector).next().map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string()).unwrap_or_default();
            let l = card.select(&link_selector).next().and_then(|el| el.value().attr("href")).unwrap_or("").to_string();
            (t, l)
        };

        if !is_valid_title(&title) {
            continue;
        }

        let mut full_link = link;
        if !full_link.is_empty() && !full_link.starts_with("http") {
            full_link = format!("{}{}", base_origin, full_link);
        }

        if full_link.is_empty() || seen_links.contains(&full_link) {
            continue;
        }
        seen_links.insert(full_link.clone());

        // Locate company
        let company = "Unknown Company".to_string();

        jobs.push(ScrapedJob {
            title,
            company,
            hash: generate_job_hash(&full_link),
            link: full_link,
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

        let location = card.select(&loc_selector)
            .next()
            .map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string())
            .unwrap_or_default();

        jobs.push(ScrapedJob {
            title,
            company,
            hash: generate_job_hash(&link),
            link,
            description: if location.is_empty() { "".to_string() } else { format!("Lokasi: {}", location) },
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
    let comp_selector = Selector::parse("span.text-secondary-500").unwrap();

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

        let company = card.select(&comp_selector)
            .next()
            .map(|el| el.text().collect::<Vec<_>>().join("").trim().to_string())
            .unwrap_or_else(|| "Unknown Company".to_string());

        jobs.push(ScrapedJob {
            title,
            company,
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

        jobs.push(ScrapedJob {
            title,
            company,
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "kitalulus".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

fn parse_kalibrr(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);

    // Strategy 1: JSON-LD
    let script_selector = Selector::parse("script[type=\"application/ld+json\"]").unwrap();
    for script in fragment.select(&script_selector) {
        let json_text = script.text().collect::<Vec<_>>().join("");
        if let Ok(data) = serde_json::from_str::<serde_json::Value>(&json_text) {
            let mut items = Vec::new();
            if let Some(item_type) = data.get("@type").and_then(|t| t.as_str()) {
                if item_type == "ItemList" {
                    if let Some(elements) = data.get("itemListElement").and_then(|e| e.as_array()) {
                        for e in elements {
                            let job_data = e.get("item").unwrap_or(e);
                            items.push(job_data.clone());
                        }
                    }
                } else if item_type == "JobPosting" {
                    items.push(data.clone());
                }
            } else if let Some(arr) = data.as_array() {
                for job_data in arr {
                    if job_data.get("@type").and_then(|t| t.as_str()) == Some("JobPosting") {
                        items.push(job_data.clone());
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
                    link = format!("https://www.kalibrr.com{}", link);
                }
                if link.is_empty() {
                    continue;
                }
                let description = job_data.get("description").and_then(|d| d.as_str()).unwrap_or("");
                let clean_desc = Html::parse_fragment(description).root_element().text().collect::<Vec<_>>().join("").trim().to_string();
                let truncated_desc = if clean_desc.len() > 500 { clean_desc[..500].to_string() } else { clean_desc };

                jobs.push(ScrapedJob {
                    title,
                    company,
                    hash: generate_job_hash(&link),
                    link,
                    description: truncated_desc,
                    platform: "kalibrr".to_string(),
                });
            }
        }
    }

    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    // Strategy 2: Direct CSS
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
                hash: generate_job_hash(&link),
                link,
                description: "".to_string(),
                platform: "kalibrr".to_string(),
            });
        }
    }

    deduplicate_by_link(jobs)
}

fn parse_glints(html: &str, _base_url: &str) -> Vec<ScrapedJob> {
    let mut jobs = Vec::new();
    let fragment = Html::parse_fragment(html);

    // Strategy 1: JSON-LD
    let script_selector = Selector::parse("script[type=\"application/ld+json\"]").unwrap();
    for script in fragment.select(&script_selector) {
        let json_text = script.text().collect::<Vec<_>>().join("");
        if let Ok(data) = serde_json::from_str::<serde_json::Value>(&json_text) {
            let mut items = Vec::new();
            if let Some(item_type) = data.get("@type").and_then(|t| t.as_str()) {
                if item_type == "ItemList" {
                    if let Some(elements) = data.get("itemListElement").and_then(|e| e.as_array()) {
                        for e in elements {
                            let job_data = e.get("item").unwrap_or(e);
                            items.push(job_data.clone());
                        }
                    }
                } else if item_type == "JobPosting" {
                    items.push(data.clone());
                }
            } else if let Some(arr) = data.as_array() {
                for job_data in arr {
                    if job_data.get("@type").and_then(|t| t.as_str()) == Some("JobPosting") {
                        items.push(job_data.clone());
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
                    link = format!("https://glints.com{}", link);
                }
                if link.is_empty() {
                    continue;
                }
                let description = job_data.get("description").and_then(|d| d.as_str()).unwrap_or("");
                let clean_desc = Html::parse_fragment(description).root_element().text().collect::<Vec<_>>().join("").trim().to_string();
                let truncated_desc = if clean_desc.len() > 500 { clean_desc[..500].to_string() } else { clean_desc };

                jobs.push(ScrapedJob {
                    title,
                    company,
                    hash: generate_job_hash(&link),
                    link,
                    description: truncated_desc,
                    platform: "glints".to_string(),
                });
            }
        }
    }

    if !jobs.is_empty() {
        return deduplicate_by_link(jobs);
    }

    // Strategy 2: Next.js __NEXT_DATA__
    let next_selector = Selector::parse("script#__NEXT_DATA__").unwrap();
    if let Some(next_script) = fragment.select(&next_selector).next() {
        let json_text = next_script.text().collect::<Vec<_>>().join("");
        if let Ok(next_data) = serde_json::from_str::<serde_json::Value>(&json_text) {
            let props = next_data.get("props").and_then(|p| p.get("pageProps")).unwrap_or(&serde_json::Value::Null);
            let initial_jobs = props.get("initialJobs").unwrap_or(&serde_json::Value::Null);
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
                let description = job_data.get("description").and_then(|d| d.as_str()).unwrap_or("");
                let clean_desc = Html::parse_fragment(description).root_element().text().collect::<Vec<_>>().join("").trim().to_string();
                let truncated_desc = if clean_desc.len() > 500 { clean_desc[..500].to_string() } else { clean_desc };

                jobs.push(ScrapedJob {
                    title,
                    company,
                    hash: generate_job_hash(&link),
                    link,
                    description: truncated_desc,
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

        jobs.push(ScrapedJob {
            title,
            company: "Unknown Company".to_string(),
            hash: generate_job_hash(&link),
            link,
            description: "".to_string(),
            platform: "unknown".to_string(),
        });
    }

    deduplicate_by_link(jobs)
}

// ============== CRAWLER EXECUTION ==============

pub async fn scrape_job_listings(url: &str) -> Vec<ScrapedJob> {
    let mut normalized_url = url.to_string();
    if url.contains("jobstreet.co.id") {
        normalized_url = url.replace("www.jobstreet.co.id", "id.jobstreet.com").replace("jobstreet.co.id", "id.jobstreet.com");
        info!("Normalized Jobstreet URL: {} -> {}", url, normalized_url);
    }

    info!("Scraping URL: {}", normalized_url);
    let platform = detect_platform(&normalized_url);
    let html = match fetch_page(&normalized_url).await {
        Some(h) => h,
        None => {
            error!("Failed to fetch page for: {}", normalized_url);
            return Vec::new();
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

        let clean_desc = Html::parse_fragment(&description).root_element().text().collect::<Vec<_>>().join("").trim().to_string();
        let truncated_desc = if clean_desc.len() > 500 { clean_desc[..500].to_string() } else { clean_desc };

        jobs.push(ScrapedJob {
            title,
            company,
            hash: generate_job_hash(&link),
            link,
            description: truncated_desc,
            platform: "rss".to_string(),
        });
    }

    Ok(deduplicate_by_link(jobs))
}
