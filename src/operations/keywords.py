# -*- coding: utf-8 -*-
"""关键词组管理和平台无关的搜索组合展开。"""

from __future__ import annotations

from datetime import datetime
import re


TERM_TYPES = ("core", "synonym", "region", "exclude")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_terms(terms) -> list[str]:
    out, seen = [], set()
    for raw in terms or []:
        # 关键词编辑框支持一行一个，也支持用顿号/逗号批量添加。
        # 每个词都是独立搜索项，不再把整行当成一条组合搜索词。
        parts = re.split(r"[、，,；;\n\r]+", str(raw or ""))
        for part in parts:
            value = " ".join(part.split()).strip()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
    return out


class KeywordGroupStore:
    def __init__(self, conn, clock=None):
        self.conn = conn
        self.clock = clock or _now

    def create_group(self, name: str, platform: str | None = None) -> int:
        name = " ".join(str(name or "").split()).strip()
        if not name:
            raise ValueError("关键词组名称不能为空")
        now = self.clock()
        cur = self.conn.execute(
            "INSERT INTO keyword_groups (name, platform, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, platform or None, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_groups(self, enabled_only: bool = False) -> list[dict]:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = self.conn.execute(
            f"SELECT * FROM keyword_groups {where} ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def update_group(self, group_id: int, name: str, platform: str | None = None,
                     *, bump_version: bool = True) -> int:
        name = " ".join(str(name or "").split()).strip()
        if not name:
            raise ValueError("关键词组名称不能为空")
        current = self.conn.execute(
            "SELECT version FROM keyword_groups WHERE id = ?", (int(group_id),)
        ).fetchone()
        if current is None:
            raise KeyError(f"关键词组不存在: {group_id}")
        now = self.clock()
        version = int(current["version"] or 1) + (1 if bump_version else 0)
        self.conn.execute(
            "UPDATE keyword_groups SET name = ?, platform = ?, version = ?, updated_at = ? WHERE id = ?",
            (name, platform or None, version, now, int(group_id)),
        )
        self.conn.commit()
        return version

    def delete_group(self, group_id: int) -> None:
        if self.conn.execute(
            "SELECT 1 FROM keyword_groups WHERE id = ?", (int(group_id),)
        ).fetchone() is None:
            raise KeyError(f"关键词组不存在: {group_id}")
        self.conn.execute("DELETE FROM keyword_terms WHERE group_id = ?", (int(group_id),))
        self.conn.execute("DELETE FROM keyword_groups WHERE id = ?", (int(group_id),))
        self.conn.commit()

    def set_terms(self, group_id: int, terms: dict[str, list[str]]) -> int:
        normalized = {}
        for term_type, values in (terms or {}).items():
            if term_type not in TERM_TYPES:
                raise ValueError(f"未知关键词类型: {term_type}")
            normalized[term_type] = _clean_terms(values)
        exists = self.conn.execute(
            "SELECT id, version FROM keyword_groups WHERE id = ?", (int(group_id),)
        ).fetchone()
        if exists is None:
            raise KeyError(f"关键词组不存在: {group_id}")
        now = self.clock()
        self.conn.execute("DELETE FROM keyword_terms WHERE group_id = ?", (int(group_id),))
        for term_type, values in normalized.items():
            self.conn.executemany(
                "INSERT INTO keyword_terms (group_id, term_type, term, created_at) VALUES (?, ?, ?, ?)",
                [(int(group_id), term_type, value, now) for value in values],
            )
        version = int(exists["version"] or 1) + 1
        self.conn.execute(
            "UPDATE keyword_groups SET version = ?, updated_at = ? WHERE id = ?",
            (version, now, int(group_id)),
        )
        self.conn.commit()
        return version

    def get(self, group_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM keyword_groups WHERE id = ?", (int(group_id),)
        ).fetchone()
        if row is None:
            return None
        group = dict(row)
        rows = self.conn.execute(
            "SELECT term_type, term FROM keyword_terms WHERE group_id = ? ORDER BY id",
            (int(group_id),),
        ).fetchall()
        terms = {term_type: [] for term_type in TERM_TYPES}
        for item in rows:
            terms[item["term_type"]].append(item["term"])
        group["terms"] = terms
        return group

    def expand(self, group_id: int) -> list[dict]:
        group = self.get(group_id)
        if group is None:
            raise KeyError(f"关键词组不存在: {group_id}")
        terms = group["terms"]
        cores = _clean_terms(terms["core"] + terms["synonym"])
        regions = _clean_terms(terms["region"]) or [""]
        if not cores:
            raise ValueError("关键词组至少需要一个核心词或同义词")
        excludes = _clean_terms(terms["exclude"])
        result, seen = [], set()
        for core in cores:
            for region in regions:
                query = " ".join(part for part in (core, region) if part).strip()
                if query in seen:
                    continue
                seen.add(query)
                result.append({
                    "query": query,
                    "exclude_terms": list(excludes),
                    "group_id": int(group_id),
                    "version": int(group["version"] or 1),
                })
        return result
