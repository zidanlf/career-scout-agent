use teloxide::prelude::*;
use teloxide::utils::command::BotCommands;
use teloxide::types::{ParseMode, LinkPreviewOptions};
use std::sync::Arc;
use log::{info, warn, error};

use crate::config::Config;
use crate::database::Database;
use crate::scraper::{scrape_job_listings, parse_rss_jobs, ScrapedJob};

#[derive(BotCommands, Clone)]
#[command(rename_rule = "lowercase", description = "These commands are supported:")]
pub enum Command {
    #[command(description = "Register user and show welcome message.")]
    Start,
    #[command(description = "Set RSS feed URL.")]
    SetRss(String),
    #[command(description = "Delete RSS feed URL.")]
    DelRss,
    #[command(description = "Set keyword filter.")]
    SetKeywords(String),
    #[command(description = "List keyword filters.")]
    ListKeywords,
    #[command(description = "Delete all keyword filters.")]
    DelKeywords,
    #[command(description = "Show system status.")]
    Status,
    #[command(description = "Show 24h summary.")]
    Report,
    #[command(description = "Scan RSS feed now.")]
    ScanRss,
    #[command(description = "Add URL to monitor.")]
    Monitor(String),
    #[command(description = "Scan monitored URLs now.")]
    ScanMonitor,
    #[command(description = "Delete monitored URL.")]
    DelMonitor(String),
    #[command(description = "List monitored URLs.")]
    ListMonitor,
    #[command(description = "Clear job history.")]
    ClearJobs,
    #[command(description = "Pause auto-scanning.")]
    Pause,
    #[command(description = "Resume auto-scanning.")]
    Resume,
}

pub fn clean_company_name(company: &str) -> String {
    if company.is_empty() {
        return company.to_string();
    }
    if company.starts_with("di") && company.len() > 2 {
        if let Some(first_char) = company.chars().nth(2) {
            if first_char.is_ascii_uppercase() {
                return company[2..].trim().to_string();
            }
        }
    }
    company.trim().to_string()
}


pub fn format_job_notification(job: &ScrapedJob) -> String {
    let platform = job.platform.to_uppercase();
    let company = clean_company_name(&job.company);
    format!(
        "<b>Job Found in {}!</b>\n\n\
        <pre>\
        Role    : {}\n\
        Company : {}\
        </pre>\n\n\
        <a href=\"{}\">Apply Here</a>",
        platform, job.title, company, job.link
    )
}

pub fn job_matches_keywords(title: &str, keywords: &[String]) -> bool {
    if keywords.is_empty() {
        return true;
    }
    let title_lower = title.to_lowercase();
    keywords.iter().any(|kw| title_lower.contains(kw))
}

pub async fn start_bot(bot: Bot, db: Database, config: Arc<Config>) {
    let handler = Update::filter_message()
        .filter_command::<Command>()
        .endpoint(answer);

    info!("Bot application starting with Teloxide dispatcher...");
    Dispatcher::builder(bot, handler)
        .dependencies(dptree::deps![db, config])
        .build()
        .dispatch()
        .await;
}

async fn answer(
    bot: Bot,
    msg: Message,
    cmd: Command,
    db: Database,
    config: Arc<Config>,
) -> ResponseResult<()> {
    let user_id = match msg.from.as_ref() {
        Some(u) => u.id.0 as i64,
        None => return Ok(()),
    };

    // Authorization Whitelist check
    let authorized = user_id == config.zidan_id || user_id == config.partner_id;
    if !authorized {
        warn!("Unauthorized access attempt by: {}", user_id);
        return Ok(());
    }

    match cmd {
        Command::Start => {
            let user = msg.from.as_ref().unwrap();
            let full_name = format!("{} {}", user.first_name, user.last_name.as_deref().unwrap_or("")).trim().to_string();
            if let Err(e) = db.upsert_user(user_id, &full_name).await {
                error!("Database error in cmd_start: {}", e);
                bot.send_message(msg.chat.id, "Database error registering user.").await?;
                return Ok(());
            }

            let welcome = format!(
                "<b>Welcome, {}!</b>\n\
                You have been registered in Career Scout.\n\n\
                <b>Keywords</b>\n\
                /setkeywords &lt;k1&gt;, &lt;k2&gt; - Set role filter keywords\n\
                /listkeywords - Show active keywords\n\
                /delkeywords - Remove all keywords\n\n\
                <b>RSS Feed</b>\n\
                /setrss &lt;url&gt; - Set RSS feed URL\n\
                /delrss - Remove RSS feed\n\
                /scanrss - Scan RSS feed now\n\n\
                <b>URL Monitoring</b>\n\
                /monitor &lt;url&gt; &lt;tag&gt; - Add URL to monitor\n\
                /listmonitor - List monitored URLs\n\
                /delmonitor &lt;id&gt; - Remove monitored URL\n\
                /scanmonitor - Scan all monitored URLs now\n\n\
                <b>Control</b>\n\
                /pause - Pause auto-scanning\n\
                /resume - Resume auto-scanning\n\n\
                <b>Info</b>\n\
                /status - System status\n\
                /report - 24h summary\n\
                /clearjobs - Reset job history (re-discover all jobs)",
                user.first_name
            );

            bot.send_message(msg.chat.id, welcome)
                .parse_mode(ParseMode::Html)
                .await?;
        }
        Command::SetRss(url) => {
            let url_trimmed = url.trim();
            if url_trimmed.is_empty() {
                bot.send_message(msg.chat.id, "Usage: /setrss <url>\nExample: /setrss https://example.com/rss").await?;
                return Ok(());
            }

            if let Err(e) = db.update_rss(user_id, url_trimmed).await {
                error!("Database error: {}", e);
                bot.send_message(msg.chat.id, "Database error setting RSS URL.").await?;
                return Ok(());
            }

            bot.send_message(
                msg.chat.id,
                format!("RSS feed URL configured:\n<code>{}</code>", url_trimmed)
            )
            .parse_mode(ParseMode::Html)
            .await?;
        }
        Command::DelRss => {
            if let Err(e) = db.delete_rss(user_id).await {
                error!("Database error: {}", e);
                bot.send_message(msg.chat.id, "Database error deleting RSS URL.").await?;
                return Ok(());
            }
            bot.send_message(msg.chat.id, "RSS feed URL removed.").await?;
        }
        Command::SetKeywords(raw_kws) => {
            let raw_trimmed = raw_kws.trim();
            if raw_trimmed.is_empty() {
                bot.send_message(
                    msg.chat.id,
                    "<b>Usage:</b> <code>/setkeywords kata1, kata2, kata3</code>\n\n\
                    Only jobs with titles matching any keyword will be notified."
                )
                .parse_mode(ParseMode::Html)
                .await?;
                return Ok(());
            }

            let kws: Vec<String> = raw_trimmed.split(',')
                .map(|k| k.trim().to_lowercase())
                .filter(|k| !k.is_empty())
                .collect();

            if kws.is_empty() {
                bot.send_message(msg.chat.id, "No valid keywords found. Separate keywords with commas.").await?;
                return Ok(());
            }

            let kws_str = kws.join(",");
            if let Err(e) = db.update_keywords(user_id, &kws_str).await {
                error!("Database error: {}", e);
                bot.send_message(msg.chat.id, "Database error setting keywords.").await?;
                return Ok(());
            }

            bot.send_message(
                msg.chat.id,
                format!("<b>Keywords Updated</b>\n\n<pre>Keywords : {}</pre>\n\nOnly jobs matching these keywords will be notified.", kws.join(", "))
            )
            .parse_mode(ParseMode::Html)
            .await?;
        }
        Command::ListKeywords => {
            match db.get_user_keywords(user_id).await {
                Ok(kws) => {
                    if kws.is_empty() {
                        bot.send_message(msg.chat.id, "No keywords set. All jobs will be notified.").await?;
                    } else {
                        bot.send_message(
                            msg.chat.id,
                            format!("<b>Your Keywords</b>\n\n<pre>{}</pre>", kws.join(", "))
                        )
                        .parse_mode(ParseMode::Html)
                        .await?;
                    }
                }
                Err(e) => {
                    error!("Database error: {}", e);
                    bot.send_message(msg.chat.id, "Database error fetching keywords.").await?;
                }
            }
        }
        Command::DelKeywords => {
            if let Err(e) = db.delete_keywords(user_id).await {
                error!("Database error: {}", e);
                bot.send_message(msg.chat.id, "Database error deleting keywords.").await?;
                return Ok(());
            }
            bot.send_message(msg.chat.id, "<b>Keywords removed.</b>\n\nAll jobs will now be notified without filtering.").parse_mode(ParseMode::Html).await?;
        }
        Command::Status => {
            let rss_url = db.get_user_rss(user_id).await.unwrap_or(None);
            let rss_status = if rss_url.is_some() { "Configured" } else { "Not configured" };
            let keywords = db.get_user_keywords(user_id).await.unwrap_or_default();
            let kw_display = if keywords.is_empty() { "None (all jobs shown)".to_string() } else { keywords.join(", ") };
            let is_active = db.get_user_active(user_id).await.unwrap_or(true);
            let scan_status = if is_active { "✅ Active" } else { "⏸ Paused" };
            let db_stats = db.get_db_stats().await.unwrap_or_default();

            let status_msg = format!(
                "<b>System Status</b>\n\n\
                <b>Your Profile:</b>\n\
                - Scanning: {}\n\
                - RSS Feed: {}\n\
                - Keywords: {}\n\n\
                <b>Database Statistics:</b>\n\
                - Users: {}\n\
                - Jobs Processed: {}\n\
                - Monitored URLs: {}",
                scan_status,
                rss_status,
                kw_display,
                db_stats.get("users").unwrap_or(&0),
                db_stats.get("processed_jobs").unwrap_or(&0),
                db_stats.get("monitored_urls").unwrap_or(&0)
            );

            bot.send_message(msg.chat.id, status_msg)
                .parse_mode(ParseMode::Html)
                .await?;
        }
        Command::Report => {
            match db.get_jobs_last_24h(user_id).await {
                Ok(jobs) => {
                    if jobs.is_empty() {
                        bot.send_message(msg.chat.id, "<b>24-Hour Report</b>\n\nNo jobs scanned in the last 24 hours.")
                            .parse_mode(ParseMode::Html)
                            .await?;
                    } else {
                        bot.send_message(msg.chat.id, format!("<b>24-Hour Report</b>\n\nJobs found: {}", jobs.len()))
                            .parse_mode(ParseMode::Html)
                            .await?;
                    }
                }
                Err(e) => {
                    error!("Database error: {}", e);
                    bot.send_message(msg.chat.id, "Database error getting report.").await?;
                }
            }
        }
        Command::ScanRss => {
            let rss_url = match db.get_user_rss(user_id).await {
                Ok(Some(url)) => url,
                _ => {
                    bot.send_message(msg.chat.id, "No RSS feed configured. Use `/setrss` first.").await?;
                    return Ok(());
                }
            };

            bot.send_message(msg.chat.id, "Scanning RSS feed for jobs. Please wait...").await?;

            match parse_rss_jobs(&rss_url).await {
                Ok(jobs) => {
                    if jobs.is_empty() {
                        bot.send_message(msg.chat.id, "No new jobs found in the last 24 hours.").await?;
                        return Ok(());
                    }

                    bot.send_message(msg.chat.id, format!("Found {} new job(s). Sending notifications...", jobs.len())).await?;

                    let mut sent = 0;
                    for job in jobs {
                        if !db.check_job_processed(&job.hash, user_id).await.unwrap_or(false) {
                            if let Ok(_) = db.log_processed_job(&job.hash, user_id).await {
                                let mut enriched_job = job.clone();
                                if enriched_job.platform.is_empty() {
                                    enriched_job.platform = "rss".to_string();
                                }
                                let text = format_job_notification(&enriched_job);
                                bot.send_message(msg.chat.id, text)
                                    .parse_mode(ParseMode::Html)
                                    .link_preview_options(LinkPreviewOptions {
                                        is_disabled: true,
                                        url: None,
                                        prefer_small_media: false,
                                        prefer_large_media: false,
                                        show_above_text: false,
                                    })
                                    .await?;
                                sent += 1;
                            }
                        }
                    }

                    bot.send_message(msg.chat.id, format!("Scan complete. {} new job(s) notified.", sent)).await?;
                }
                Err(err) => {
                    error!("Error scanning RSS: {}", err);
                    bot.send_message(msg.chat.id, format!("Scan error: {}", err)).await?;
                }
            }
        }
        Command::Monitor(args) => {
            let parts: Vec<&str> = args.split_whitespace().collect();
            if parts.len() < 2 {
                let usage = "<b>Usage:</b> <code>/monitor &lt;URL&gt; &lt;TAG&gt;</code>\n\n\
                             <b>Example:</b>\n\
                             <code>/monitor https://www.kalibrr.com/c/jobs?search=data DE</code>";
                bot.send_message(msg.chat.id, usage)
                    .parse_mode(ParseMode::Html)
                    .await?;
                return Ok(());
            }

            let url = parts[0];
            let tag = parts[1].to_uppercase();

            if !url.starts_with("http") {
                bot.send_message(msg.chat.id, "Invalid URL. Must start with http:// or https://").await?;
                return Ok(());
            }

            match db.add_monitored_url(user_id, url, &tag).await {
                Ok(new_id) => {
                    let confirm = format!(
                        "<b>URL Added to Monitoring</b>\n\n\
                        <pre>\
                        ID    : {}\n\
                        TAG   : {}\n\
                        URL   : {}\
                        </pre>\n\
                        All your monitored URLs will be scanned every 2 minutes when it's your turn.",
                        new_id, tag, url
                    );
                    bot.send_message(msg.chat.id, confirm)
                        .parse_mode(ParseMode::Html)
                        .await?;
                }
                Err(e) => {
                    error!("Database error: {}", e);
                    bot.send_message(msg.chat.id, "Database error adding monitored URL.").await?;
                }
            }
        }
        Command::ScanMonitor => {
            bot.send_message(msg.chat.id, "Scanning your monitored URLs...").await?;
            
            let urls = match db.get_monitored_urls(user_id).await {
                Ok(list) => list,
                Err(e) => {
                    error!("Database error: {}", e);
                    bot.send_message(msg.chat.id, "Database error fetching monitored URLs.").await?;
                    return Ok(());
                }
            };

            if urls.is_empty() {
                bot.send_message(msg.chat.id, "No monitored URLs. Use `/monitor` to add one.").await?;
                return Ok(());
            }

            let keywords = db.get_user_keywords(user_id).await.unwrap_or_default();
            let mut total_scraped = 0;
            let mut new_sent = 0;
            let mut already_processed = 0;
            let mut filtered = 0;
            let mut errors = 0;

            for url_data in urls {
                match scrape_job_listings(&url_data.url).await {
                    jobs => {
                        if jobs.is_empty() {
                            continue;
                        }
                        total_scraped += jobs.len();
                        for job in jobs {
                            if db.check_job_processed(&job.hash, user_id).await.unwrap_or(false) {
                                already_processed += 1;
                                continue;
                            }

                            let _ = db.log_processed_job(&job.hash, user_id).await;
                            if job_matches_keywords(&job.title, &keywords) {
                                let text = format_job_notification(&job);
                                if let Err(e) = bot.send_message(msg.chat.id, text)
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
                                    error!("Failed to send job notification: {}", e);
                                    errors += 1;
                                } else {
                                    new_sent += 1;
                                }
                            } else {
                                filtered += 1;
                            }
                        }
                    }
                }
            }

            let mut summary = format!(
                "<b>Scan Complete</b>\n\n\
                Jobs scraped: {}\n\
                New jobs sent: {}\n\
                Filtered (keywords): {}\n\
                Already processed: {}\n",
                total_scraped, new_sent, filtered, already_processed
            );
            if errors > 0 {
                summary.push_str(&format!("Errors: {}\n", errors));
            }

            bot.send_message(msg.chat.id, summary)
                .parse_mode(ParseMode::Html)
                .await?;
        }
        Command::DelMonitor(id_str) => {
            let trimmed = id_str.trim();
            if trimmed.is_empty() {
                bot.send_message(msg.chat.id, "<b>Usage:</b> <code>/delmonitor &lt;ID&gt;</code>\n\nUse <code>/listmonitor</code> to list IDs.")
                    .parse_mode(ParseMode::Html)
                    .await?;
                return Ok(());
            }

            let url_id: i64 = match trimmed.parse() {
                Ok(num) => num,
                Err(_) => {
                    bot.send_message(msg.chat.id, "Invalid ID. Must be a number.").await?;
                    return Ok(());
                }
            };

            match db.delete_monitored_url(user_id, url_id).await {
                Ok(true) => {
                    bot.send_message(msg.chat.id, format!("Monitored URL with ID <code>{}</code> deleted.", url_id))
                        .parse_mode(ParseMode::Html)
                        .await?;
                }
                Ok(false) => {
                    bot.send_message(msg.chat.id, "URL not found.").await?;
                }
                Err(e) => {
                    error!("Database error: {}", e);
                    bot.send_message(msg.chat.id, "Database error deleting monitored URL.").await?;
                }
            }
        }
        Command::ListMonitor => {
            match db.get_monitored_urls(user_id).await {
                Ok(urls) => {
                    if urls.is_empty() {
                        bot.send_message(msg.chat.id, "No monitored URLs. Use <code>/monitor</code> to add one.")
                            .parse_mode(ParseMode::Html)
                            .await?;
                        return Ok(());
                    }

                    let mut lines = vec!["<b>Your Monitored URLs</b>\n---------------------------".to_string()];
                    for u in urls {
                        lines.push(format!(
                            "\nID    : <code>{}</code>\n\
                            TAG   : <code>{}</code>\n\
                            URL   : {}",
                            u.id, u.label, u.url
                        ));
                    }
                    bot.send_message(msg.chat.id, lines.join("\n"))
                        .parse_mode(ParseMode::Html)
                        .await?;
                }
                Err(e) => {
                    error!("Database error: {}", e);
                    bot.send_message(msg.chat.id, "Database error listing monitored URLs.").await?;
                }
            }
        }
        Command::ClearJobs => {
            match db.clear_processed_jobs(user_id).await {
                Ok(count) => {
                    bot.send_message(
                        msg.chat.id,
                        format!("<b>Job history cleared</b>\n\nDeleted {} processed job records.\nRun <code>/scanmonitor</code> to re-scan.", count)
                    )
                    .parse_mode(ParseMode::Html)
                    .await?;
                }
                Err(e) => {
                    error!("Database error: {}", e);
                    bot.send_message(msg.chat.id, "Database error clearing history.").await?;
                }
            }
        }
        Command::Pause => {
            if let Err(e) = db.set_user_active(user_id, false).await {
                error!("Database error: {}", e);
                bot.send_message(msg.chat.id, "Database error pausing scanning.").await?;
                return Ok(());
            }
            bot.send_message(
                msg.chat.id,
                "<b>⏸ Scanning Paused</b>\n\nAuto-scanning has been paused.\nUse <code>/resume</code> to re-enable."
            )
            .parse_mode(ParseMode::Html)
            .await?;
        }
        Command::Resume => {
            if let Err(e) = db.set_user_active(user_id, true).await {
                error!("Database error: {}", e);
                bot.send_message(msg.chat.id, "Database error resuming scanning.").await?;
                return Ok(());
            }
            bot.send_message(
                msg.chat.id,
                "<b>▶️ Scanning Resumed</b>\n\nAuto-scanning has been re-enabled."
            )
            .parse_mode(ParseMode::Html)
            .await?;
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_clean_company_name() {
        assert_eq!(clean_company_name("diPT YAKIN BERTUMBUH"), "PT YAKIN BERTUMBUH");
        assert_eq!(clean_company_name("diCV Maju Jaya"), "CV Maju Jaya");
        assert_eq!(clean_company_name("PT Indah Jaya"), "PT Indah Jaya");
        assert_eq!(clean_company_name(""), "");
    }

    #[test]
    fn test_job_matches_keywords() {
        let keywords = vec!["rust".to_string(), "python".to_string()];
        assert!(job_matches_keywords("Senior Rust Developer", &keywords));
        assert!(job_matches_keywords("Python Engineer", &keywords));
        assert!(!job_matches_keywords("Java Architect", &keywords));
        assert!(job_matches_keywords("Java Developer", &[])); // Empty matches all
    }
}

