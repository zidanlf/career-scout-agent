mod config;
mod database;
mod scraper;
mod bot;

use std::sync::Arc;
use teloxide::prelude::*;
use teloxide::types::{ParseMode, LinkPreviewOptions};
use log::{info, error};
use tokio::time::{sleep, Duration};

use crate::config::Config;
use crate::database::Database;
use crate::bot::{start_bot, format_job_notification, job_matches_filters};
use crate::scraper::{scrape_job_listings, parse_rss_jobs, needs_proxy, is_indeed};

async fn scheduled_scan_loop(bot: Bot, db: Database, config: Arc<Config>) {
    let mut scan_index = 0;
    let interval = Duration::from_secs(config.scan_interval_seconds);
    let proxy_interval = Duration::from_secs(600); // 10 minutes for Jobstreet/Glints (ScrapingAnt)
    let indeed_interval = Duration::from_secs(4 * 3600); // 4 hours for Indeed (ZenRows: ~180 scans/month < 5000 credits)
    let mut last_proxy_scans: std::collections::HashMap<i64, tokio::time::Instant> = std::collections::HashMap::new();
    let mut last_indeed_scans: std::collections::HashMap<i64, tokio::time::Instant> = std::collections::HashMap::new();

    info!("Background scheduler loop started. Direct scan every {}s, proxy scan every 600s, Indeed scan every 4h...", config.scan_interval_seconds);

    loop {
        // Run database cleanup at the start of each cycle
        if let Err(e) = db.clean_old_jobs(30).await {
            error!("Database cleanup error: {}", e);
        }

        match db.get_all_users().await {
            Ok(all_users) => {
                if all_users.is_empty() {
                    info!("No registered users, skipping scan cycle.");
                } else {
                    let active_users: Vec<_> = all_users.into_iter()
                        .filter(|u| u.active != 0)
                        .collect();

                    if active_users.is_empty() {
                        info!("No active users, skipping scan cycle.");
                    } else {
                        scan_index = scan_index % active_users.len();
                        let user = &active_users[scan_index];
                        let user_id = user.telegram_id;
                        let user_name = user.name.as_deref().unwrap_or("Unknown");
                        let keywords = db.get_user_keywords(user_id).await.unwrap_or_default();
                        let banned_keywords = db.get_user_banned_keywords(user_id).await.unwrap_or_default();

                        info!(
                            "--- Scanning for {} (ID: {}, turn: {}/{}, keywords: {:?}, banned: {:?}) ---",
                            user_name, user_id, scan_index + 1, active_users.len(), keywords, banned_keywords
                        );

                        // 1. RSS Scan
                        if let Some(rss_url) = &user.rss_url {
                            if !rss_url.trim().is_empty() {
                                match parse_rss_jobs(rss_url).await {
                                    Ok(jobs) => {
                                        let mut sent = 0;
                                        for job in jobs {
                                            if !db.check_job_processed(&job.hash, user_id).await.unwrap_or(false) {
                                                let _ = db.log_processed_job(&job.hash, user_id).await;
                                                if job_matches_filters(&job.title, &keywords, &banned_keywords) {
                                                    let text = format_job_notification(&job);
                                                    if let Err(e) = bot.send_message(ChatId(user_id), text)
                                                        .parse_mode(ParseMode::Html)
                                                        .link_preview_options(LinkPreviewOptions {
                                                            is_disabled: true,
                                                            url: None,
                                                            prefer_small_media: false,
                                                            prefer_large_media: false,
                                                            show_above_text: false,
                                                        })
                                                        .await 
                                                    {
                                                        error!("Failed to send RSS notification to {}: {}", user_id, e);
                                                    } else {
                                                        sent += 1;
                                                    }
                                                }
                                            }
                                        }
                                        info!("RSS scan done for {}: {} notified", user_name, sent);
                                    }
                                    Err(e) => {
                                        error!("RSS scan failed for {}: {}", user_name, e);
                                    }
                                }
                            }
                        }

                        // 2. Monitored URLs Scan
                        let should_scan_proxy = match last_proxy_scans.get(&user_id) {
                            None => true, // First run for this user
                            Some(last) => last.elapsed() >= proxy_interval,
                        };
                        let should_scan_indeed = match last_indeed_scans.get(&user_id) {
                            None => true, // First run for this user
                            Some(last) => last.elapsed() >= indeed_interval,
                        };
                        let proxy_key = config.scrapingant_api_key.as_deref();
                        let zenrows_key = config.zenrows_api_key.as_deref();
                        match db.get_monitored_urls(user_id).await {
                            Ok(urls) => {
                                if !urls.is_empty() {
                                    let mut sent = 0;
                                    let mut scanned_proxy = false;
                                    let mut scanned_indeed = false;
                                    for url_data in urls {
                                        // Skip Indeed if not 4 hours yet for this user
                                        if is_indeed(&url_data.url) && !should_scan_indeed {
                                            continue;
                                        }
                                        // Skip ScrapingAnt URLs if not 10 min yet for this user
                                        if needs_proxy(&url_data.url) && !should_scan_proxy {
                                            continue;
                                        }
                                        match scrape_job_listings(&url_data.url, proxy_key, zenrows_key).await {
                                            jobs => {
                                                if is_indeed(&url_data.url) {
                                                    scanned_indeed = true;
                                                }
                                                if needs_proxy(&url_data.url) {
                                                    scanned_proxy = true;
                                                }
                                                for job in jobs {
                                                    if !db.check_job_processed(&job.hash, user_id).await.unwrap_or(false) {
                                                        let _ = db.log_processed_job(&job.hash, user_id).await;
                                                        if job_matches_filters(&job.title, &keywords, &banned_keywords) {
                                                            let text = format_job_notification(&job);
                                                            if let Err(e) = bot.send_message(ChatId(user_id), text)
                                                                .parse_mode(ParseMode::Html)
                                                                .link_preview_options(LinkPreviewOptions {
                                                                    is_disabled: true,
                                                                    url: None,
                                                                    prefer_small_media: false,
                                                                    prefer_large_media: false,
                                                                    show_above_text: false,
                                                                })
                                                                .await 
                                                            {
                                                                error!("Failed to send URL notification to {}: {}", user_id, e);
                                                            } else {
                                                                sent += 1;
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                        // Sleep to prevent aggressive scraping
                                        sleep(Duration::from_secs(2)).await;
                                    }
                                    if scanned_indeed {
                                        last_indeed_scans.insert(user_id, tokio::time::Instant::now());
                                    }
                                    if scanned_proxy {
                                        last_proxy_scans.insert(user_id, tokio::time::Instant::now());
                                    }
                                    info!("Monitored URLs scan done for {}: {} notified", user_name, sent);
                                }
                            }
                            Err(e) => {
                                error!("Failed to fetch monitored URLs for {}: {}", user_name, e);
                            }
                        }

                        scan_index += 1;
                    }
                }
            }
            Err(e) => {
                error!("Failed to fetch users: {}", e);
            }
        }

        info!("Next scheduled scan cycle in {} seconds...", config.scan_interval_seconds);
        sleep(interval).await;
    }
}

#[tokio::main]
async fn main() {
    // Initialize logging
    unsafe {
        std::env::set_var("RUST_LOG", "info");
    }
    env_logger::init();

    info!("==================================================");
    info!("Career Scout Agent (Rust Edition) Starting...");
    info!("==================================================");

    let config = Arc::new(Config::from_env());
    let db = match Database::init().await {
        Ok(database) => database,
        Err(e) => {
            error!("Fatal database initialization error: {}", e);
            return;
        }
    };

    // Run database cleanup on startup
    let _ = db.clean_old_jobs(30).await;

    let bot = Bot::new(&config.bot_token);

    // Spawn the scheduler loop
    let scheduler_bot = bot.clone();
    let scheduler_db = db.clone();
    let scheduler_config = config.clone();
    tokio::spawn(async move {
        scheduled_scan_loop(scheduler_bot, scheduler_db, scheduler_config).await;
    });

    // Start bot dispatch
    start_bot(bot, db, config).await;
}
