
import time, json, os, re, glob
OUT_DIR="out"
KEYWORDS=["干洗店","快递柜","AI漫剧","穿越剧","短剧创作","短剧道具制作","快递加盟","京东家政","家政门店","无人快递柜加盟","幼儿园招生","美食推荐","云台","稳定器","私人定制","新疆旅游","机器人运动会","自动驾驶","科普好物","三折叠","惠济区幼儿园"]
while True:
    time.sleep(20*60)
    try:
        import glob, json, os, re
        outs=glob.glob(os.path.join(OUT_DIR,"*_comments.json"))
        print("[REPORT %s] 已完成 %d/21 关键词" % (time.strftime("%H:%M:%S"), len(outs)))
        for p in sorted(outs):
            head=open(p,'r',encoding='utf-8').read(4096)
            m1=re.search(r'"keyword"\s*:\s*"([^"]+)"', head)
            m2=re.search(r'"total_items"\s*:\s*(\d+)', head)
            m3=re.search(r'"total_comments"\s*:\s*(\d+)', head)
            kw=m1.group(1) if m1 else os.path.basename(p)
            c=int(m2.group(1)) if m2 else 0
            cm=int(m3.group(1)) if m3 else 0
            print("  %s: %d items %d comments" % (kw,c,cm))
    except Exception as e:
        print("REPORT ERR", e)
