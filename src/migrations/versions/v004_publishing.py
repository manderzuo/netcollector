# -*- coding: utf-8 -*-
from ..runner import register


@register("004_publishing")
def upgrade_004(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS publish_drafts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL DEFAULT '',
          source_content TEXT NOT NULL DEFAULT '',
          content_type TEXT NOT NULL DEFAULT 'text',
          status TEXT NOT NULL DEFAULT 'draft',
          version INTEGER NOT NULL DEFAULT 1,
          created_by TEXT NOT NULL DEFAULT 'local_user',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS publish_variants (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          draft_id INTEGER NOT NULL,
          platform TEXT NOT NULL,
          content_type TEXT NOT NULL DEFAULT 'text',
          title TEXT NOT NULL DEFAULT '',
          body TEXT NOT NULL DEFAULT '',
          topics TEXT NOT NULL DEFAULT '[]',
          cover_path TEXT,
          settings TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'ready',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(draft_id) REFERENCES publish_drafts(id) ON DELETE CASCADE,
          UNIQUE(draft_id, platform)
        );

        CREATE TABLE IF NOT EXISTS publish_assets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          draft_id INTEGER NOT NULL,
          path TEXT NOT NULL,
          asset_type TEXT NOT NULL,
          sha256 TEXT,
          width INTEGER,
          height INTEGER,
          duration_ms INTEGER,
          validation TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          FOREIGN KEY(draft_id) REFERENCES publish_drafts(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS publish_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          variant_id INTEGER NOT NULL,
          account_id INTEGER NOT NULL,
          scheduled_at TEXT,
          status TEXT NOT NULL DEFAULT 'queued',
          current_step TEXT NOT NULL DEFAULT 'queued',
          real_send_authorized INTEGER NOT NULL DEFAULT 0,
          retry_count INTEGER NOT NULL DEFAULT 0,
          platform_post_id TEXT,
          platform_url TEXT,
          error_code TEXT,
          error_message TEXT,
          run_id TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(variant_id) REFERENCES publish_variants(id) ON DELETE CASCADE,
          FOREIGN KEY(account_id) REFERENCES accounts(id)
        );

        CREATE TABLE IF NOT EXISTS publish_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id INTEGER NOT NULL,
          run_id TEXT NOT NULL,
          step TEXT NOT NULL,
          page_url TEXT,
          selector TEXT,
          result TEXT NOT NULL DEFAULT '{}',
          evidence_path TEXT,
          error_code TEXT,
          error_message TEXT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          FOREIGN KEY(job_id) REFERENCES publish_jobs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS publish_account_locks (
          account_id INTEGER PRIMARY KEY,
          job_id INTEGER,
          owner TEXT NOT NULL,
          acquired_at TEXT NOT NULL,
          FOREIGN KEY(account_id) REFERENCES accounts(id),
          FOREIGN KEY(job_id) REFERENCES publish_jobs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_publish_drafts_status
          ON publish_drafts(status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_publish_variants_platform
          ON publish_variants(platform, draft_id);
        CREATE INDEX IF NOT EXISTS idx_publish_jobs_status
          ON publish_jobs(status, scheduled_at);
        CREATE INDEX IF NOT EXISTS idx_publish_attempts_job
          ON publish_attempts(job_id, started_at);
        """
    )
