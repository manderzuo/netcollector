# -*- coding: utf-8 -*-
"""x_bogus.py — X-Bogus 签名生成器（老版签名，备用通道）。

结构概述：
  1. 对请求体做双重 MD5，对 stub 做单重 MD5；
  2. 组装 9 字节明文（计数器低位、环境标志、v14、摘要字节、随机位）；
  3. 字节异或得校验和，单字节 key RC4 加密；
  4. 自定义 Base64 输出。

设计说明：
- 与 a_bogus 互为独立通道；新版接口主要用 a_bogus，本模块作为备用。
- 命名与结构独立组织，仅保留算法功能事实。
"""

import hashlib
import random as _rnd

_ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe"


def _custom_base64(data: list) -> str:
    """自定义字母表 Base64 编码。"""
    out = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        n = len(chunk)
        b = list(chunk) + [0] * (3 - n)
        v = (b[0] << 16) | (b[1] << 8) | b[2]
        idx = [(v >> 18) & 63, (v >> 12) & 63, (v >> 6) & 63, v & 63]
        if n == 1:
            out += [_ALPHABET[idx[0]], _ALPHABET[idx[1]], "=", "="]
        elif n == 2:
            out += [_ALPHABET[idx[0]], _ALPHABET[idx[1]], _ALPHABET[idx[2]], "="]
        else:
            out += [_ALPHABET[k] for k in idx]
    return "".join(out)


def _rc4_encrypt(key: list, data: list) -> list:
    """标准 RC4（正序 S 盒）。"""
    sbox = list(range(256))
    j = 0
    for i in range(256):
        j = (j + sbox[i] + key[i % len(key)]) & 255
        sbox[i], sbox[j] = sbox[j], sbox[i]
    out = []
    i = j = 0
    for byte in data:
        i = (i + 1) & 255
        j = (j + sbox[i]) & 255
        sbox[i], sbox[j] = sbox[j], sbox[i]
        out.append(byte ^ sbox[(sbox[i] + sbox[j]) & 255])
    return out


def _random_byte(r: float) -> int:
    return int(255 * r) & 255


def _environment_flags(browser: str = "Chrome", top_level: bool = True,
                       geometry_sane: bool = True) -> int:
    """环境标志字节（各 bit 表示浏览器/层级/几何状态）。"""
    is_firefox = browser == "Firefox"
    is_iframe = not top_level
    is_odd_geometry = not geometry_sane
    return (1
            | 0 << 1
            | 0 << 2
            | 1 << 3
            | 0 << 4
            | int(is_firefox) << 5
            | int(is_iframe) << 6
            | int(is_odd_geometry) << 7)


def _version_flags() -> int:
    return 4 | 8


def _build_signature(stub_hex: str, counter: int, rand_a: float, rand_b: float,
                     rand_c: float, payload: str = "", env_flags: int = None,
                     version: int = None) -> str:
    """构造 X-Bogus 签名。"""
    if env_flags is None:
        env_flags = _environment_flags()
    if version is None:
        version = _version_flags()

    digest_payload = hashlib.md5(hashlib.md5(payload.encode("utf-8")).digest()).digest()
    digest_stub = hashlib.md5(bytes.fromhex(stub_hex)).digest()
    counter += 1

    plain = [
        counter & 0x3F,
        (counter >> 8) & 255,
        env_flags,
        version,
        digest_payload[14], digest_payload[15],
        digest_stub[14], digest_stub[15],
        _random_byte(rand_b),
    ]
    checksum = 0
    for byte in plain:
        checksum ^= byte
    key_byte = _random_byte(rand_c)
    cipher = _rc4_encrypt([key_byte], plain + [checksum])
    header = (1 << 6) | ((int(100 * rand_a) & 1) << 4)
    return _custom_base64([header, key_byte] + cipher)


class XBogusGenerator:
    """X-Bogus 签名生成器。"""

    def __init__(self, browser: str = "Chrome", top_level: bool = True,
                 geometry_sane: bool = True):
        self.counter = 0
        self.env_flags = _environment_flags(browser, top_level, geometry_sane)
        self.version = _version_flags()

    def sign(self, stub_hex: str, payload: str = "") -> str:
        """对 stub 十六进制字符串生成 X-Bogus。"""
        signature = _build_signature(
            stub_hex, self.counter,
            _rnd.random(), _rnd.random(), _rnd.random(),
            payload, self.env_flags, self.version,
        )
        self.counter += 1
        return signature
