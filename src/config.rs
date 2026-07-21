use std::env;
use chrono::FixedOffset;

pub struct Config {
    pub bot_token: String,
    pub zidan_id: i64,
    pub partner_id: i64,
    pub scan_interval_seconds: u64,
    pub scrapingant_api_key: Option<String>,
}

impl Config {
    pub fn from_env() -> Self {
        dotenvy::dotenv().ok();
        
        let bot_token = env::var("TELEGRAM_BOT_TOKEN")
            .expect("TELEGRAM_BOT_TOKEN must be set in .env or environment");
            
        let zidan_id = env::var("ZIDAN_ID")
            .unwrap_or_else(|_| "0".to_string())
            .parse::<i64>()
            .unwrap_or(0);
            
        let partner_id = env::var("PARTNER_ID")
            .unwrap_or_else(|_| "0".to_string())
            .parse::<i64>()
            .unwrap_or(0);
            
        let scan_interval_seconds = env::var("SCAN_INTERVAL")
            .unwrap_or_else(|_| "120".to_string())
            .parse::<u64>()
            .unwrap_or(120);

        let scrapingant_api_key = env::var("SCRAPINGANT_API_KEY").ok()
            .filter(|s| !s.trim().is_empty());

        Config {
            bot_token,
            zidan_id,
            partner_id,
            scan_interval_seconds,
            scrapingant_api_key,
        }
    }
}

pub fn get_wib_timezone() -> FixedOffset {
    FixedOffset::east_opt(7 * 3600).expect("Valid timezone offset")
}
