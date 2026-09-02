# -*- coding: utf-8 -*-
"""地域识别与可信度（技术方案 Agent B）。

纯函数：不访问 GUI、不读写数据库、不推荐发送。人工修正值不可被自动重算覆盖
（验收红线 9.3）。
"""

from __future__ import annotations

from typing import Optional

from .models import RegionResult, RegionSource, REGION_CONFIDENCE

# 省份 → 该省主要城市别名（用于城市到省份映射）。仅收录较常见的城市写法。
_PROVINCE_CITIES: dict[str, set[str]] = {
    "河南省": {
        "郑州", "洛阳", "开封", "新乡", "安阳", "濮阳", "焦作", "鹤壁", "济源",
        "许昌", "漯河", "平顶山", "南阳", "信阳", "商丘", "周口", "驻马店", "三门峡",
    },
}

# 省份别名归一化（标准省名 + 常用简称）
_PROVINCE_ALIASES = {
    # 河南（业务重点）
    "河南": "河南省", "河南省": "河南省", "豫": "河南省", "河南郑州": "河南省",
    "豫北": "河南省", "豫南": "河南省", "豫东": "河南省", "豫西": "河南省",
    # 常见省级行政区（标准名 + 常用简称/俗称）
    "北京": "北京市", "北京市": "北京市", "京": "北京市",
    "上海": "上海市", "上海市": "上海市", "沪": "上海市", "申": "上海市",
    "天津": "天津市", "天津市": "天津市", "津": "天津市",
    "重庆": "重庆市", "重庆市": "重庆市", "渝": "重庆市",
    "广东": "广东省", "广东省": "广东省", "粤": "广东省", "广东广州": "广东省",
    "广州": "广东省",
    "江苏": "江苏省", "江苏省": "江苏省", "苏": "江苏省",
    "浙江": "浙江省", "浙江省": "浙江省", "浙": "浙江省",
    "山东": "山东省", "山东省": "山东省", "鲁": "山东省",
    "四川": "四川省", "四川省": "四川省", "川": "四川省", "蜀": "四川省",
    "湖北": "湖北省", "湖北省": "湖北省", "鄂": "湖北省",
    "湖南": "湖南省", "湖南省": "湖南省", "湘": "湖南省",
    "福建": "福建省", "福建省": "福建省", "闽": "福建省",
    "安徽": "安徽省", "安徽省": "安徽省", "皖": "安徽省",
    "河北": "河北省", "河北省": "河北省", "冀": "河北省",
    "陕西": "陕西省", "陕西省": "陕西省", "陕": "陕西省", "秦": "陕西省",
    "山西": "山西省", "山西省": "山西省", "晋": "山西省",
    "辽宁": "辽宁省", "辽宁省": "辽宁省", "辽": "辽宁省",
    "吉林": "吉林省", "吉林省": "吉林省", "吉": "吉林省",
    "黑龙": "黑龙江省", "黑龙江": "黑龙江省", "黑龙江省": "黑龙江省", "黑": "黑龙江省",
    "江西": "江西省", "江西省": "江西省", "赣": "江西省",
    "云南": "云南省", "云南省": "云南省", "滇": "云南省",
    "贵州": "贵州省", "贵州省": "贵州省", "黔": "贵州省",
    "广西": "广西壮族自治区", "广西壮族自治": "广西壮族自治区", "桂": "广西壮族自治区",
    "海南": "海南省", "海南省": "海南省", "琼": "海南省",
    "甘肃": "甘肃省", "甘肃省": "甘肃省", "甘": "甘肃省", "陇": "甘肃省",
    "宁夏": "宁夏回族自治区", "宁": "宁夏回族自治区",
    "青海": "青海省", "青海省": "青海省", "青": "青海省",
    "新疆": "新疆维吾尔自治区", "新疆维吾尔自治": "新疆维吾尔自治区", "新": "新疆维吾尔自治区",
    "西藏": "西藏自治区", "藏": "西藏自治区",
    "内蒙古": "内蒙古自治区", "蒙": "内蒙古自治区",
    "台湾": "台湾省", "台湾省": "台湾省", "台": "台湾省",
    "香港": "香港特别行政区", "香港特别行政区": "香港特别行政区", "港": "香港特别行政区",
    "澳门": "澳门特别行政区", "澳门特别行政区": "澳门特别行政区", "澳": "澳门特别行政区",
}

# 标准省名集合（别名表 value 去重），用于在正文中做 sub-string 匹配
_STANDARD_PROVINCES = tuple(sorted(set(_PROVINCE_ALIASES.values())))

# 城市后缀规范化
_CITY_SUFFIXES = ("市", "区", "县")


def normalize_province(value: Optional[str]) -> Optional[str]:
    """把省份别名归一化为标准省名（如 河南/豫 → 河南省）。无法识别返回 None。"""
    if not value:
        return None
    v = str(value).strip()
    # 去掉常见前后缀
    for suf in ("省", "市"):
        if v.endswith(suf) and len(v) > 1:
            v = v[:-1]
    if not v:
        return None
    return _PROVINCE_ALIASES.get(v)


def _province_by_text(text: str) -> Optional[str]:
    """从自由文本中识别明确提到的省份（河南为业务重点，也支持常见外省）。

    原则：只返回**明确出现**的省份，绝不猜测。常见结构：
    - 整串精确别名（“河南”“广东省”“豫”）
    - “省名+市”（“河南郑州”“广东省广州”）
    - 正文含标准省名（“我是河南的”“家在广东”）
    """
    if not text:
        return None
    t = str(text).strip()

    # 1) 整串精确匹配别名表
    if t in _PROVINCE_ALIASES:
        return _PROVINCE_ALIASES[t]

    # 2) 正文包含完整省级别名（如“我是河南的”“来自广东”）。
    # 单字简称不参与 substring 匹配，避免“豫见”“新手”等普通词误判。
    for alias in sorted(_PROVINCE_ALIASES, key=len, reverse=True):
        if len(alias) >= 2 and alias in t:
            return _PROVINCE_ALIASES[alias]

    # 3) 城市信息也可作为明确地域证据。实际平台常只展示“郑州”等城市名。
    city_province = city_to_province(t)
    if city_province:
        return city_province
    for province, cities in _PROVINCE_CITIES.items():
        if any(city in t for city in cities):
            return province
    return None


def city_to_province(city: Optional[str]) -> Optional[str]:
    """城市名映射到省份。返回标准省名；未知返回 None。"""
    if not city:
        return None
    c = str(city).strip()
    for suf in _CITY_SUFFIXES:
        if c.endswith(suf):
            c = c[:-1]
            break
    for province, cities in _PROVINCE_CITIES.items():
        if c in cities:
            return province
    return None


class RegionClassifier:
    """按技术方案 6.1 的地域判定优先级计算最终省份与可信度。

    优先级（高→低）：人工确认 > 自述 > 主页地区 > IP 属地 > 文本弱推断 > 未知。
    人工确认值单独传入，自动规则重算时不得覆盖（保留在原判定之上）。
    """

    def __init__(self, confidence_threshold: int = 65):
        self._threshold = confidence_threshold

    # ------------------------------------------------------------------
    def classify(
        self,
        *,
        self_declared: Optional[str] = None,
        profile_region: Optional[str] = None,
        ip_label: Optional[str] = None,
        text: Optional[str] = None,
        manual_province: Optional[str] = None,
        manual_city: Optional[str] = None,
    ) -> RegionResult:
        """综合判定地域。manual_* 优先且不可被自动覆盖。"""
        notes: list[str] = []

        # 1. 人工确认（优先级最高）
        if manual_province:
            return RegionResult(
                province=normalize_province(manual_province) or manual_province,
                city=manual_city,
                source=RegionSource.MANUAL,
                confidence=REGION_CONFIDENCE[RegionSource.MANUAL],
                notes=["人工确认"],
            )

        # 2. 用户明确自述
        declared = _province_by_text(self_declared) if self_declared else None
        if declared:
            return self._result(declared, city_from=self_declared, source=RegionSource.SELF_DECLARED,
                                notes=["用户在公开内容中明确自述"], notes_out=notes)

        # 3. 平台公开主页地区
        prof = normalize_province(profile_region) or _province_by_text(profile_region) \
            if profile_region else None
        if prof:
            return self._result(prof, city_from=profile_region, source=RegionSource.PROFILE,
                                notes=["平台公开主页地区"], notes_out=notes)

        # 4. 平台公开 IP 属地
        ip = normalize_province(ip_label) or _province_by_text(ip_label) if ip_label else None
        if ip:
            return self._result(ip, city_from=ip_label, source=RegionSource.IP_LABEL,
                                notes=["平台公开 IP 属地"], notes_out=notes)

        # 5. 评论文本弱推断（只认明确河南，避免误判外省）
        inferred = _province_by_text(text) if text else None
        if inferred:
            return self._result(inferred, city_from=text, source=RegionSource.TEXT_INFERRED,
                                notes=["评论文本弱推断"], notes_out=notes)

        # 6. 未知
        notes.append("无法判断地域")
        return RegionResult(source=RegionSource.UNKNOWN, confidence=0, notes=notes)

    def _result(self, province, city_from, source, notes, notes_out) -> RegionResult:
        province = province if province.startswith("河南省") else normalize_province(province) or province
        city = self._extract_city(province, city_from)
        notes_out.append(f"省份={province}")
        return RegionResult(
            province=province,
            city=city,
            source=source,
            confidence=REGION_CONFIDENCE[source],
            notes=list(notes_out),
        )

    def _extract_city(self, province: str, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        for p, cities in _PROVINCE_CITIES.items():
            if p != province:
                continue
            for c in cities:
                if c in str(raw):
                    return c
        return None

    # ------------------------------------------------------------------
    def is_henan_qualified(self, result: RegionResult) -> bool:
        """是否可进入河南可跟进池：省份为河南且可信度达标。"""
        return (
            result.province == "河南省"
            and result.confidence >= self._threshold
        )

    def pool_for(self, result: RegionResult) -> str:
        """按地域判定结果分池（6.2）。"""
        from .models import Pool

        if not result.province:
            return Pool.UNKNOWN_REGION
        if result.province == "河南省":
            return Pool.HENAN if self.is_henan_qualified(result) else Pool.REGION_REVIEW
        return Pool.OTHER_PROVINCE
