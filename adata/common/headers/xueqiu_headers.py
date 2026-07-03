# -*- coding: utf-8 -*-
"""
@desc: 雪球（xueqiu.com）请求头
       参照 sina_headers、ths_headers 风格定义。
       - web_headers：用于匿名访问 https://xueqiu.com 首页获取会话 Cookie
       - json_headers：用于访问自选股 / 用户动态等 JSON 数据接口
@author: xueqiu-user-monitor
"""

# 访问雪球首页获取匿名会话 Cookie 时使用的请求头
web_headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
    'Accept-Encoding': 'gzip, deflate, br',
    'Host': 'xueqiu.com',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
}

# 访问雪球 JSON 数据接口（自选股、用户动态等）时使用的请求头
json_headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://xueqiu.com/',
    'Connection': 'keep-alive',
}
