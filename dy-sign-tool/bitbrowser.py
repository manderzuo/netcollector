# -*- coding: utf-8 -*-
"""BitBrowser(比特浏览器)本地 API 客户端封装。

接口来源官方接口文档: D:\\DSH\\cdp\\doc-browser-api.txt
(所有接口均为 POST + JSON body，返回 {"success": true, "data": ...} 或
{"success": false, "msg": "..."}；无需鉴权，服务默认运行在 http://127.0.0.1:54345)

要点：
- 请求 JSON 必须以 UTF-8 字节发送(json.dumps(..., ensure_ascii=False).encode('utf-8'))，
  否则中文 name / 备注会乱码。
- health/checkagent 等接口的 data 内层可能还有嵌套的 {"success": ...}，一律原样透传。
- 仅使用标准库 urllib，无第三方依赖。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional, Union


class BitBrowserError(Exception):
    """BitBrowser 本地 API 调用失败。

    包装两种失败：
    1. HTTP/网络层失败(连不上、超时、非 JSON 响应)；
    2. 接口业务失败(返回 {"success": false, "msg": "..."})，msg 会附在异常信息里。
    """


class BitBrowserClient:
    """BitBrowser 本地服务的 HTTP 客户端，负责组装请求体并解析统一返回形状。"""

    def __init__(self, base_url: str = "http://127.0.0.1:54345", timeout: float = 20):
        """初始化客户端。

        :param base_url: 本地 API 服务地址，默认 http://127.0.0.1:54345
        :param timeout: 单次请求超时秒数，默认 20
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    # 私有基础方法
    # ------------------------------------------------------------------ #
    def _post(self, path: str, body: Optional[dict] = None) -> Any:
        """向本地 API 发送 POST 请求并解析返回值。

        :param path: 接口路径，如 "/health"
        :param body: 请求体 dict，None 视为空对象 {}
        :return: 成功时返回响应中的 data 部分(原样透传，不处理嵌套 success)
        :raises BitBrowserError: 网络/HTTP 失败、响应非 JSON、或接口返回 success=false 时抛出
        """
        url = self.base_url + path
        payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # HTTP 错误码：尝试读取响应体里的 msg 增强可读性
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise BitBrowserError(
                "请求 {} 失败: HTTP {} {}{}".format(
                    path, exc.code, exc.reason, f" - {detail}" if detail else ""
                )
            ) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            raise BitBrowserError(
                "请求 {} 失败(无法连接 BitBrowser 本地服务 {}): {}".format(
                    path, self.base_url, exc
                )
            ) from exc

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BitBrowserError(
                "请求 {} 返回非 JSON 响应: {}".format(path, raw[:200])
            ) from exc

        if not obj.get("success"):
            msg = obj.get("msg") or "未知错误"
            raise BitBrowserError("BitBrowser 接口 {} 返回失败: {}".format(path, msg))

        return obj.get("data")

    @staticmethod
    def _as_list(value: Union[str, Any]) -> list:
        """把单个 id 或可迭代对象统一成列表(字符串按单个元素处理)。"""
        if value is None:
            return []
        if isinstance(value, (str, bytes)):
            return [value]
        if isinstance(value, Iterable):
            return list(value)
        return [value]

    # ------------------------------------------------------------------ #
    # 健康检查 / 浏览器列表 / 详情
    # ------------------------------------------------------------------ #
    def health(self) -> Any:
        """健康检查，无参数。

        成功时 data 为字符串 "the server is running good."。
        """
        return self._post("/health", {})

    def list_browsers(
        self, page: int = 0, page_size: int = 100, **filters: Any
    ) -> dict:
        """分页获取浏览器窗口列表。

        :param page: 页码，从 0 开始
        :param page_size: 每页数量，最大 100
        :param filters: 可选过滤条件，如 groupId、name、seq、minSeq、maxSeq、
                        sort("asc"/"desc")、ownedByMe、opened 等，直接并入请求体
        :return: data dict，含 {"page", "pageSize", "totalNum", "list": [...]}
        """
        body: dict = {"page": page, "pageSize": page_size}
        body.update(filters)
        return self._post("/browser/list", body)

    def detail(self, browser_id: str) -> dict:
        """获取单个浏览器窗口详情。

        :param browser_id: 窗口 ID(配置里复制的 ID，非序号)
        :return: 窗口对象 dict
        """
        return self._post("/browser/detail", {"id": browser_id})

    # ------------------------------------------------------------------ #
    # 打开 / 关闭
    # ------------------------------------------------------------------ #
    def open_browser(
        self,
        browser_id: str,
        args: Optional[list] = None,
        queue: bool = True,
        ignore_default_urls: bool = False,
        new_page_url: Optional[str] = None,
    ) -> dict:
        """打开浏览器窗口。

        :param browser_id: 窗口 ID
        :param args: 浏览器启动参数列表，如 ["--incognito", "--no-sandbox"]
        :param queue: 是否以队列方式打开，防多线程并发启动冲突，默认 True
        :param ignore_default_urls: 忽略已同步的 url，只打开空白页/工作台
        :param new_page_url: 打开时指定的 url(需配合 ignore_default_urls=True)
        :return: data dict，含 ws、http、coreVersion、driver、pid 等
        """
        body: dict = {"id": browser_id, "args": args or [], "queue": queue}
        if ignore_default_urls:
            body["ignoreDefaultUrls"] = True
        if new_page_url:
            body["newPageUrl"] = new_page_url
        return self._post("/browser/open", body)

    def close_browser(self, browser_id: str) -> Any:
        """关闭单个浏览器窗口。

        注意：关闭后需等 5 秒进程彻底退出再删除/重开。
        """
        return self._post("/browser/close", {"id": browser_id})

    def delete_browser(self, browser_id: str) -> Any:
        """彻底删除一个浏览器窗口(profile)。删除后无法从回收站找回。

        注意：删除前应先 close_browser 并等待进程退出。
        """
        return self._post("/browser/delete", {"id": browser_id})

    def close_by_seqs(self, seqs: Union[Iterable[int], int]) -> Any:
        """按窗口序号批量关闭窗口。

        :param seqs: 序号或序号列表，如 [12, 13]
        """
        return self._post("/browser/close/byseqs", {"seqs": self._as_list(seqs)})

    def close_all(self) -> Any:
        """关闭所有窗口，无参数。"""
        return self._post("/browser/close/all", {})

    # ------------------------------------------------------------------ #
    # 进程 / 端口
    # ------------------------------------------------------------------ #
    def pids(self, ids: Union[Iterable[str], str]) -> dict:
        """查询指定窗口的进程 pid(也可用来判断窗口是否已打开)。

        :param ids: 窗口 ID 或 ID 列表
        :return: {browserId: pid} 映射 dict
        """
        return self._post("/browser/pids", {"ids": self._as_list(ids)})

    def pids_all(self) -> dict:
        """获取所有活着(已打开)窗口的进程 ID，自动过滤已死进程。"""
        return self._post("/browser/pids/all", {})

    def ports(self) -> dict:
        """获取所有已打开窗口的调试端口 remote-debugging-port。

        :return: {browserId: "端口"} 映射 dict
        """
        return self._post("/browser/ports", {})

    # ------------------------------------------------------------------ #
    # 创建 / 部分更新
    # ------------------------------------------------------------------ #
    def create_window(
        self,
        name: str,
        group_id: Optional[str] = None,
        proxy: Optional[dict] = None,
        fingerprint: Optional[dict] = None,
        **kw: Any,
    ) -> dict:
        """创建浏览器窗口(调用 /browser/update)。

        :param name: 窗口名称(将按 UTF-8 发送，避免中文乱码)
        :param group_id: 分组 ID；不传时系统自动创建 "API 分组" 并归入
        :param proxy: 代理 dict，如 {"proxyType": "socks5", "host": "...", "port": 1020,
                      "proxyUserName": "u", "proxyPassword": "p"}；
                      None 表示不设置代理(proxyType=noproxy 直连)
        :param fingerprint: 指纹对象 dict；None 表示空对象 {} = 随机指纹。
                      browserFingerPrint 必传，字段留空即随机
        :param kw: 其余可修改字段，如 platform、url、remark、userName、password、
                   cookie、ipCheckService、workbench、abortImage、muteAudio 等，
                   直接并入请求体(与 /browser/update 参数表一致)
        :return: 完整窗口对象 dict(含新建的 id)
        """
        body: dict = {
            "name": name,
            "proxyMethod": 2,
            "proxyType": "noproxy",
            "browserFingerPrint": dict(fingerprint or {}),
        }
        if group_id:
            body["groupId"] = group_id
        if proxy:
            body.update(
                {
                    "proxyMethod": 2,
                    "proxyType": proxy.get("proxyType", "socks5"),
                    "host": proxy.get("host"),
                    "port": proxy.get("port"),
                    "proxyUserName": proxy.get("proxyUserName") or proxy.get("username", ""),
                    "proxyPassword": proxy.get("proxyPassword") or proxy.get("password", ""),
                }
            )
        body.update(kw)
        return self._post("/browser/update", body)

    def update_partial(self, ids: Union[Iterable[str], str], **fields: Any) -> Any:
        """批量修改窗口与指纹的指定字段值。

        只传需要更新的字段即可(如 name、groupId、url 等，参数与 /browser/update 一致；
        修改代理请用代理专用接口)。

        :param ids: 窗口 ID 或 ID 列表
        :param fields: 待更新的字段，如 update_partial(ids, name="新名称")
        """
        body: dict = {"ids": self._as_list(ids)}
        body.update(fields)
        return self._post("/browser/update/partial", body)

    # ------------------------------------------------------------------ #
    # Cookie
    # ------------------------------------------------------------------ #
    def cookies_get(self, browser_id: str) -> list:
        """获取已打开窗口的实时 cookies。

        注意：实时 cookie 可能一直在变，两次获取结果可能不一致。
        :return: cookie dict 列表(标准 cookie 格式)
        """
        return self._post("/browser/cookies/get", {"browserId": browser_id})

    def cookies_set(self, browser_id: str, cookies: list) -> Any:
        """对已打开窗口设置实时 cookie。

        :param browser_id: 窗口 ID
        :param cookies: 标准 cookie 格式的 dict 列表
        """
        return self._post("/browser/cookies/set", {"browserId": browser_id, "cookies": cookies})

    def cookies_clear(self, browser_id: str, save_synced: bool = True) -> Any:
        """清空窗口 cookie(无论窗口是否打开均可调用)。

        :param browser_id: 窗口 ID
        :param save_synced: 是否保留已同步到服务端的 cookie，默认 True(本地+云端一起清)
        """
        return self._post(
            "/browser/cookies/clear", {"browserId": browser_id, "saveSynced": save_synced}
        )

    # ------------------------------------------------------------------ #
    # 代理检测
    # ------------------------------------------------------------------ #
    def check_agent(
        self,
        host: str,
        port: int,
        proxy_type: str = "socks5",
        username: str = "",
        password: str = "",
        ip_check_service: str = "ip123in",
    ) -> dict:
        """检测代理是否可用并查询代理 IP 信息。

        :param host: 代理主机
        :param port: 代理端口
        :param proxy_type: 代理类型，http | socks5 | ssh 选一，默认 socks5
        :param username: 代理用户名(可空)
        :param password: 代理密码(可空)
        :param ip_check_service: IP 检测渠道，默认 ip123in，可选 ip-api
        :return: 嵌套 data，含 ip、countryName、city、status、used 等
                 (内层还有一层 {"success": ..., "data": ...}，原样透传)
        """
        return self._post(
            "/checkagent",
            {
                "host": host,
                "port": port,
                "proxyType": proxy_type,
                "proxyUserName": username,
                "proxyPassword": password,
                "ipCheckService": ip_check_service,
            },
        )


__all__ = ["BitBrowserClient", "BitBrowserError"]