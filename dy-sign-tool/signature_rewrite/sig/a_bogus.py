# -*- coding: utf-8 -*-
"""a_bogus.py — 抖音 a_bogus 签名生成器。

签名结构概述（算法事实，签名有效性所必需）：
  1. 对 URL query 与请求体分别做双重 SM3 摘要（拼接固定盐值）；
  2. 对 User-Agent 做 RC4 变体加密后 Base64，再做 SM3 摘要；
  3. 组装 90 槽位载荷（时间戳、计数器、环境标志、摘要字节、几何、校验和）；
  4. 载荷经混合置换/扩展编码，RC4 变体加密，自定义 Base64 输出。

本实现采用独立命名与结构组织，仅保留算法功能事实。
"""

import random as _rnd
import re
import time as _time
import urllib.parse

from .digest import sm3_digest
from .env_profile import browser_profile

# ---------------------------------------------------------------------------
# 算法常量（功能必需的事实参数）
# ---------------------------------------------------------------------------
_SALT = "dhzx"                                   # 摘要拼接盐值
_FORTNIGHT_EPOCH_MS = 1721836800000              # 双周计数基准时间戳
_CLOSURE_EPOCH_MS = 1720000000000                # 载荷闭合时间基准
_INITIAL_COUNTER = 2                             # 签名计数器初值
_FIXED_TIMESTAMP_MS = 1720000000000              # 固定模式时间戳（调试/确定性）
_FIXED_RANDOM = 0.4142135623730951               # 固定模式随机数（调试/确定性）

_ALPHABET_A = ("ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe")
_ALPHABET_B = ("Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe")

_CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
_FIREFOX_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) "
               "Gecko/20100101 Firefox/117.0")

_GEO_FIXED = (2560, 1297, 2560, 1392, 2560, 1400, 2560, 1440)
_GEO_PLATFORM = "Win32"

_RC4_KEY_BYTE = 211                              # 加密密钥字节

# 载荷字段排列顺序（90 槽位中参与校验和的字段）
_CHECKSUM_FIELDS = (
    24, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 38, 39, 40, 41, 42, 43,
    44, 45, 46, 47, 48, 49, 51, 52, 53, 55, 56, 57, 59, 60, 61, 62, 63, 64,
    65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 79, 80, 84, 85,
)

# 提取顺序（从载荷中挑出的字段参与最终编码）
_EXTRACT_ORDER = (
    34, 44, 56, 61, 73, 29, 70, 45, 35, 49, 38, 66, 51, 68, 28, 48, 64, 47,
    30, 71, 26, 55, 31, 69, 59, 40, 62, 63, 27, 72, 41, 74, 57, 52, 42, 39,
    33, 67, 53, 43, 65, 46, 36, 24, 60, 32, 79, 80, 84, 85,
)

# 浏览器类型识别正则
_BROWSER_PATTERNS = (
    ("Huawei", (r"\bhuawei\b",)),
    ("Chrome", (r"(chrome)\/([\w.]+)(?!.*chromium)",)),
    ("Edge", (r"(edg|edge)\/([\w.]+)",)),
    ("Firefox", (r"\bfocus\/([\w.]+)", r"fxios\/([-\w.]+)",
                 r"mobile vr; rv:([\w.]+)\).+firefox", r"(firefox)\/([\w.]+)")),
    ("IE", (r"(msie |trident.*rv:)([\w.]+)",)),
    ("Opera", (r"(opera|opr)\/([\w.]+)",)),
    ("Safari", (r"(safari)\/([\w.]+)(?!.*chrome)",)),
)
_BROWSER_OFFSET = {"Chrome": 0, "Firefox": 40, "Safari": 81, "Edge": 125, "Huawei": 170}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _custom_base64(data: list, alphabet: str) -> str:
    """自定义字母表 Base64 编码（3 字节 → 4 字符）。"""
    out = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        n = len(chunk)
        b = list(chunk) + [0] * (3 - n)
        v = (b[0] << 16) | (b[1] << 8) | b[2]
        idx = [(v >> 18) & 63, (v >> 12) & 63, (v >> 6) & 63, v & 63]
        if n == 1:
            out += [alphabet[idx[0]], alphabet[idx[1]], "=", "="]
        elif n == 2:
            out += [alphabet[idx[0]], alphabet[idx[1]], alphabet[idx[2]], "="]
        else:
            out += [alphabet[k] for k in idx]
    return "".join(out)


def _rc4_stream(key: list, data: list) -> list:
    """RC4 变体流密码（逆序 S 盒初始化）。"""
    sbox = [255 - x for x in range(256)]
    j = 0
    for i in range(256):
        j = (j * sbox[i] + j + key[i % len(key)]) % 256
        sbox[i], sbox[j] = sbox[j], sbox[i]
    out = []
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + sbox[i]) % 256
        sbox[i], sbox[j] = sbox[j], sbox[i]
        out.append(byte ^ sbox[(sbox[i] + sbox[j]) % 256])
    return out


def _utf16_style_bytes(text: str) -> list:
    """按字符值编码为字节（高位优先，仅对 >0xFF 字符拆分两字节）。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code & 0xFF00:
            out.append(code >> 8)
            out.append(code & 255)
        else:
            out.append(code)
    return out


def _little_endian_bytes(value: int, count: int) -> list:
    """value 的 count 字节小端表示。"""
    return [(int(value) >> (8 * i)) & 255 for i in range(count)]


def _signed_int32(value: int) -> int:
    """转为有符号 32 位整数。"""
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


# ---------------------------------------------------------------------------
# 载荷编码辅助
# ---------------------------------------------------------------------------
def _interleave_bytes(c0, c1, r0, r1):
    """将两个随机字节与两个载荷字节交错混合（高低位掩码交换）。"""
    return [(r0 & 0xAA) | (c0 & 0x55),
            (r0 & 0x55) | (c0 & 0xAA),
            (r1 & 0xAA) | (c1 & 0x55),
            (r1 & 0x55) | (c1 & 0xAA)]


def _expand_triples(arr, rand_fn):
    """每组 3 字节扩展为 4 字节（随机位掩码混合），余数原样保留。"""
    out = []
    i = 0
    n = len(arr)
    while i < n:
        if i + 2 < n:
            r = int(rand_fn() * 1000) & 255
            b0 = (r & 0x91) | (arr[i] & 0x6E)
            b1 = (r & 0x42) | (arr[i + 1] & 0xBD)
            b2 = (r & 0x2C) | (arr[i + 2] & 0xD3)
            b3 = ((arr[i] & 0x91) | (arr[i + 1] & 0x42) | (arr[i + 2] & 0x2C))
            out += [b0, b1, b2, b3]
        else:
            out.append(arr[i])
            if i + 1 < n and arr[i + 1]:
                out.append(arr[i + 1])
        i += 3
    return out


def _pick_digest_byte(digest: bytes, idx: int, reserved: int, fallback: int,
                      force_reserved: bool) -> int:
    """从摘要中选字节，避开保留值。"""
    v = digest[idx] if idx < len(digest) else fallback
    while v == reserved:
        idx += 1
        v = digest[idx] if idx < len(digest) else fallback
    return reserved if force_reserved else v


def _detect_browser(ua: str) -> str:
    """从 UA 识别浏览器名。"""
    for name, patterns in _BROWSER_PATTERNS:
        if any(re.search(p, ua, re.I) for p in patterns):
            return name
    return "Other"


def _env_flags(is_node_stack: bool, ua: str) -> int:
    """环境标志字节：位数 1/2/3/6 反映运行环境与浏览器。"""
    flags = 1
    flags |= 1 << 1
    flags |= int(is_node_stack) << 2
    flags |= int(_detect_browser(ua) == "Firefox") << 5
    return flags


def _counter_bucket(counter: int) -> int:
    """计数器分桶：按规模映射为 3~6。"""
    if counter > 10745:
        return 3
    if counter > 1283:
        return 4
    if counter > 139:
        return 5
    return 6


def _random_offset(rand_fn, browser_name: str) -> int:
    return int(rand_fn() * 40) + _BROWSER_OFFSET.get(browser_name, 210)


def _checksum_probe(rand_fn, flags_byte: int) -> int:
    if flags_byte & 64:
        r = int(rand_fn() * 109)
        return r + 110 + (r % 2)
    r = int(rand_fn() * 240)
    return r + (r % 2) + 1 if r > 109 else r


def _permission_bits(rand_fn) -> int:
    base = int(rand_fn() * 255) & 77
    return base | 2 | 16 | 32 | 128


# ---------------------------------------------------------------------------
# 签名器
# ---------------------------------------------------------------------------
class ABogusSigner:
    """a_bogus 签名器。

    两种模式：
    - fixed=True  ：确定性输出（固定时间/随机数），用于调试与单元测试。
    - fixed=False ：实时时间戳 + 随机数，用于线上请求。
    """

    def __init__(self, fixed: bool = True, user_agent: str = None):
        self.fixed = fixed
        self.ua = user_agent or (_FIREFOX_UA if fixed else browser_profile()["ua"])
        self.counter = _INITIAL_COUNTER
        self.geometry = _GEO_FIXED if fixed else browser_profile()["geometry"]
        self.browser_name = ("Firefox" if fixed else _detect_browser(self.ua))

    def _now_ms(self) -> int:
        return _FIXED_TIMESTAMP_MS if self.fixed else int(_time.time() * 1000)

    def _rand(self) -> float:
        return _FIXED_RANDOM if self.fixed else _rnd.random()

    def sign(self, url: str, body: str = "") -> str:
        """对完整 URL 生成 a_bogus。"""
        return self.sign_query(urllib.parse.urlsplit(url).query, body)

    def sign_query(self, query: str, body: str = "") -> str:
        self.counter += 1
        now = self._now_ms()

        # ---- 1. 摘要生成 ----
        query_digest = sm3_digest(sm3_digest((query + _SALT).encode("utf-8")))
        body_digest = sm3_digest(sm3_digest((body + _SALT).encode("utf-8")))
        ua_cipher = _rc4_stream([129 // 256, 129 % 256, 14 % 256],
                                [ord(c) for c in self.ua.strip()])
        ua_digest = sm3_digest(_custom_base64(ua_cipher, _ALPHABET_A).encode("utf-8"))

        # ---- 2. 载荷组装（90 槽位） ----
        payload = {}
        payload[12] = 3
        payload[14] = now
        payload[23] = [3, 82]
        payload[24] = 41
        payload[25] = [1, 0, 1, 0, 1]
        payload[26] = int((now - _FORTNIGHT_EPOCH_MS) / 1000 / 3600 / 24 / 14)
        payload[27] = _counter_bucket(self.counter)
        closure = _CLOSURE_EPOCH_MS if self.fixed else now
        payload[28] = (now - closure + 3) & 255 if closure > 0 else 2
        for i, b in enumerate(_little_endian_bytes(now, 6)):
            payload[29 + i] = b
        payload[35], payload[36] = _little_endian_bytes(129, 2)
        flags = _env_flags(self.fixed, self.ua)
        payload[37] = [0, 0, 0, 0, flags]
        payload[38], payload[39] = _little_endian_bytes(flags, 2)
        for i in range(4):
            payload[40 + i] = 0
        for i, b in enumerate(_little_endian_bytes(14, 4)):
            payload[44 + i] = b
        payload[48], payload[49] = query_digest[9], query_digest[18]
        payload[51] = _pick_digest_byte(query_digest, 3, 11, 12, bool(flags & 2))
        payload[52], payload[53] = body_digest[10], body_digest[19]
        payload[55] = _pick_digest_byte(body_digest, 4, 8, 9, bool(flags & 4))
        payload[56], payload[57] = ua_digest[11], ua_digest[21]
        payload[59] = _pick_digest_byte(ua_digest, 5, 12, 13, bool(flags & 8))
        for i, b in enumerate(_little_endian_bytes(now - 1, 6)):
            payload[60 + i] = b
        payload[66] = payload[12]
        for i, b in enumerate(_little_endian_bytes(6383, 4)):
            payload[67 + i] = b
        for i, b in enumerate(_little_endian_bytes(6383, 4)):
            payload[71 + i] = b
        geo_text = "|".join([str(v) for v in self.geometry] + [_GEO_PLATFORM])
        payload[77] = _utf16_style_bytes(geo_text)
        payload[78] = len(payload[77])
        payload[79], payload[80] = _little_endian_bytes(payload[78], 2)
        payload[81] = str((now + 3) & 255) + ","
        payload[82] = _utf16_style_bytes(payload[81])
        payload[83] = len(payload[82])
        payload[84], payload[85] = _little_endian_bytes(payload[83], 2)

        # 随机混合块（26~85 槽位的交错扩展）
        seed = self._rand() * 65535
        blend = _interleave_bytes(payload[25][0], payload[25][1],
                                  int(seed) & 255, (int(seed) >> 8) & 255)
        self._rand()
        blend += _interleave_bytes(payload[25][2], payload[25][3],
                                   _checksum_probe(self._rand, flags),
                                   _permission_bits(self._rand))
        payload[86] = blend

        # 校验和（参与字段异或）
        checksum = 0
        for b in blend + [payload[s] for s in _CHECKSUM_FIELDS]:
            checksum ^= b
        payload[87] = _signed_int32(checksum)

        # ---- 3. 提取与最终编码 ----
        extracted = [payload[s] for s in _EXTRACT_ORDER] \
            + payload[77] + payload[82] + [payload[87]]
        payload[88] = extracted

        head = int(self._rand() * 65535) & 255
        header = _interleave_bytes(3, 82, head,
                                   _random_offset(self._rand, self.browser_name))

        plain = blend + _expand_triples(extracted, self._rand)
        cipher = _rc4_stream([_RC4_KEY_BYTE], [x & 0xFFFF for x in plain])
        return _custom_base64(header + [c & 255 for c in cipher], _ALPHABET_B)
