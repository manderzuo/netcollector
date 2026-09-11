# -*- coding: utf-8 -*-
"""2.2.4 独立后台服务启动入口。

后台进程负责持有 Scheduler、浏览器采集器和 SQLite 连接；GUI 只通过
``backend_protocol`` 访问它。默认不启动演示数据，也不会清理现有数据库。
V2.2.4 默认桌面入口 ``src/ui2/default_app.py`` 会在同一进程内装配本服务；
本文件仍可单独启动后台，供接口调试和部署验证使用。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from .app_version import APP_VERSION  # type: ignore
    from .backend_service import BackendService  # type: ignore
    from .config_loader import AppConfig  # type: ignore
    from .scheduler import FakeCollector, Scheduler  # type: ignore
    from .tieba_adapter import TiebaApiCollector  # type: ignore
except ImportError:  # pragma: no cover - 支持 python src/backend_app.py
    from app_version import APP_VERSION  # type: ignore
    from backend_service import BackendService  # type: ignore
    from config_loader import AppConfig  # type: ignore
    from scheduler import FakeCollector, Scheduler  # type: ignore
    from tieba_adapter import TiebaApiCollector  # type: ignore


DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "platform_gui.db")
DEFAULT_ENDPOINT = os.path.join(PROJECT_ROOT, "data", "backend_endpoint.json")


@dataclass
class BackendRuntime:
    """后台资源集合，便于启动器和测试统一释放资源。"""

    scheduler: Scheduler
    collector: object
    browser: object | None = None

    def close(self) -> None:
        try:
            self.scheduler.shutdown(close_connections=True)
        finally:
            # Scheduler 负责取消任务；平台采集器还拥有 asyncio 线程，需要单独收尾。
            shutdown = getattr(self.collector, "shutdown", None)
            if callable(shutdown):
                try:
                    shutdown()
                except Exception:
                    pass


def build_runtime(db_path: str = DEFAULT_DB, *, demo: bool = False) -> BackendRuntime:
    """创建与生产后台一致的调度器，不启动网络服务。"""
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    if demo:
        collector = FakeCollector(video_count=25)
        scheduler = Scheduler(db_path, bb=None, collector=collector)
        for index in range(1, 4):
            scheduler.add_account(
                f"演示账号{index}",
                bb_window_id=f"demo-{index}",
                platform="douyin",
            )
        scheduler.cooldown_handler = lambda _account, _seconds: True
        return BackendRuntime(scheduler=scheduler, collector=collector)

    from bitbrowser import BitBrowserClient
    from live_collector import HybridCollector, LiveCollector
    from config_loader import AppConfig

    config = AppConfig().bitbrowser()
    browser = BitBrowserClient(
        base_url=config.get("base_url") or "http://127.0.0.1:54345",
        timeout=float(config.get("timeout") or 20),
    )
    tieba_config = AppConfig().tieba_api() or {}
    tieba = TiebaApiCollector(
        token=str(tieba_config.get("token") or ""),
        timeout=float(tieba_config.get("timeout") or 30),
    )
    collector = HybridCollector(
        LiveCollector(platforms=("douyin", "xhs", "kuaishou"), bb=browser),
        bb=browser, tieba=tieba,
    )
    scheduler = Scheduler(db_path, bb=browser, collector=collector)
    return BackendRuntime(scheduler=scheduler, collector=collector, browser=browser)


def start_service(
    db_path: str = DEFAULT_DB,
    *,
    endpoint_path: str = DEFAULT_ENDPOINT,
    demo: bool = False,
    snapshot_interval: float = 0.5,
) -> tuple[BackendRuntime, BackendService]:
    """创建并启动后台服务，返回资源对象和服务对象。"""
    runtime = build_runtime(db_path, demo=demo)
    try:
        service = BackendService(
            runtime.scheduler,
            endpoint_path=os.path.abspath(endpoint_path),
            snapshot_interval=snapshot_interval,
        )
        service.start()
        return runtime, service
    except Exception:
        runtime.close()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"多平台采集工作台 {APP_VERSION} 独立后台服务")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="服务地址 JSON 文件")
    parser.add_argument("--demo", action="store_true", help="使用临时演示采集器")
    parser.add_argument("--snapshot-interval", type=float, default=0.5)
    args = parser.parse_args(argv)

    runtime, service = start_service(
        args.db,
        endpoint_path=args.endpoint,
        demo=args.demo,
        snapshot_interval=args.snapshot_interval,
    )
    print(
        f"后台服务已启动：{service.endpoint.host}:{service.endpoint.port} "
        f"endpoint={os.path.abspath(args.endpoint)}",
        flush=True,
    )
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0
    finally:
        service.stop(shutdown_scheduler=False)
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
