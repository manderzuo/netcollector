# -*- coding: utf-8 -*-
"""report_body.py — 字节跳动安全 SDK 上报请求体构造。

构造 mssdk 上报接口需要的 strData 载荷：
  1. 组装浏览器环境指纹 JSON（含导航器/窗口/屏幕/插件等字段）；
  2. 用单字节随机 nonce 做 RC4 加密；
  3. 自定义 Base64 编码后装入上报信封（magic/version/dataType/strData）。

设计说明：
- 指纹 JSON 字段是服务端校验的输入事实，字段结构不可省略。
- 本实现以结构化模板 + 运行时填充的方式组织，便于维护。
"""

import json
import random
import time

from .env_profile import browser_profile

# 自定义 Base64 字母表（与 a_bogus 输出编码共用同一套字符）
_ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe"

# 指纹 JSON 模板：环境字段的默认值（运行时按 profile 覆盖关键项）
_FINGERPRINT_TEMPLATE = {
    "tokenList": [],
    "navigator": {
        "appCodeName": "Mozilla", "appMinorVersion": "undefined",
        "appName": "Netscape", "appVersion": "5.0 (Windows)",
        "buildID": "undefined", "doNotTrack": "null",
        "msDoNotTrack": "undefined", "oscpu": "undefined",
        "platform": "Win32", "product": "Gecko", "productSub": "20030107",
        "cpuClass": "undefined", "vendor": "Google Inc.", "vendorSub": "undefined",
        "deviceMemory": "8", "language": "zh-CN", "systemLanguage": "undefined",
        "userLanguage": "undefined", "webdriver": "false", "cookieEnabled": 1,
        "vibrate": 4, "credentials": 4, "storage": 4,
        "requestMediaKeySystemAccess": 4, "bluetooth": 4,
        "hardwareConcurrency": 12, "maxTouchPoints": -1,
        "languages": "zh-CN,zh", "touchEvent": 1, "touchstart": 2,
    },
    "wID": {
        "load": 0, "nap": "6", "nativeLength": 33, "jsFontsList": "0",
        "timestamp": "1784564385945", "timezone": 8, "magic": 3,
        "canvas": "-1", "wProps": 374262, "dProps": 2, "jsv": "",
        "browserType": 0, "iframe": 2, "aid": 0, "msgType": 1,
        "privacyMode": 0, "aidList": [], "index": 1,
    },
    "window": {
        "Image": 3, "isSecureContext": 4, "ActiveXObject": 4,
        "toolbar": 4, "locationbar": 4, "external": 4,
        "mozRTCPeerConnection": 4, "postMessage": 3,
        "webkitRequestAnimationFrame": 4, "BluetoothUUID": 4,
        "netscape": 4, "localStorage": 11, "sessionStorage": 11,
        "indexDB": 4, "devicePixelRatio": 1,
        "location": "https://www.douyin.com/",
    },
    "webgl": {},
    "document": {
        "characterSet": "UTF-8", "compatMode": "undefined",
        "documentMode": "undefined", "layers": 4, "all": 4, "images": 4,
    },
    "screen": {
        "innerWidth": 1707, "innerHeight": 809, "outerWidth": 1707,
        "outerHeight": 912, "screenX": 0, "screenY": 0, "pageXOffset": 0,
        "pageYOffset": 0, "availWidth": 1707, "availHeight": 912,
        "sizeWidth": 1707, "sizeHeight": 960, "clientWidth": 1697,
        "clientHeight": 809, "colorDepth": 24, "pixelDepth": 24,
    },
    "plugins": {"plugin": [], "pv": "0"},
    "custom": {},
}


def _rc4_encrypt(key: bytes, data: bytes) -> bytes:
    """标准 RC4 流密码（正序 S 盒初始化）。"""
    sbox = list(range(256))
    j = 0
    for i in range(256):
        j = (j + sbox[i] + key[i % len(key)]) & 255
        sbox[i], sbox[j] = sbox[j], sbox[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 255
        j = (j + sbox[i]) & 255
        sbox[i], sbox[j] = sbox[j], sbox[i]
        out.append(byte ^ sbox[(sbox[i] + sbox[j]) & 255])
    return bytes(out)


def _custom_base64(data: bytes) -> str:
    """自定义字母表 Base64 编码。"""
    out = []
    n = len(data)
    for i in range(0, n, 3):
        chunk = data[i:i + 3]
        b0 = chunk[0]
        b1 = chunk[1] if len(chunk) > 1 else 0
        b2 = chunk[2] if len(chunk) > 2 else 0
        trip = (b0 << 16) | (b1 << 8) | b2
        out.append(_ALPHABET[(trip >> 18) & 63])
        out.append(_ALPHABET[(trip >> 12) & 63])
        out.append(_ALPHABET[(trip >> 6) & 63] if len(chunk) > 1 else "=")
        out.append(_ALPHABET[trip & 63] if len(chunk) > 2 else "=")
    return "".join(out)


def _encode_strdata(plaintext: bytes, nonce: int) -> str:
    """RC4 加密 + 前缀 0x41/nonce + Base64。"""
    cipher = _rc4_encrypt(bytes([nonce]), plaintext)
    raw = bytes([0x41, nonce]) + cipher
    return _custom_base64(raw)


def build_fingerprint() -> str:
    """构造完整指纹 JSON 字符串（填充 profile 的几何/硬件数据）。"""
    profile = browser_profile()
    geometry = profile["geometry"]

    fp = json.loads(json.dumps(_FINGERPRINT_TEMPLATE))  # 深拷贝
    fp["navigator"]["hardwareConcurrency"] = int(profile["cpu_core_num"])
    fp["navigator"]["deviceMemory"] = profile["device_memory"]
    fp["screen"].update({
        "innerWidth": geometry[0], "innerHeight": geometry[1],
        "outerWidth": geometry[2], "outerHeight": geometry[3],
        "availWidth": geometry[4], "availHeight": geometry[5],
        "sizeWidth": geometry[6], "sizeHeight": geometry[7],
        "clientWidth": geometry[0] - 10, "clientHeight": geometry[1],
    })
    fp["wID"]["timestamp"] = str(int(time.time() * 1000))
    return json.dumps(fp, ensure_ascii=False, separators=(",", ":"))


def build_envelope() -> str:
    """构造上报信封 JSON（strData 加密负载）。"""
    plaintext = build_fingerprint()
    nonce = random.randint(0, 255)
    str_data = _encode_strdata(plaintext.encode("utf-8"), nonce)
    envelope = {
        "magic": 538969122,
        "version": 1,
        "dataType": 8,
        "strData": str_data,
        "tspFromClient": int(time.time() * 1000),
        "ulr": 0,
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
