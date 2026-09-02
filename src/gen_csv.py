#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_csv.py — 从指定的 notes3.json 生成最终意向客户 CSV（7 列）。

用法: python gen_csv.py --json out/干洗店/notes3.json --out out/洗衣洗鞋店/xhs_意向客户.csv
"""

import argparse
import csv
import json
import os
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HIGH_KW = ["怎么联系", "联系方式", "私信", "多少钱", "多少米", "怎么卖", "怎么买", "哪里买", "哪里", "购买", "入手", "加盟", "代理", "合作", "招商", "报价", "询价", "落地", "回头", "做代理", "货源", "厂家", "能做", "想开", "想加盟", "求购", "要买", "设备", "多少钱一台", "怎么收费", "培训", "学技术", "去哪学", "教"]
MEDIUM_KW = ["能不能赚", "赚钱吗", "利润", "回本", "成本", "房租", "面积", "选址", "怎么开", "流程", "效果好吗", "好用吗", "卫生", "安全", "多久", "资质", "营业执照", "办证", "怎么学"]
UNWANTED = ["招人", "找工作", "兼职", "招聘"]


def rate(text):
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return "low"
    if any(k in text for k in UNWANTED):
        return "recruit"
    if any(k in text for k in HIGH_KW):
        return "high"
    if any(k in text for k in MEDIUM_KW):
        return "medium"
    return "low"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=os.path.join(PROJECT_ROOT, "data", "notes3.json"))
    ap.add_argument("--out", default=os.path.join(PROJECT_ROOT, "data", "xhs_意向客户.csv"))
    args = ap.parse_args()
    notes = json.load(open(args.json, encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = ["信息提取时间", "地区", "用户名", "用户主页", "发布时间", "发布内容", "意向评级", "整体评论内容分析"]
    rows = []
    for n in notes:
        title = n.get("title", "").strip()
        pub = n.get("pub_time") or n.get("time") or ""
        content = title or (n.get("desc", "") or "").split("\n")[0][:60]
        for cm in n["comments"]:
            user = str(cm.get("user", "") or cm.get("nickname", "")).strip()
            text = cm.get("text", "")
            if not isinstance(text, str):
                text = str(text)
            text = text.strip()
            if not user or not text:
                continue
            # 评论者主页：优先用评论自带 homepage / user_id
            home = cm.get("homepage", "") or (("https://www.xiaohongshu.com/user/profile/" + str(cm["user_id"])) if cm.get("user_id") else "")
            rows.append([now, cm.get("region", ""), user, home, pub, content, rate(text), text])
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    rating = {}
    region_cnt = {}
    for r in rows:
        rating[r[6]] = rating.get(r[6], 0) + 1
        region_cnt[r[1]] = region_cnt.get(r[1], 0) + 1
    print(f"✅ CSV: {args.out}")
    print(f"   总评论 {len(rows)} | 意向分布: {rating}")
    print(f"   地区分布: {region_cnt}")
    high = [r for r in rows if r[6] == "high"]
    print(f"   高意向 {len(high)} 条，示例:")
    for r in high[:15]:
        print(f"     【{r[2]}】{r[7][:45]}")


if __name__ == "__main__":
    main()
