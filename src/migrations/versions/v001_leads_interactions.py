# -*- coding: utf-8 -*-
"""
迁移 v001：线索运营与互动中心 数据基础。

严格遵循技术方案第 5 章的数据模型，只新增表与索引，绝不触碰原有业务表。
本迁移由 ``MigrationRunner`` 在独立事务内执行，失败自动回滚。
"""
# 确保 register 装饰器可用；这里通过 import 触发注册
from ..runner import register


@register("001_leads_interactions")
def upgrade_001(conn):
    conn.executescript(
        """
        -- 5.2 leads：用户级线索表
        CREATE TABLE IF NOT EXISTS leads (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          platform TEXT NOT NULL,
          platform_user_id TEXT,
          profile_url TEXT,
          nickname TEXT,

          dedupe_key TEXT NOT NULL,
          source_comment_id INTEGER,
          first_seen_at TEXT NOT NULL,
          last_seen_at TEXT NOT NULL,
          last_interaction_at TEXT,

          region_province TEXT,
          region_city TEXT,
          region_source TEXT NOT NULL DEFAULT 'unknown',
          region_confidence INTEGER NOT NULL DEFAULT 0,
          region_manual_province TEXT,
          region_manual_city TEXT,

          freshness_bucket TEXT NOT NULL DEFAULT 'unknown',
          intent_score INTEGER NOT NULL DEFAULT 0,
          intent_level TEXT NOT NULL DEFAULT 'unknown',
          intent_reasons TEXT NOT NULL DEFAULT '[]',
          rule_version TEXT,

          pool TEXT NOT NULL DEFAULT 'unclassified',
          owner_id INTEGER,
          status TEXT NOT NULL DEFAULT 'new',
          contact_allowed INTEGER NOT NULL DEFAULT 0,
          contact_block_reason TEXT,

          note TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,

          FOREIGN KEY(source_comment_id) REFERENCES comments(id),
          UNIQUE(dedupe_key)
        );

        -- 5.3 lead_evidence：线索证据表
        CREATE TABLE IF NOT EXISTS lead_evidence (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          lead_id INTEGER NOT NULL,
          task_id INTEGER,
          video_id INTEGER,
          comment_id INTEGER,
          evidence_type TEXT NOT NULL,
          evidence_text TEXT,
          occurred_at TEXT,
          collected_at TEXT NOT NULL,
          metadata TEXT NOT NULL DEFAULT '{}',
          FOREIGN KEY(lead_id) REFERENCES leads(id),
          FOREIGN KEY(task_id) REFERENCES tasks(id),
          FOREIGN KEY(video_id) REFERENCES videos(id),
          FOREIGN KEY(comment_id) REFERENCES comments(id),
          UNIQUE(lead_id, comment_id, evidence_type)
        );

        -- 5.4 owners 与分配
        CREATE TABLE IF NOT EXISTS owners (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          province TEXT,
          contact TEXT,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lead_assignments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          lead_id INTEGER NOT NULL,
          from_owner_id INTEGER,
          to_owner_id INTEGER,
          province TEXT,
          action TEXT NOT NULL,
          reason TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(lead_id) REFERENCES leads(id)
        );

        -- 5.5 互动数据
        CREATE TABLE IF NOT EXISTS interaction_drafts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          lead_id INTEGER NOT NULL,
          channel TEXT NOT NULL,
          template_id TEXT,
          content TEXT NOT NULL,
          generation_mode TEXT NOT NULL,
          policy_snapshot TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'draft',
          reviewed_by TEXT,
          reviewed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY(lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS interaction_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          lead_id INTEGER NOT NULL,
          draft_id INTEGER,
          account_id INTEGER,
          idempotency_key TEXT,
          event_type TEXT NOT NULL,
          result TEXT,
          detail TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(lead_id) REFERENCES leads(id),
          FOREIGN KEY(draft_id) REFERENCES interaction_drafts(id),
          FOREIGN KEY(account_id) REFERENCES accounts(id),
          UNIQUE(idempotency_key)
        );

        CREATE TABLE IF NOT EXISTS suppression_list (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          platform TEXT NOT NULL,
          platform_user_id TEXT,
          dedupe_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(dedupe_key)
        );

        CREATE TABLE IF NOT EXISTS lead_audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          lead_id INTEGER NOT NULL,
          actor TEXT NOT NULL,
          action TEXT NOT NULL,
          before_value TEXT,
          after_value TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(lead_id) REFERENCES leads(id)
        );

        -- 5.6 必需索引
        CREATE INDEX IF NOT EXISTS idx_leads_pool_status
          ON leads(pool, status);
        CREATE INDEX IF NOT EXISTS idx_leads_region
          ON leads(region_province, region_confidence);
        CREATE INDEX IF NOT EXISTS idx_leads_freshness
          ON leads(freshness_bucket, last_interaction_at);
        CREATE INDEX IF NOT EXISTS idx_leads_intent
          ON leads(intent_level, intent_score);
        CREATE INDEX IF NOT EXISTS idx_lead_evidence_lead
          ON lead_evidence(lead_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_interaction_drafts_status
          ON interaction_drafts(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_lead_audit_log_lead
          ON lead_audit_log(lead_id, created_at);
        """
    )
