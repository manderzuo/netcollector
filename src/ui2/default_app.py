# -*- coding: utf-8 -*-
"""2.1.1 默认桌面入口。

这个入口把 QML 界面和本地后台服务装配在同一个进程中：后台服务仍然通过
本机回环 TCP 与界面通信，但不再要求用户先手动启动 ``backend_app.py``。
这样启动器只需要拉起一个应用进程，界面切页不会重新创建 Tk 页面，也不会
在 Qt 主线程中等待数据库或浏览器请求。

旧版 ``src/gui.py`` 保留不动，便于需要时回退排查。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
from datetime import datetime

try:
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QFont, QGuiApplication, QIcon
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle
except ImportError as exc:  # pragma: no cover - 取决于运行环境
    raise SystemExit(
        "2.1.1 界面需要 PySide6，请先安装 requirements-v2.txt；"
        "旧版入口仍可使用。"
    ) from exc

if getattr(sys, "frozen", False):
    # PyInstaller 的入口脚本在发布包根目录下运行，不能再按源码文件的
    # ``src/ui2`` 层级反推项目根目录。资源与可写数据也要分开：资源从
    # _MEIPASS 读取（兼容 one-file），数据写到 EXE 所在目录（onedir）。
    PROJECT_ROOT = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_ROOT = getattr(sys, "_MEIPASS", PROJECT_ROOT)
    HERE = os.path.join(RESOURCE_ROOT, "src")
else:
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    PROJECT_ROOT = os.path.dirname(HERE)
    RESOURCE_ROOT = PROJECT_ROOT
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from app_version import APP_VERSION  # type: ignore
    from backend_app import DEFAULT_DB, DEFAULT_ENDPOINT, start_service  # type: ignore
    from ui2.bridge import Ui2Bridge  # type: ignore
    from ui2.qml_app import QmlBridge  # type: ignore
except ImportError:  # pragma: no cover - 支持以包形式导入
    from ..app_version import APP_VERSION  # type: ignore
    from ..backend_app import DEFAULT_DB, DEFAULT_ENDPOINT, start_service  # type: ignore
    from .bridge import Ui2Bridge  # type: ignore
    from .qml_app import QmlBridge  # type: ignore


HEARTBEAT_PATH = os.path.join(PROJECT_ROOT, "data", "gui_heartbeat.txt")
_NATIVE_ICON_HANDLES: list[int] = []


def write_heartbeat(path: str = HEARTBEAT_PATH) -> None:
    """写入供监控器读取的心跳，调用点始终位于 Qt 主线程。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    content = (
        f"pid={os.getpid()} "
        f"ts={datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
        f"threads={threading.active_count()} mode={APP_VERSION}\n"
    )
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except OSError:
        # 心跳不能影响界面；监控器会在下一次周期继续读取旧值。
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass


def apply_windows_dark_titlebar(window) -> None:
    """把系统原生标题栏切换为工作台深色主题，同时保留原生拖拽和缩放。"""
    if sys.platform != "win32":
        return
    try:
        hwnd = int(window.winId())
        dwmapi = ctypes.WinDLL("dwmapi")
        set_attribute = dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        set_attribute.restype = ctypes.c_long

        # Windows 10/11 对应的深色模式属性编号不同，两个都尝试以兼容旧版本。
        dark = ctypes.c_int(1)
        for attribute in (20, 19):
            if set_attribute(hwnd, attribute, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                break

        # DWM 颜色使用 COLORREF（0x00BBGGRR），与 QML 的 #RRGGBB 顺序相反。
        for attribute, color in (
            (34, 0x00523926),  # 边框 #263952
            (35, 0x0027170D),  # 标题栏 #0D1727
            (36, 0x00FFF7F2),  # 标题文字 #F2F7FF
        ):
            value = ctypes.c_uint32(color)
            set_attribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        # 通知 Windows 立即重绘非客户区，否则颜色可能要到下一次窗口操作
        # 才显示出来。
        user32 = ctypes.WinDLL("user32")
        set_window_pos = user32.SetWindowPos
        set_window_pos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_uint,
        ]
        set_window_pos.restype = ctypes.c_bool
        set_window_pos(hwnd, None, 0, 0, 0, 0, 0x27)
    except (AttributeError, OSError, TypeError, ValueError):
        # 系统不支持 DWM 颜色时保留 Qt 默认窗口，不影响程序启动。
        return


def set_windows_app_user_model_id() -> None:
    """避免 Windows 把任务栏图标归到 Python 默认应用下。"""
    if sys.platform != "win32":
        return
    try:
        shell32 = ctypes.WinDLL("shell32")
        set_app_id = shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        set_app_id("DouyinXhsCollector.Workbench.2")
    except (AttributeError, OSError, TypeError, ValueError):
        return


def apply_windows_window_icon(window, icon_path: str) -> None:
    """把 ICO 同时写入窗口的大/小图标，避免任务栏回退到 python.exe。"""
    if sys.platform != "win32" or not os.path.exists(icon_path):
        return
    try:
        user32 = ctypes.WinDLL("user32")
        load_image = user32.LoadImageW
        load_image.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        load_image.restype = ctypes.c_void_p
        send_message = user32.SendMessageW
        send_message.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        send_message.restype = ctypes.c_void_p
        hwnd = int(window.winId())
        # IMAGE_ICON=1，LR_LOADFROMFILE=0x10。
        big = load_image(None, icon_path, 1, 32, 32, 0x10)
        small = load_image(None, icon_path, 1, 16, 16, 0x10)
        if big:
            send_message(hwnd, 0x0080, 1, big)  # WM_SETICON / ICON_BIG
            _NATIVE_ICON_HANDLES.append(int(big.value))
        if small:
            send_message(hwnd, 0x0080, 0, small)  # WM_SETICON / ICON_SMALL
            _NATIVE_ICON_HANDLES.append(int(small.value))
    except (AttributeError, OSError, TypeError, ValueError):
        return


def restore_window_to_visible_screen(window) -> None:
    """把主窗口限制在当前主屏，并修正多屏布局导致的屏外位置。

    Qt/Windows 可能沿用上一次的窗口位置。显示器拔出、远程桌面切换或
    分辨率变化后，窗口仍可能保留负坐标，进程和心跳都正常但用户看不到
    界面。正常尺寸且仍在当前主屏内的用户布局不会被覆盖；只有尺寸超出
    屏幕或窗口已经移到屏外时才会重新居中。
    """
    try:
        app = QGuiApplication.instance()
        primary = app.primaryScreen() if app is not None else None
        if primary is None:
            return
        available = primary.availableGeometry()
        frame = window.frameGeometry()
        intersection = frame.intersected(available)
        # 默认窗口为总览首屏预留了更大的工作区，但在小分辨率或远程
        # 桌面环境下必须先按可用屏幕尺寸收缩，避免窗口打开后被截断。
        width = min(max(window.width(), 1100), available.width())
        height = min(max(window.height(), 720), available.height())
        size_changed = window.width() != width or window.height() != height
        if intersection.width() >= 80 and intersection.height() >= 80 and not size_changed:
            return

        x = available.left() + max(0, (available.width() - width) // 2)
        y = available.top() + max(0, (available.height() - height) // 2)
        window.setGeometry(x, y, width, height)
    except (AttributeError, TypeError, ValueError):
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"多平台采集工作台 {APP_VERSION} 默认界面")
    parser.add_argument("--db", default=os.path.join(PROJECT_ROOT, "data", "platform_gui.db"), help=argparse.SUPPRESS)
    parser.add_argument("--endpoint", default=os.path.join(PROJECT_ROOT, "data", "backend_endpoint.json"), help=argparse.SUPPRESS)
    parser.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--snapshot-interval", type=float, default=0.5, help=argparse.SUPPRESS)
    # 只供自动化冒烟测试使用，不影响日常启动。
    parser.add_argument("--quit-after-ms", type=int, default=0, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 心跳与运行端点放在同一目录。这样离屏冒烟测试使用临时 endpoint
    # 时不会覆盖正式 data/gui_heartbeat.txt，避免监控误判或拉起第二个 GUI。
    heartbeat_path = os.path.join(
        os.path.dirname(os.path.abspath(args.endpoint)), "gui_heartbeat.txt"
    ) if args.endpoint else HEARTBEAT_PATH
    runtime = None
    service = None
    client_bridge = None
    heartbeat_timer = None
    try:
        # 服务先启动并写出 endpoint，界面随后异步连接；任何网络/数据库慢操作
        # 都不占用 Qt 主线程。
        runtime, service = start_service(
            args.db,
            endpoint_path=args.endpoint,
            demo=args.demo,
            snapshot_interval=args.snapshot_interval,
        )
        client_bridge = Ui2Bridge.from_endpoint(service.endpoint)

        QQuickStyle.setStyle("Basic")
        # 必须在创建 QGuiApplication 前设置 AppUserModelID，Windows 才不会
        # 继续使用 python.exe 的默认任务栏图标。
        set_windows_app_user_model_id()
        app = QGuiApplication([sys.argv[0]])
        # 统一使用中文界面字体，避免系统默认字体缺少中文字形时出现方框。
        app.setFont(QFont("Microsoft YaHei UI", 10))
        # 使用发布包内的用户版高清工作台图标，保证窗口标题栏和任务栏图标
        # 与左侧品牌图标一致；不依赖用户电脑上的 Pictures 路径。
        app_icon_path = os.path.join(RESOURCE_ROOT, "assets", "user_app_icon.ico")
        if os.path.exists(app_icon_path):
            app.setWindowIcon(QIcon(app_icon_path))
        engine = QQmlApplicationEngine()
        qml_bridge = QmlBridge(client_bridge)
        engine.rootContext().setContextProperty("backend", qml_bridge)
        qml_path = os.path.join(RESOURCE_ROOT, "src", "ui2", "qml", "main.qml")
        engine.load(QUrl.fromLocalFile(qml_path))
        if not engine.rootObjects():
            return 2
        root_window = engine.rootObjects()[0]
        root_window.setProperty("appVersion", APP_VERSION)
        root_window.setIcon(QIcon(app_icon_path))
        apply_windows_window_icon(root_window, app_icon_path)
        apply_windows_dark_titlebar(root_window)
        restore_window_to_visible_screen(root_window)
        root_window.show()
        # ApplicationWindow 的原生句柄在首轮布局后最稳定，再补一次主题属性，
        # 不使用无边框窗口，避免破坏系统级拖拽、边框缩放和窗口按钮。
        QTimer.singleShot(0, lambda: apply_windows_dark_titlebar(root_window))
        QTimer.singleShot(100, lambda: apply_windows_window_icon(root_window, app_icon_path))
        QTimer.singleShot(0, lambda: restore_window_to_visible_screen(root_window))

        # 心跳由 Qt 主线程的定时器写入。若页面事件循环卡住，时间戳会停在
        # 上一次，监控器即可识别真实 UI 假死，而不是仅凭进程仍存活误判。
        heartbeat_timer = QTimer()
        heartbeat_timer.setInterval(1000)
        heartbeat_timer.timeout.connect(lambda: write_heartbeat(heartbeat_path))
        heartbeat_timer.start()
        write_heartbeat(heartbeat_path)

        # 连接后台后尝试恢复“记住登录状态”的会话；恢复成功会沿用
        # 正常登录回调，重新设置员工数据范围并拉取账号/任务/线索快照。
        client_bridge.connect_async(lambda _view: qml_bridge.restoreAuthSession())
        if args.quit_after_ms > 0:
            QTimer.singleShot(args.quit_after_ms, app.quit)
        return int(app.exec())
    finally:
        if heartbeat_timer is not None:
            heartbeat_timer.stop()
        if client_bridge is not None:
            client_bridge.close()
        if service is not None:
            service.stop(shutdown_scheduler=False)
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
