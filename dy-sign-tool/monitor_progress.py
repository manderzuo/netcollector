import time, re, os
path = "out/京东积分_comments.json"
next_milestone = 140
next_hundred = 200  # already passed 100
print(f"[{time.strftime('%H:%M:%S')}] 监控已重建(极轻量) 下一里程碑:{next_milestone} 每10个播报", flush=True)
while True:
    time.sleep(30)
    try:
        # 只读前4KB, 避免加载5MB大文件
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(4096)
        m1 = re.search(r'"total_items"\s*:\s*(\d+)', head)
        m2 = re.search(r'"total_comments"\s*:\s*(\d+)', head)
        if not m1 or not m2:
            continue
        count = int(m1.group(1))
        comments = int(m2.group(1))
        now = time.strftime("%H:%M:%S")
        if count >= 100 and next_hundred == 200 and count < 200:
            # 100已过, 下一个是200(实际上175就结束)
            pass
        if count >= next_milestone:
            pct = round(count/175*100,1)
            print(f"[{now}] ✅ 里程碑 {count}/175 ({pct}%) 总评论:{comments}", flush=True)
            while next_milestone <= count:
                next_milestone += 10
            if next_milestone > 175:
                next_milestone = 175
            if count >= 175:
                print(f"[{now}] 🎉 全部完成 {count}/175 总评论:{comments}", flush=True)
                break
            print(f"下一里程碑: {next_milestone}", flush=True)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] 读取中... {e}", flush=True)
