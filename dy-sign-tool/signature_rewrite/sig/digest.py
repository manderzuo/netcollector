# -*- coding: utf-8 -*-
"""digest.py — 国密 SM3 密码杂凑算法实现。

实现依据：GB/T 32905-2016《信息安全技术 SM3 密码杂凑算法》公开规范。
算法流程：消息填充 → 分组扩展(W/W1) → 64 轮压缩函数 → 8×32bit 状态寄存器。

设计说明：
- 纯标准库实现，无第三方依赖。
- 输出为 32 字节原始摘要(bytes)；提供 sm3_hex 便捷方法。
- 标准测试向量：
    SM3("abc")      = 66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0
    SM3("abcd"*16)  = debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732
"""

# 初始状态寄存器 IV（8 个 32 位字）
_IV = [
    0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
    0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E,
]

_MASK32 = 0xFFFFFFFF


def _rotl(value: int, shift: int) -> int:
    """32 位循环左移。"""
    shift &= 31
    value &= _MASK32
    return ((value << shift) | (value >> (32 - shift))) & _MASK32


def _round_constant(j: int) -> int:
    """第 j 轮压缩常量 T_j。"""
    return 0x79CC4519 if j < 16 else 0x7A879D8A


def _ff(x: int, y: int, z: int, j: int) -> int:
    """布尔函数 FF。"""
    if j < 16:
        return x ^ y ^ z
    return (x & y) | (x & z) | (y & z)


def _gg(x: int, y: int, z: int, j: int) -> int:
    """布尔函数 GG。"""
    if j < 16:
        return x ^ y ^ z
    return (x & y) | ((~x) & z)


def _p0(x: int) -> int:
    """置换函数 P0。"""
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    """置换函数 P1。"""
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _message_expansion(block: bytes):
    """消息扩展：512 位分组 → 68 字 W + 64 字 W'。"""
    w = list(range(68))
    for i in range(16):
        w[i] = int.from_bytes(block[i * 4:i * 4 + 4], "big")
    for j in range(16, 68):
        w[j] = (_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
                ^ _rotl(w[j - 13], 7) ^ w[j - 6]) & _MASK32
    w_prime = [(w[j] ^ w[j + 4]) & _MASK32 for j in range(64)]
    return w, w_prime


def _compression(state, block: bytes):
    """压缩函数：一轮处理 512 位分组。"""
    w, w_prime = _message_expansion(block)
    a, b, c, d, e, f, g, h = state

    for j in range(64):
        ss1 = _rotl((_rotl(a, 12) + e + _rotl(_round_constant(j), j)) & _MASK32, 7)
        ss2 = ss1 ^ _rotl(a, 12)
        tt1 = (_ff(a, b, c, j) + d + ss2 + w_prime[j]) & _MASK32
        tt2 = (_gg(e, f, g, j) + h + ss1 + w[j]) & _MASK32
        d, c = c, _rotl(b, 9)
        b, a = a, tt1
        h, g = g, _rotl(f, 19)
        f, e = e, _p0(tt2)

    return [(x ^ y) & _MASK32 for x, y in zip([a, b, c, d, e, f, g, h], state)]


def sm3_digest(message: bytes) -> bytes:
    """计算消息的 SM3 摘要，返回 32 字节原始字节串。"""
    if isinstance(message, str):
        message = message.encode("utf-8")
    msg = bytearray(message)
    bit_length = len(msg) * 8

    # 填充：追加 0x80，补零至长度 ≡ 56 (mod 64)，最后 8 字节写原始比特长度
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0x00)
    msg += bit_length.to_bytes(8, "big")

    state = _IV[:]
    for i in range(0, len(msg), 64):
        state = _compression(state, msg[i:i + 64])

    return b"".join(word.to_bytes(4, "big") for word in state)


def sm3_hex(message) -> str:
    """计算 SM3 摘要，返回十六进制字符串。"""
    return sm3_digest(message).hex()


if __name__ == "__main__":
    cases = [
        (b"abc", "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"),
        (b"abcd" * 16, "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732"),
    ]
    for data, expected in cases:
        got = sm3_hex(data)
        print(f"SM3({data[:8]!r}...) = {got}")
        print(f"  expect            = {expected}")
        print(f"  {'PASS' if got == expected else 'FAIL'}")
