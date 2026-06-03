use sqlx::{sqlite::SqlitePoolOptions, SqlitePool, Row};
use std::path::Path;
use log::{info, debug};

#[derive(sqlx::FromRow, Debug, Clone)]
pub struct DbUser {
    pub telegram_id: i64,
    pub name: Option<String>,
    pub rss_url: Option<String>,
    pub keywords: Option<String>,
    pub active: i32,
}

#[derive(sqlx::FromRow, Debug, Clone)]
pub struct DbMonitoredUrl {
    pub id: i64,
    pub user_id: i64,
    pub url: String,
    pub label: String,
    pub created_at: Option<String>,
}

#[derive(Clone)]
pub struct Database {
    pool: SqlitePool,
}

impl Database {
    pub async fn init() -> Result<Self, sqlx::Error> {
        let db_dir = Path::new("data");
        tokio::fs::create_dir_all(db_dir).await.ok();
        let db_path = db_dir.join("scout.db");
        let db_url = format!("sqlite://{}", db_path.to_string_lossy());
        
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect(&db_url)
            .await?;
            
        // Create tables
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT,
                rss_url TEXT,
                keywords TEXT,
                active INTEGER DEFAULT 1
            )"
        ).execute(&pool).await?;

        // Migration: add keywords column if missing
        let _ = sqlx::query("ALTER TABLE users ADD COLUMN keywords TEXT")
            .execute(&pool)
            .await;
            
        // Migration: add active column if missing
        let _ = sqlx::query("ALTER TABLE users ADD COLUMN active INTEGER DEFAULT 1")
            .execute(&pool)
            .await;

        sqlx::query(
            "CREATE TABLE IF NOT EXISTS processed_jobs (
                job_hash TEXT,
                user_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
                found_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_hash, user_id)
            )"
        ).execute(&pool).await?;

        // Migration: add created_at column if missing
        if let Err(_) = sqlx::query("ALTER TABLE processed_jobs ADD COLUMN created_at TIMESTAMP").execute(&pool).await {
            // Already exists or other error
        } else {
            let _ = sqlx::query("UPDATE processed_jobs SET created_at = found_at WHERE created_at IS NULL")
                .execute(&pool)
                .await;
            info!("Migrated: added 'created_at' column to processed_jobs table");
        }

        sqlx::query(
            "CREATE TABLE IF NOT EXISTS monitored_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(telegram_id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                label TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"
        ).execute(&pool).await?;

        info!("Database initialized successfully");
        Ok(Database { pool })
    }

    // ============== USER OPERATIONS ==============

    pub async fn upsert_user(&self, telegram_id: i64, name: &str) -> Result<(), sqlx::Error> {
        sqlx::query(
            "INSERT INTO users (telegram_id, name)
             VALUES (?, ?)
             ON CONFLICT(telegram_id) DO UPDATE SET name = excluded.name"
        )
        .bind(telegram_id)
        .bind(name)
        .execute(&self.pool)
        .await?;
        info!("User upserted: {} ({})", telegram_id, name);
        Ok(())
    }

    pub async fn get_user_rss(&self, telegram_id: i64) -> Result<Option<String>, sqlx::Error> {
        let row = sqlx::query("SELECT rss_url FROM users WHERE telegram_id = ?")
            .bind(telegram_id)
            .fetch_optional(&self.pool)
            .await?;
        Ok(row.and_then(|r| r.get::<Option<String>, _>("rss_url")))
    }

    pub async fn get_all_users(&self) -> Result<Vec<DbUser>, sqlx::Error> {
        let rows = sqlx::query_as::<_, DbUser>(
            "SELECT telegram_id, name, rss_url, keywords, active FROM users"
        )
        .fetch_all(&self.pool)
        .await?;
        Ok(rows)
    }

    pub async fn get_user_keywords(&self, telegram_id: i64) -> Result<Vec<String>, sqlx::Error> {
        let row = sqlx::query("SELECT keywords FROM users WHERE telegram_id = ?")
            .bind(telegram_id)
            .fetch_optional(&self.pool)
            .await?;
        
        let kw_str = match row.and_then(|r| r.get::<Option<String>, _>("keywords")) {
            Some(s) => s,
            None => return Ok(vec![]),
        };
        
        Ok(kw_str.split(',')
            .map(|k| k.trim().to_lowercase())
            .filter(|k| !k.is_empty())
            .collect())
    }

    pub async fn update_keywords(&self, telegram_id: i64, keywords_str: &str) -> Result<(), sqlx::Error> {
        let kw_val = if keywords_str.trim().is_empty() { None } else { Some(keywords_str) };
        sqlx::query("UPDATE users SET keywords = ? WHERE telegram_id = ?")
            .bind(kw_val)
            .bind(telegram_id)
            .execute(&self.pool)
            .await?;
        info!("Keywords updated for user {}: {:?}", telegram_id, kw_val);
        Ok(())
    }

    pub async fn set_user_active(&self, telegram_id: i64, active: bool) -> Result<(), sqlx::Error> {
        let active_val = if active { 1 } else { 0 };
        sqlx::query("UPDATE users SET active = ? WHERE telegram_id = ?")
            .bind(active_val)
            .bind(telegram_id)
            .execute(&self.pool)
            .await?;
        let status = if active { "active" } else { "paused" };
        info!("User {} set to {}", telegram_id, status);
        Ok(())
    }

    pub async fn get_user_active(&self, telegram_id: i64) -> Result<bool, sqlx::Error> {
        let row = sqlx::query("SELECT active FROM users WHERE telegram_id = ?")
            .bind(telegram_id)
            .fetch_optional(&self.pool)
            .await?;
        Ok(row.map(|r| r.get::<i32, _>("active") != 0).unwrap_or(true))
    }

    pub async fn delete_keywords(&self, telegram_id: i64) -> Result<(), sqlx::Error> {
        sqlx::query("UPDATE users SET keywords = NULL WHERE telegram_id = ?")
            .bind(telegram_id)
            .execute(&self.pool)
            .await?;
        info!("Keywords deleted for user {}", telegram_id);
        Ok(())
    }

    // ============== RSS OPERATIONS ==============

    pub async fn update_rss(&self, telegram_id: i64, url: &str) -> Result<(), sqlx::Error> {
        sqlx::query("UPDATE users SET rss_url = ? WHERE telegram_id = ?")
            .bind(url)
            .bind(telegram_id)
            .execute(&self.pool)
            .await?;
        info!("RSS URL updated for user {}", telegram_id);
        Ok(())
    }

    pub async fn delete_rss(&self, telegram_id: i64) -> Result<(), sqlx::Error> {
        sqlx::query("UPDATE users SET rss_url = NULL WHERE telegram_id = ?")
            .bind(telegram_id)
            .execute(&self.pool)
            .await?;
        info!("RSS URL deleted for user {}", telegram_id);
        Ok(())
    }

    // ============== JOB OPERATIONS ==============

    pub async fn check_job_processed(&self, job_hash: &str, user_id: i64) -> Result<bool, sqlx::Error> {
        let row = sqlx::query("SELECT 1 FROM processed_jobs WHERE job_hash = ? AND user_id = ?")
            .bind(job_hash)
            .bind(user_id)
            .fetch_optional(&self.pool)
            .await?;
        Ok(row.is_some())
    }

    pub async fn log_processed_job(&self, job_hash: &str, user_id: i64) -> Result<(), sqlx::Error> {
        sqlx::query("INSERT OR IGNORE INTO processed_jobs (job_hash, user_id, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)")
            .bind(job_hash)
            .bind(user_id)
            .execute(&self.pool)
            .await?;
        debug!("Job logged: hash={}... user={}", &job_hash[..std::cmp::min(8, job_hash.len())], user_id);
        Ok(())
    }

    pub async fn clear_processed_jobs(&self, user_id: i64) -> Result<u64, sqlx::Error> {
        let res = sqlx::query("DELETE FROM processed_jobs WHERE user_id = ?")
            .bind(user_id)
            .execute(&self.pool)
            .await?;
        let count = res.rows_affected();
        info!("Cleared {} processed jobs for user {}", count, user_id);
        Ok(count)
    }

    pub async fn clean_old_jobs(&self, days: i64) -> Result<u64, sqlx::Error> {
        let cutoff = (chrono::Utc::now() - chrono::Duration::days(days)).format("%Y-%m-%d %H:%M:%S").to_string();
        let res = sqlx::query("DELETE FROM processed_jobs WHERE created_at < ?")
            .bind(cutoff)
            .execute(&self.pool)
            .await?;
        let count = res.rows_affected();
        if count > 0 {
            info!("Auto-cleaned {} expired jobs older than {} days", count, days);
        }
        Ok(count)
    }

    pub async fn get_jobs_last_24h(&self, user_id: i64) -> Result<Vec<(String, String)>, sqlx::Error> {
        let cutoff = (chrono::Utc::now() - chrono::Duration::hours(24)).format("%Y-%m-%dT%H:%M:%S").to_string();
        let rows = sqlx::query(
            "SELECT job_hash, found_at 
             FROM processed_jobs 
             WHERE user_id = ? AND found_at >= ?
             ORDER BY found_at DESC"
        )
        .bind(user_id)
        .bind(cutoff)
        .fetch_all(&self.pool)
        .await?;
        
        Ok(rows.into_iter().map(|r| {
            (r.get::<String, _>("job_hash"), r.get::<String, _>("found_at"))
        }).collect())
    }

    // ============== STATISTICS ==============

    pub async fn get_db_stats(&self) -> Result<std::collections::HashMap<String, i64>, sqlx::Error> {
        let mut stats = std::collections::HashMap::new();
        
        let users_cnt: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users")
            .fetch_one(&self.pool)
            .await?;
        stats.insert("users".to_string(), users_cnt);
        
        let jobs_cnt: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM processed_jobs")
            .fetch_one(&self.pool)
            .await?;
        stats.insert("processed_jobs".to_string(), jobs_cnt);
        
        let urls_cnt: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM monitored_urls")
            .fetch_one(&self.pool)
            .await?;
        stats.insert("monitored_urls".to_string(), urls_cnt);
        
        Ok(stats)
    }

    // ============== MONITORED URL OPERATIONS ==============

    pub async fn add_monitored_url(&self, user_id: i64, url: &str, label: &str) -> Result<i64, sqlx::Error> {
        let label_upper = label.to_uppercase();
        let res = sqlx::query(
            "INSERT INTO monitored_urls (user_id, url, label)
             VALUES (?, ?, ?)"
        )
        .bind(user_id)
        .bind(url)
        .bind(&label_upper)
        .execute(&self.pool)
        .await?;
        
        let new_id = res.last_insert_rowid();
        info!("Monitored URL added: id={} user={} label={}", new_id, user_id, label_upper);
        Ok(new_id)
    }

    pub async fn get_monitored_urls(&self, user_id: i64) -> Result<Vec<DbMonitoredUrl>, sqlx::Error> {
        let rows = sqlx::query_as::<_, DbMonitoredUrl>(
            "SELECT id, user_id, url, label, created_at FROM monitored_urls WHERE user_id = ?"
        )
        .bind(user_id)
        .fetch_all(&self.pool)
        .await?;
        Ok(rows)
    }

    pub async fn get_all_monitored_urls(&self) -> Result<Vec<DbMonitoredUrl>, sqlx::Error> {
        let rows = sqlx::query_as::<_, DbMonitoredUrl>(
            "SELECT id, user_id, url, label, created_at FROM monitored_urls"
        )
        .fetch_all(&self.pool)
        .await?;
        Ok(rows)
    }

    pub async fn delete_monitored_url(&self, user_id: i64, url_id: i64) -> Result<bool, sqlx::Error> {
        let res = sqlx::query("DELETE FROM monitored_urls WHERE id = ? AND user_id = ?")
            .bind(url_id)
            .bind(user_id)
            .execute(&self.pool)
            .await?;
        let deleted = res.rows_affected() > 0;
        if deleted {
            info!("Monitored URL deleted: id={} user={}", url_id, user_id);
        }
        Ok(deleted)
    }
}
