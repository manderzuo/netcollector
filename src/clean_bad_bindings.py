# -*- coding: utf-8 -*-
import sqlite3
import os
db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "platform_gui.db")
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
print("=== 恢复后 accounts ===")
for r in con.execute("SELECT id,name,bb_window_id,platform FROM accounts"):
    print(dict(r))
cur = con.execute("DELETE FROM accounts WHERE name='未登录'")
print("删除错误绑定(未登录):", cur.rowcount)
con.commit()
print("=== 清理后 ===")
for r in con.execute("SELECT id,name,bb_window_id,platform FROM accounts"):
    print(dict(r))
con.close()
