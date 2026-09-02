# -*- coding: utf-8 -*-
"""update_sign.py — 抖音签名失效检测 + 常量提取比对工具。

两个子命令：
  1. --probe    检测当前签名是否仍有效（用真实 cookie 打线上接口）
  2. --extract  从最新 webmssdk.js 提取关键常量，与当前 ab_pure.py 比对

用法：
  # 检测签名是否失效
  python update_sign.py --probe --aweme-id 7584458335752834340 --cookie "ttwid=xxx; ..."

  # 提取新 JS 里的常量并比对
  python update_sign.py --extract --js webmssdk_new.js

  # 两者都做
  python update_sign.py --probe --aweme-id 123 --cookie "..." --extract --js webmssdk_new.js
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from dy_sign.ab_pure import ABogusPureSigner
from dy_sign.dy_sign_service import DySignService

COMMENT_API = "https://www.douyin.com/aweme/v1/web/comment/list/"

# ab_pure.py 里需要比对的常量（从当前实现里读出来做基准）
CURRENT_CONSTANTS = {
    "SALT": "dhzx",
    "BDMS_VERSION": "1.0.1.19-fix.01",
    "FORTNIGHT_EPOCH": 1721836800000,
    "RC4_KEY_BYTE": 211,
    "ALPHABET_S3": "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe",
    "ALPHABET_S4": "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe",
}


# ---------------------------------------------------------------------------
# 子命令 1：探测签名是否有效
# ---------------------------------------------------------------------------
def probe_sign(aweme_id: str, cookie: str, sign_service: str = "") -> dict:
    """用当前签名请求评论接口，判断是否被接受。"""
    signer = DySignService(fixed=False)

    # 构造 URL
    params = urllib.parse.urlencode({
        "aweme_id": aweme_id,
        "cursor": "0",
        "count": "20",
        "item_type": "0",
        "device_platform": "webapp",
        "aid": "6383",
    })
    url = f"{COMMENT_API}?{params}"

    # 生成签名（本地直接算，不走 HTTP 服务，保证测的是算法本身）
    a_bogus = signer.sign_a_bogus(url)
    full = f"{url}&a_bogus={urllib.parse.quote(a_bogus, safe='')}"

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/148.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie,
    }
    try:
        req = urllib.request.Request(full, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"verdict": "NETWORK_ERROR", "detail": str(exc), "status": None}

    verdict = "UNKNOWN"
    if status == 200:
        try:
            data = json.loads(body)
            n = len(data.get("comments") or [])
            verdict = "PASS" if n >= 0 else "PASS_EMPTY"
            return {"verdict": verdict, "status": 200, "comments": n,
                    "has_more": data.get("has_more")}
        except json.JSONDecodeError:
            # 200 但非 JSON：可能是登录跳转/风控 HTML 页。不能算有效。
            verdict = "SUSPECT_200_NONJSON"
            return {"verdict": verdict, "status": 200, "body_preview": body[:200]}
    if status == 403:
        verdict = "FAIL_403"
    elif status in (418, 429):
        verdict = "FAIL_RATE_LIMIT"
    elif status in (401, 302):
        verdict = "FAIL_AUTH"
    else:
        verdict = f"FAIL_HTTP_{status}"
    return {"verdict": verdict, "status": status, "body_preview": body[:300]}


# ---------------------------------------------------------------------------
# 子命令 2：从 webmssdk.js 提取常量
# ---------------------------------------------------------------------------
def extract_constants(js_path: str) -> dict:
    """扫描 webmssdk.js 提取关键常量（尽力而为，混淆限制下是辅助）。"""
    with open(js_path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    found = {}
    # 1. 盐值：明文 dhzx 或 hex 形式
    if "dhzx" in src:
        found["SALT"] = "dhzx (明文找到)"
    elif re.search(r"\\x64\\x68\\x7a\\x78", src):  # dhzx hex
        found["SALT"] = "dhzx (hex形式找到)"
    else:
        found["SALT"] = "未找到(可能在混淆数组里)"

    # 2. 字母表
    for name, table in (("ALPHABET_S3", CURRENT_CONSTANTS["ALPHABET_S3"]),
                        ("ALPHABET_S4", CURRENT_CONSTANTS["ALPHABET_S4"])):
        if table in src:
            found[name] = "与当前一致"
        else:
            # 找前 8 字符
            head = table[:8]
            found[name] = f"未找到旧表(头8字符 {head} 不在JS里，可能已更换)"

    # 3. 版本号
    ver_patterns = re.findall(r'["\'](\d+\.\d+\.\d+\.\d+[^"\']*)["\']', src)
    found["BDMS_VERSION_candidates"] = ver_patterns[:5] if ver_patterns else "未找到"

    # 4. RC4 key（211 = 0xD3）
    found["RC4_KEY_BYTE_0xd3"] = "0xd3(211) 出现" if "0xd3" in src.lower() else "未直接出现"

    return found


def diff_constants(found: dict) -> list:
    """对比提取结果与当前常量，输出差异清单。"""
    issues = []
    for key, val in found.items():
        if isinstance(val, str) and "未" in val and key != "SALT":
            issues.append(f"[变化?] {key}: {val}")
    return issues


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="抖音签名失效检测 + 常量提取")
    ap.add_argument("--probe", action="store_true", help="检测当前签名是否有效")
    ap.add_argument("--aweme-id", default="", help="真实视频ID")
    ap.add_argument("--cookie", default="", help="真实登录 cookie")
    ap.add_argument("--extract", action="store_true", help="从 webmssdk.js 提取常量")
    ap.add_argument("--js", default="", help="webmssdk.js 路径")
    args = ap.parse_args()

    if not args.probe and not args.extract:
        ap.print_help()
        return

    if args.probe:
        if not args.aweme_id or not args.cookie:
            print("[ERROR] --probe 需要 --aweme-id 和 --cookie")
            return
        print("[1/2] 探测签名有效性 ...")
        result = probe_sign(args.aweme_id, args.cookie)
        verdict = result.get("verdict")
        print(f"      判定: {verdict}  (HTTP {result.get('status')})")
        if result.get("comments") is not None:
            print(f"      返回评论数: {result['comments']}, has_more: {result.get('has_more')}")
        if result.get("body_preview"):
            print(f"      响应: {result['body_preview'][:200]}")
        if verdict == "PASS":
            print("\n[结论] 签名仍有效，无需更新。")
        elif verdict == "SUSPECT_200_NONJSON":
            print("\n[结论] 返回 200 但非 JSON（可能是登录跳转/风控 HTML 页）。")
            print("       多半是 cookie 无效/过期，请用真实登录 cookie 重试。")
        else:
            print("\n[结论] 签名可能已失效，按《签名失效重逆向更新手册》更新。")
            print("       提示：403 也可能是 cookie 过期/IP 风控，请换 IP 复测确认。")

    if args.extract:
        if not args.js:
            print("[ERROR] --extract 需要 --js 路径")
            return
        print(f"[2/2] 提取常量 from {args.js} ...")
        found = extract_constants(args.js)
        print("\n=== 常量比对结果 ===")
        for key, val in found.items():
            print(f"  {key:28s}: {val}")
        issues = diff_constants(found)
        if issues:
            print("\n=== 疑似变化项（需人工确认）===")
            for i in issues:
                print("  " + i)
        else:
            print("\n=== 常量与当前实现一致（可能是算法层变化，看手册第4节）===")


if __name__ == "__main__":
    main()
