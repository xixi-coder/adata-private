# -*- coding: utf-8 -*-
"""
代理:https://jahttp.zhimaruanjian.com/getapi/

@desc: adata 请求工具类
@author: 1nchaos
@time:2023/3/30
@log: 封装请求次数
"""

import threading
import time

import requests


class SunProxy(object):
    _data = {}
    _instance_lock = threading.Lock()

    def __init__(self):
        pass

    def __new__(cls, *args, **kwargs):
        if not hasattr(SunProxy, "_instance"):
            with SunProxy._instance_lock:
                if not hasattr(SunProxy, "_instance"):
                    SunProxy._instance = object.__new__(cls)

    @classmethod
    def set(cls, key, value):
        cls._data[key] = value

    @classmethod
    def get(cls, key):
        return cls._data.get(key)

    @classmethod
    def delete(cls, key):
        if key in cls._data:
            del cls._data[key]


class SunRequests(object):
    def __init__(self, sun_proxy: SunProxy = None) -> None:
        super().__init__()
        self.sun_proxy = sun_proxy

    def request(self, method='get', url=None, times=3, retry_wait_time=1588, proxies=None, wait_time=None, **kwargs):
        """
        简单封装的请求，参考requests，增加循环次数和次数之间的等待时间
        :param proxies: 代理配置
        :param method: 请求方法： get；post
        :param url: url
        :param times: 次数，int
        :param retry_wait_time: 重试等待时间，毫秒
        :param wait_time: 等待时间：毫秒；表示每个请求的间隔时间，在请求之前等待sleep，主要用于防止请求太频繁的限制。
        :param kwargs: 其它 requests 参数，用法相同
        :return: res
        """
        # 1. 获取设置代理
        proxies = self.__get_proxies(proxies)
        # 避免单次网络请求无限阻塞：未显式传入 timeout 时，提供默认超时。
        # tuple 语义: (connect_timeout, read_timeout)
        kwargs.setdefault('timeout', (5, 20))
        payload_text = self.__format_payload(kwargs)
        # 2. 请求数据结果
        res = None
        last_exc = None
        for i in range(times):
            if wait_time:
                time.sleep(wait_time / 1000)
            try:
                res = requests.request(method=method, url=url, proxies=proxies, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                print(
                    f"[http error] {method.upper()} {url} payload={payload_text} "
                    f"attempt={i + 1}/{times} status=NA failed: {exc}",
                    flush=True,
                )
                if i == times - 1:
                    raise
                time.sleep(retry_wait_time / 1000)
                continue

            if res.status_code in (200, 404):
                return res

            print(
                f"[http warn] {method.upper()} {url} payload={payload_text} "
                f"attempt={i + 1}/{times} status={res.status_code}",
                flush=True,
            )
            time.sleep(retry_wait_time / 1000)
            if i == times - 1:
                return res
        if last_exc is not None:
            raise last_exc
        return res

    def __get_proxies(self, proxies):
        """
        获取代理配置
        """
        if proxies is None:
            proxies = {}
        is_proxy = SunProxy.get('is_proxy')
        ip = SunProxy.get('ip')
        proxy_url = SunProxy.get('proxy_url')
        if not ip and is_proxy and proxy_url:
            ip = requests.get(url=proxy_url).text.replace('\r\n', '') \
                .replace('\r', '').replace('\n', '').replace('\t', '')
        if is_proxy and ip:
            if ip.startswith('http'):
                proxies = {'https': f"{ip}", 'http': f"{ip}"}
            else:
                proxies = {'https': f"http://{ip}", 'http': f"http://{ip}"}
        return proxies

    @staticmethod
    def __format_payload(kwargs):
        params = kwargs.get('params')
        json_data = kwargs.get('json')
        data = kwargs.get('data')
        payload = {}
        if params is not None:
            payload['params'] = params
        if json_data is not None:
            payload['json'] = json_data
        if data is not None:
            payload['data'] = data
        if not payload:
            return "{}"
        payload_text = str(payload)
        if len(payload_text) > 500:
            payload_text = payload_text[:500] + "...(truncated)"
        return payload_text


sun_requests = SunRequests()
