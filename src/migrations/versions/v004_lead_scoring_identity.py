# -*- coding: utf-8 -*-
"""迁移 v004：线索评分历史与用户身份操作审计。"""

from ..runner import register


@register("004_lead_scoring_identity")
def upgrade_004(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lead_score_rules (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          platform TEXT,
          version TEXT NOT NULL,
          config TEXT NOT NULL DEFAULT '{}',
          enabled INTEGER NOT NULL DEFAULT 1,
          applies_to_new INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(name, version)
        );

        CREATE TABLE IF NOT EXISTS lead_score_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          lead_id INTEGER NOT NULL,
          score INTEGER NOT NULL,
          level TEXT NOT NULL,
          reasons TEXT NOT NULL DEFAULT '[]',
          rule_version TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'rule',
          created_at TEXT NOT NULL,
          FOREIGN KEY(lead_id) REFERENCES leads(id)
        );

        CREATE TABLE IF NOT EXISTS lead_identity_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source_lead_id INTEGER NOT NULL,
          target_lead_id INTEGER,
          action TEXT NOT NULL,
          actor TEXT NOT NULL,
          reversible INTEGER NOT NULL DEFAULT 1,
          metadata TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          FOREIGN KEY(source_lead_id) REFERENCES leads(id),
          FOREIGN KEY(target_lead_id) REFERENCES leads(id)
        );

        CREATE INDEX IF NOT EXISTS idx_lead_score_history_lead_created
          ON lead_score_history(lead_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_lead_identity_events_source
          ON lead_identity_events(source_lead_id, created_at DESC);
        """
    )

