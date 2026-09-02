#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore_bindings.py — 逐个 BitBrowser 窗口检查登录状态，恢复账号绑定。

遍历 BitBrowser 所有窗口：打开 → 读登录账号昵称/ID → 绑定到 scheduler accounts。
绑定信息持久化，供 GUI 下次启动显示「已绑定」。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bitbrowser import BitBrowserClient  # noqa: E402
from cdp import CdpSession  # noqa: E402
import account_reader  # noqa: E402
import db  # noqa: E402
from scheduler import Scheduler, FakeCollector  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "platform_gui.db")


def platform_of(win):
    """从 BitBrowser 窗口字段判断平台。"""
    p = (win.get("platform") or "").lower()
    nm = win.get("name") or ""
    if "xiaohongshu" in p or "小红" in nm:
        return "xhs"
    if "douyin" in p or "抖音" in nm or "douyin" in nm.lower():
        return "douyin"
    return ""


def open_and_read(bb, wid, plat, name):
    """打开窗口并读登录账号。返回 dict 或 None（失败/未登录）。"""
    try:
        page_url = ("https://creator.xiaohongshu.com/new/note-manager?source=official" if plat == "xhs"
                    else "https://www.douyin.com/user/self?from_nav=1")
        # 若已打开，先拿 ws；否则打开
        d = bb.open_browser(wid, ignore_default_urls=True)
        ws = d.get("ws")
        # 写窗口文件（供后续）
        fname = "xhs_window.txt" if plat == "xhs" else "dy_window.txt"
        with open(os.path.join(PROJECT_ROOT, "data", fname), "w", encoding="utf-8") as f:
            f.write(wid + "\n" + ws + "\n")
        time.sleep(1)
        # 读账号
        try:
            loop = __import__("asyncio").new_event_loop()
            async def _nav_read():
                c = CdpSession(ws)
                await c.connect()
                sid = await c.attach_page()
                # 导航到平台页（确保在平台域内）
                try:
                    await c.cmd("Page.navigate", {"url": page_url}, session_id=sid)
                except Exception:
                    pass
                await __import__("asyncio").sleep(6)
                await c.close()
            loop.run_until_complete(_nav_read())
            loop.close()
        except Exception:
            pass
        acc = account_reader.read_account(plat, ws)
        return acc
    except Exception as e:
        return {"error": str(e)[:80]}


def main():
    db.init_db(DB_PATH)
    sched = Scheduler(DB_PATH, bb=None, collector=FakeCollector(10))
    bb = BitBrowserClient()
    d = bb.list_browsers(page=0, page_size=100)
    wins = d.get("list", [])
    print(f"=== 共 {len(wins)} 个窗口，逐个检查登录态 ===")
    restored = 0
    for w in wins:
        wid = w.get("id")
        nm = w.get("name") or ""
        plat = platform_of(w)
        print(f"\n窗口: {nm} | id={wid} | 平台={plat or '未知'}")
        if not plat:
            print("  平台未知，跳过")
            continue
        acc = open_and_read(bb, wid, plat, nm)
        if acc.get("error"):
            print(f"  打开/读取失败: {acc.get('error')}")
            continue
        if not acc.get("logged_in"):
            print("  该窗口未登录")
            continue
        nick = acc.get("nick") or acc.get("uid") or acc.get("sec_uid") or ""
        print(f"  已登录: 昵称/ID = {nick}")
        if not nick:
            nick = nm
        # 绑定（name=nick，bb_window_id=wid，platform=plat）
        try:
            sched.add_account(nick, bb_window_id=wid, platform=plat)
            print(f"  ✅ 已绑定账号: {plat}·{nick}")
            restored += 1
        except Exception as e:
            print(f"  绑定失败: {e}")
    # 汇总
    rep = sched.status_report()
    print("\n=== 恢复后已绑定账号 ===")
    for name, a in rep.get("accounts", {}).items():
        print(f"  {name} | platform={a['platform']} | window={a.get('bb_window_id')}")
    print(f"\n恢复完成，共绑定 {restored} 个账号")
    sched.shutdown()


if __name__ == "__main__":
    main()
