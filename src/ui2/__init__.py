# -*- coding: utf-8 -*-
"""2.0 PySide6/QML 界面实验区。

这里不依赖 tkinter，也不改变 1.2 的旧入口；页面通过 backend_client
访问本地任务服务。
"""

from .state import Ui2State

__all__ = ["Ui2State"]

