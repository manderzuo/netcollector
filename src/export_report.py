#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""export_report.py — 统一的采集结果导出与意向分析（跨平台抖音/小红书）。

按设计文档 §7A/§7B：
  · 统一打分引擎（intent_rules.json 三级 + 回复模板 + 招聘排除）
  · 4 类 CSV 导出（UTF-8 BOM，Excel 友好）：
      1. {task}_videos.csv      作品清单
      2. {task}_comments.csv    全部评论
      3. {task}_intent_leads.csv 意向评论明细
      4. {task}_user_leads.csv   高意向用户聚合
  · 兼容两种采集输入形状：
      抖音  videos.json（dy_collect 输出：vid/url/desc/.../comments[]）
      小红书 notes3.json（xhs_collect3 输出：note_id/title/author/.../comments[]）

用法:
  python export_report.py --platform douyin --input out/抖音-快递柜/videos.json --outdir out/抖音-快递柜
  python export_report.py --platform xhs    --input out/快递柜/notes3.json --outdir out/快递柜
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def _intent_file_candidates() -> list[Path]:
    """查找源码版和 PyInstaller 版的意向规则资源。"""
    module_dir = Path(__file__).resolve().parent
    roots = [module_dir, module_dir / "src"]
    bundle_root = getattr(sys, "_MEIPASS", "")
    if bundle_root:
        roots.extend([Path(bundle_root), Path(bundle_root) / "src"])
    return [root / "intent_rules.json" for root in roots]


INTENT_FILE = str(_intent_file_candidates()[0])


def _minute_time(value):
    """统一输出采集时间，精确到分钟。"""
    if not value:
        return ""
    text = str(value).strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text)).strftime("%Y-%m-%d %H:%M")
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text[:16].replace("T", " ")


def load_intent_rules():
    for path in _intent_file_candidates():
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
    searched = ", ".join(str(path) for path in _intent_file_candidates())
    raise FileNotFoundError(f"找不到意向规则文件 intent_rules.json，已搜索：{searched}")


_RULES = None


def get_rules():
    global _RULES
    if _RULES is None:
        _RULES = load_intent_rules()
    return _RULES


def rate_text(text):
    """打分：返回 (score, label) 。命中高优先于中，招聘单独归类。"""
    r = get_rules()
    if not isinstance(text, str) or not text:
        return 0, r["low"]["label"]
    for k in r.get("recruit", {}).get("keywords", []):
        if k in text:
            return r["recruit"]["score"], r["recruit"]["label"]
    for k in r.get("high", {}).get("keywords", []):
        if k in text:
            return r["high"]["score"], r["high"]["label"]
    for k in r.get("medium", {}).get("keywords", []):
        if k in text:
            return r["medium"]["score"], r["medium"]["label"]
    return r["low"]["score"], r["low"]["label"]


def template_for(label):
    r = get_rules()
    for lv in ("high", "medium", "low", "recruit"):
        if r[lv]["label"] == label:
            return r[lv].get("template", "")
    return ""


# ---------------------------------------------------------------------------
# 字段适配：把不同平台的采集 JSON 归一化为统一结构
#   NormalizedItem = {
#     "pid", "url", "title", "author", "pub_str", "comments": [NormalizedComment]
#   }
#   NormalizedComment = {
#     "uid", "nickname", "homepage", "text", "region",
#     "ctime_str", "digg", "parent_id"
#   }
# ---------------------------------------------------------------------------
def parse_video_item(v):
    """抖音 videos.json 的一项。"""
    comments = v.get("comments") or []
    return {
        "pid": str(v.get("vid", "")),
        "url": v.get("url", ""),
        "kind": v.get("kind", "video"),
        "title": (v.get("desc") or "")[:120] or (v.get("title") or ""),
        "author": v.get("nickname", ""),
        "pub_str": _dy_pub(v),
        "collected_at": _minute_time(v.get("collected_at")),
        "author_home": ("https://www.douyin.com/user/" + str(v["sec_uid"])) if v.get("sec_uid") else "",
        "comments": [_parse_dy_comment(c) for c in comments],
    }


def _dy_pub(v):
    ct = v.get("create_time")
    if ct:
        try:
            import time as _t
            return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(int(ct)))
        except Exception:
            return ""
    return v.get("collected_at", "")


def _parse_dy_comment(c):
    return {
        "uid": str(c.get("user_id", "")),
        "nickname": c.get("nickname", ""),
        "homepage": c.get("homepage", ""),
        "text": (c.get("text") or ""),
        "region": c.get("region", ""),
        "ctime_str": c.get("create_time_str", ""),
        "digg": c.get("digg_count", ""),
        "parent_id": str(c.get("parent_id", "")),
    }


def parse_note_item(n):
    """小红书 notes3.json 的一项。"""
    comments = n.get("comments") or []
    pub = (n.get("pub_time") or n.get("time") or "").strip()
    # pub_time 形如 "1月前" 或 "2025-10-09"；time 可能是多行，取最后一行
    if pub and "\n" in pub:
        pub = pub.split("\n")[-1].strip()
    return {
        "pid": str(n.get("note_id") or n.get("id", "")),
        "url": "https://www.xiaohongshu.com/explore/" + str(n.get("note_id") or n.get("id", "")),
        "kind": n.get("type", "note"),
        "title": (n.get("title") or "")[:120],
        "author": n.get("author", ""),
        "pub_str": pub,
        "collected_at": _minute_time(n.get("collected_at")),
        "author_home": ("https://www.xiaohongshu.com/user/profile/" + str(n["author_id"])) if n.get("author_id") else "",
        "comments": [_parse_xhs_comment(c) for c in comments],
    }


def _parse_xhs_comment(c):
    return {
        "uid": str(c.get("user_id", "")),
        "nickname": c.get("user", ""),
        "homepage": c.get("homepage", ""),
        "text": (c.get("text") or ""),
        "region": c.get("region", ""),
        "ctime_str": c.get("time", ""),
        "digg": c.get("digg_count", ""),
        "parent_id": "",
    }


def parse_bilibili_item(v):
    """B站适配器输出的统一映射。"""
    comments = v.get("comments") or []
    extra = v.get("extra") or {}
    return {
        "pid": str(v.get("vid") or v.get("bvid") or ""),
        "url": v.get("url", ""), "kind": "video",
        "title": (v.get("title") or "")[:120],
        "author": v.get("author", ""),
        "pub_str": v.get("create_time") or extra.get("create_time", ""),
        "collected_at": _minute_time(v.get("collected_at")),
        "author_home": extra.get("author_url", ""),
        "comments": [{
            "uid": str(c.get("user_id", "")), "nickname": c.get("nickname", ""),
            "homepage": c.get("homepage", ""), "text": c.get("content", ""),
            "region": c.get("region", ""), "ctime_str": c.get("comment_time", ""),
            "digg": c.get("digg_count", c.get("digg", "")),
            "parent_id": c.get("reply_to", ""),
        } for c in comments],
    }


def load_items(platform, path):
    data = json.load(open(path, encoding="utf-8"))
    if platform in ("douyin", "dy"):
        return [parse_video_item(v) for v in data]
    if platform in ("xhs", "xiaohongshu"):
        return [parse_note_item(n) for n in data]
    if platform in ("bilibili", "bili"):
        return [parse_bilibili_item(v) for v in data]
    raise ValueError(f"未知平台: {platform}")


# ---------------------------------------------------------------------------
# 打分 + 汇总
# ---------------------------------------------------------------------------
def _has_text(value):
    return str(value or "").strip() != ""


def filter_valid_items(items):
    """过滤无效作品/评论，导出计数与文件内容使用同一份数据集。

    采集链路保留原始库记录用于审计，但导出层不输出空标题、空链接或
    空评论正文，避免“任务完成数量”和可交付数据不一致。
    """
    valid = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        if not _has_text(raw.get("title")) or not _has_text(raw.get("url")):
            continue
        comments = []
        for comment in raw.get("comments") or []:
            if not isinstance(comment, dict) or not _has_text(
                comment.get("text", comment.get("content"))
            ):
                continue
            item = dict(comment)
            item["text"] = str(item.get("text", item.get("content")) or "").strip()
            comments.append(item)
        item = dict(raw)
        item["comments"] = comments
        valid.append(item)
    return valid


def score_items(items):
    """为每条评论打分，注入 intent_score/intent_label/suggestion。"""
    for it in items:
        for index, cm in enumerate(it["comments"]):
            sc, label = rate_text(cm["text"])
            cm["intent_score"] = sc
            cm["intent_label"] = label
            cm["suggestion"] = template_for(label) if sc >= 3 else ""
    return items


# ---------------------------------------------------------------------------
# 4 类 CSV 导出
# ---------------------------------------------------------------------------
def export_unified(items, outdir, task="task"):
    """导出单一主数据 CSV：作品、评论、意向和用户聚合字段合并。"""
    items = filter_valid_items(items)
    score_items(items)
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, f"{task}.csv")
    header = [
        "平台", "视频ID", "标题", "帖子内容", "作者", "发布时间", "采集时间", "作品URL", "评论状态",
        "地区", "用户ID", "昵称", "用户主页", "评论内容", "点赞数", "评论时间",
        "意向评分", "意向评级", "高意向评论数", "评论总条数",
        "最新评论时间", "涉及作品数", "高意向评论摘录", "涉及作品列表",
    ]

    users = {}
    for it in items:
        for index, cm in enumerate(it.get("comments", [])):
            uid = cm.get("uid")
            homepage = cm.get("homepage")
            if uid:
                aggregate_key = f"{it.get('kind', '')}:uid:{uid}"
            elif homepage:
                aggregate_key = f"{it.get('kind', '')}:home:{homepage}"
            else:
                # 没有稳定身份时宁可按评论拆开，也不能把同名/匿名用户错误合并。
                aggregate_key = f"{it.get('kind', '')}:anonymous:{it.get('pid', '')}:{index}"
            cm["_aggregate_key"] = aggregate_key
            u = users.setdefault(aggregate_key, {
                "high": 0, "total": 0, "times": [], "works": set(), "high_texts": [],
            })
            if cm.get("intent_label") == "高意向":
                u["high"] += 1
                u["high_texts"].append(cm.get("text", ""))
            u["total"] += 1
            if cm.get("ctime_str"):
                u["times"].append(cm["ctime_str"])
            u["works"].add(it.get("title") or it.get("url") or "")

    rows = []
    for it in items:
        comments = it.get("comments", [])
        content = it.get("content") or it.get("description") or ""
        if not comments:
            # 贴吧帖子可能已被删除或暂时没有可读楼层，但搜索结果本身仍
            # 是有效采集内容；保留帖子元数据，避免导出只有表头。
            rows.append([
                _esc(it.get("kind")), _esc(it.get("pid")), _esc(it.get("title")),
                _esc(content), _esc(it.get("author")), _esc(it.get("pub_str")),
                _minute_time(it.get("collected_at")), _esc(it.get("url")), "0条",
                *([""] * 15),
            ])
            continue
        for cm in comments:
            aggregate_key = cm.get("_aggregate_key", "")
            u = users.get(aggregate_key, {"high": 0, "total": 0, "times": [], "works": set(), "high_texts": []})
            times = sorted(u["times"])
            works = sorted(u["works"])
            rows.append([
                _esc(it.get("kind")), _esc(it.get("pid")), _esc(it.get("title")),
                _esc(content), _esc(it.get("author")), _esc(it.get("pub_str")),
                _minute_time(it.get("collected_at")), _esc(it.get("url")),
                f"{len(comments)}条",
                _esc(cm.get("region")), _esc(cm.get("uid")), _esc(cm.get("nickname")),
                _esc(cm.get("homepage")), _esc(cm.get("text")), _esc(cm.get("digg")),
                _esc(cm.get("ctime_str")), cm.get("intent_score", ""),
                _esc(cm.get("intent_label")),
                u["high"], u["total"], times[-1] if times else "", len(u["works"]),
                "；".join(u["high_texts"])[:300], "；".join(works)[:300],
            ])
    _w(path, header, rows)
    return {
        "path": path,
        "n_videos": len(items),
        "n_comments": sum(len(it.get("comments", [])) for it in items),
        "n_users": sum(1 for u in users.values() if u["high"] > 0),
    }


def _w(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _esc(s):
    if s is None:
        return ""
    text = str(s)
    # 防止来自平台评论/昵称的内容被 Excel 当成公式执行。
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def export(items, outdir, task="task"):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    items = filter_valid_items(items)
    score_items(items)

    # ---- 1. videos.csv 作品清单 ----
    vh = ["平台", "视频ID", "标题", "作者", "发布时间", "采集时间", "URL", "评论状态"]
    vrows = [[_esc(it.get("kind")), _esc(it["pid"]), _esc(it["title"]),
              _esc(it["author"]), _esc(it["pub_str"]), _minute_time(it.get("collected_at")), _esc(it["url"]),
              f"{len(it['comments'])}条"] for it in items]
    vpath = os.path.join(outdir, f"{task}_videos.csv")
    _w(vpath, vh, vrows)

    # ---- 2. comments.csv 全部评论 ----
    ch = ["采集时间", "地区", "用户ID", "昵称", "评论内容", "点赞数", "评论时间", "来源作品", "作品链接"]
    crows = []
    for it in items:
        for index, cm in enumerate(it["comments"]):
            if not cm["text"]:
                continue
            crows.append([_minute_time(it.get("collected_at")), _esc(cm["region"]), _esc(cm["uid"]), _esc(cm["nickname"]),
                          _esc(cm["text"]), _esc(cm["digg"]), _esc(cm["ctime_str"]),
                          _esc(it["title"]), _esc(it["url"])])
    cpath = os.path.join(outdir, f"{task}_comments.csv")
    _w(cpath, ch, crows)

    # ---- 3. intent_leads.csv 意向评论明细 ----
    ih = ["采集时间", "地区", "用户ID", "用户主页", "评论内容", "意向评分", "意向评级", "昵称", "评论时间", "来源作品", "作品链接"]
    irows = []
    for it in items:
        for cm in it["comments"]:
            if not cm["text"]:
                continue
            irows.append([_minute_time(it.get("collected_at")), _esc(cm["region"]), _esc(cm["uid"]), _esc(cm["homepage"]),
                          _esc(cm["text"]), cm["intent_score"], cm["intent_label"],
                          _esc(cm["nickname"]),
                          _esc(cm["ctime_str"]), _esc(it["title"]), _esc(it["url"])])
    ipath = os.path.join(outdir, f"{task}_intent_leads.csv")
    _w(ipath, ih, irows)

    # ---- 4. user_leads.csv 高意向用户聚合（按 uid 去重，只留高/中意向）----
    uh = ["采集时间", "用户ID", "昵称", "用户主页", "地区", "高意向评论数", "评论总条数", "最新评论时间", "涉及作品数", "高意向评论摘录", "涉及作品列表"]
    users = {}
    for it in items:
        for index, cm in enumerate(it["comments"]):
            if not cm["text"]:
                continue
            uid = cm["uid"]
            key = (f"uid:{uid}" if uid else
                   f"home:{cm['homepage']}" if cm["homepage"] else
                   f"anonymous:{it.get('pid', '')}:{index}")
            u = users.setdefault(key, {
                "uid": uid,
                "nickname": cm["nickname"], "homepage": cm["homepage"], "region": set(),
                "high": 0, "medium": 0, "total": 0, "times": [], "works": set(),
                "high_texts": [], "collection_times": set(),
            })
            if not u["nickname"]:
                u["nickname"] = cm["nickname"]
            if cm["region"]:
                u["region"].add(cm["region"])
            if cm["intent_label"] == "高意向":
                u["high"] += 1
                u["high_texts"].append(cm["text"])
            if cm["intent_label"] == "中意向":
                u["medium"] += 1
            u["total"] += 1
            if cm["ctime_str"]:
                u["times"].append(cm["ctime_str"])
            u["works"].add(it["title"] or it["url"])
            if it.get("collected_at"):
                u["collection_times"].add(_minute_time(it["collected_at"]))
    urows = []
    for _key, u in users.items():
        # 只聚合有高意向的用户（设计文档 §7A.1：只留 ≥5 分）
        if u["high"] == 0:
            continue
        times_sorted = sorted(u["times"])
        urows.append(["、".join(sorted(u.get("collection_times", set()))), u["uid"], u["nickname"], u["homepage"], "、".join(sorted(u["region"])),
                      u["high"], u["total"],
                      times_sorted[-1] if times_sorted else "",
                      len(u["works"]),
                      "；".join(u["high_texts"])[:300],
                      "；".join(list(u["works"]))[:300]])
    urows.sort(key=lambda r: -int(r[5]))  # 高意向数降序
    upath = os.path.join(outdir, f"{task}_user_leads.csv")
    _w(upath, uh, urows)

    return {
        "videos": vpath, "comments": cpath,
        "intent_leads": ipath, "user_leads": upath,
        "n_videos": len(items), "n_comments": len(crows), "n_users": len(urows),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="douyin", choices=["douyin", "dy", "xhs", "xiaohongshu"])
    ap.add_argument("--input", required=True)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--task", default=None, help="CSV 文件名前缀，默认取平台")
    args = ap.parse_args()
    items = load_items(args.platform, args.input)
    task = args.task or ("douyin" if args.platform in ("douyin", "dy") else "xhs")
    outdir = args.outdir or os.path.dirname(args.input)
    res = export(items, outdir, task)
    print("✅ 导出完成")
    for k in ("videos", "comments", "intent_leads", "user_leads"):
        print(f"   {k}: {res[k]}")
    print(f"   作品 {res['n_videos']} | 评论 {res['n_comments']} | 聚合用户 {res['n_users']}")


if __name__ == "__main__":
    main()
