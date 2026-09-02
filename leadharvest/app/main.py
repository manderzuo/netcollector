# -*- coding: utf-8 -*-
"""main.py — LeadHarvest 命令行入口。

用法：
  # 创建并启动采集任务（后台调度）
  python -m app.main run --platform douyin --keyword "快递柜" --target 20

  # 数据库初始化
  python -m app.main init-db --db data/leadharvest.db

  # 平台注册表
  python -m app.main platforms
"""

import argparse
import logging
import os
import sys

# 允许从项目根直接运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import __version__  # noqa: E402
from app.database import Database  # noqa: E402
from app.scheduler import Scheduler  # noqa: E402


def _default_db_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data", "leadharvest.db")


def cmd_init_db(args):
    db = Database(args.db)
    conn = db.connect()
    conn.close()
    print(f"[OK] 数据库已初始化: {args.db}")


def cmd_platforms(args):
    from app.platform import PLATFORM_REGISTRY
    print(f"已注册平台 ({len(PLATFORM_REGISTRY)}):")
    for name, cls in PLATFORM_REGISTRY.items():
        print(f"  - {name}: {cls.__name__}")


def cmd_run(args):
    db = Database(args.db)
    db.connect()
    sched = Scheduler(db)

    task_id = sched.create_task(
        keyword=args.keyword,
        platform=args.platform,
        target_count=args.target,
        collect_mode=args.mode,
        batch_size=args.batch_size,
        cooldown_seconds=args.cooldown,
        search_sort=args.sort,
    )
    print(f"[OK] 任务已创建: id={task_id}, platform={args.platform}, "
          f"keyword={args.keyword}, target={args.target}")

    handle = sched.start(task_id)
    print(f"[OK] 任务已启动 (Ctrl+C 停止等待)...")
    try:
        handle.done_event.wait(timeout=args.timeout)
        print(f"[DONE] 任务状态: {handle.status}")
        print(f"       视频: {handle.stats['videos']}, "
              f"评论: {handle.stats['comments']}")
        if handle.error_message:
            print(f"       错误: {handle.error_message}")
    except KeyboardInterrupt:
        sched.stop_task(task_id)
        print("[STOP] 已停止")


def main():
    parser = argparse.ArgumentParser(prog="leadharvest", description="LeadHarvest 采集平台")
    parser.add_argument("--version", action="version", version=f"LeadHarvest {__version__}")
    parser.add_argument("--db", default="", help="SQLite 数据库路径（默认 data/leadharvest.db）")
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init-db", help="初始化数据库")
    p_init.set_defaults(func=cmd_init_db)

    p_plats = sub.add_parser("platforms", help="列出已注册平台")
    p_plats.set_defaults(func=cmd_platforms)

    p_run = sub.add_parser("run", help="运行采集任务")
    p_run.add_argument("--platform", required=True, choices=["douyin", "xhs", "weibo", "bilibili"])
    p_run.add_argument("--keyword", required=True)
    p_run.add_argument("--target", type=int, default=100)
    p_run.add_argument("--mode", default="standard", choices=["fast", "standard", "deep"])
    p_run.add_argument("--batch-size", type=int, default=10)
    p_run.add_argument("--cooldown", type=int, default=75)
    p_run.add_argument("--sort", default="default")
    p_run.add_argument("--timeout", type=int, default=1800)
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    if not getattr(args, "db", None):
        args.db = _default_db_path()
    if not hasattr(args, "func"):
        parser.print_help()
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
