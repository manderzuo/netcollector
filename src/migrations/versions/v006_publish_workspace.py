# -*- coding: utf-8 -*-
"""发布工作区数据表。

这些表与采集、互动表分开，保证发布中心的缓存和草稿不会改变原有采集链路。
"""

from ..runner import register


@register("006_publish_workspace")
def upgrade_006(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_contents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          account_id INTEGER NOT NULL,
          platform TEXT NOT NULL,
          content_id TEXT NOT NULL,
          content_type TEXT NOT NULL DEFAULT 'video',
          url TEXT NOT NULL DEFAULT '',
          title TEXT NOT NULL DEFAULT '',
          description TEXT NOT NULL DEFAULT '',
          cover_url TEXT NOT NULL DEFAULT '',
          published_at TEXT,
          like_count INTEGER NOT NULL DEFAULT 0,
          comment_count INTEGER NOT NULL DEFAULT 0,
          share_count INTEGER NOT NULL DEFAULT 0,
          favorite_count INTEGER NOT NULL DEFAULT 0,
          extra TEXT NOT NULL DEFAULT '{}',
          fetched_at TEXT NOT NULL,
          UNIQUE(account_id, platform, content_id),
          FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_account_contents_account
          ON account_contents(account_id, platform, fetched_at DESC);

        CREATE TABLE IF NOT EXISTS account_content_comments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          account_content_id INTEGER NOT NULL,
          platform TEXT NOT NULL,
          user_id TEXT NOT NULL DEFAULT '',
          nickname TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          comment_time TEXT,
          like_count INTEGER NOT NULL DEFAULT 0,
          parent_id INTEGER,
          is_reply INTEGER NOT NULL DEFAULT 0,
          extra TEXT NOT NULL DEFAULT '{}',
          FOREIGN KEY(account_content_id) REFERENCES account_contents(id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_account_content_comments
          ON account_content_comments(
            account_content_id, platform, COALESCE(user_id, ''), COALESCE(content, '')
          );

        CREATE TABLE IF NOT EXISTS generated_contents (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL DEFAULT '',
          body TEXT NOT NULL DEFAULT '',
          source_type TEXT NOT NULL DEFAULT 'local_template',
          source_ref TEXT NOT NULL DEFAULT '',
          platforms TEXT NOT NULL DEFAULT '[]',
          topics TEXT NOT NULL DEFAULT '[]',
          outline TEXT NOT NULL DEFAULT '',
          score_json TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'draft',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_generated_contents_updated
          ON generated_contents(updated_at DESC, id DESC);

        CREATE TABLE IF NOT EXISTS published_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          account_id INTEGER NOT NULL,
          platform TEXT NOT NULL,
          content_id TEXT NOT NULL DEFAULT '',
          message_id TEXT NOT NULL,
          message_type TEXT NOT NULL DEFAULT 'comment',
          user_id TEXT NOT NULL DEFAULT '',
          nickname TEXT NOT NULL DEFAULT '',
          content TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          is_read INTEGER NOT NULL DEFAULT 0,
          reply_draft_id INTEGER,
          extra TEXT NOT NULL DEFAULT '{}',
          UNIQUE(account_id, platform, message_id),
          FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_published_messages_read
          ON published_messages(is_read, created_at DESC);
        """
    )
