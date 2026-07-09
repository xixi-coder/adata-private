# -*- coding: utf-8 -*-
"""
@desc: adata
@author: 1nchaos
@time: 2023/4/4
"""
# -*- coding: utf-8 -*-

import logging

from adata.__version__ import __version__
from adata.bond import bond
from adata.common.utils.sunrequests import SunProxy
from adata.fund import fund
from adata.sentiment import sentiment
from adata.stock import stock
from adata.xueqiu import xueqiu


def version():
    """返回当前 adata 包版本号。"""
    return __version__


def proxy(is_proxy=False, ip: str = None, proxy_url: str = None):
    """
    设置请求代理
    :param is_proxy: 是否启用代理，默认：否
    :param ip: 代理ip地址；格式样例：192.123.123.4:4568
    :param proxy_url: 能获取到代理的url，返回格式必须和ip一样
    """
    # SunProxy 是一个进程内全局代理配置。这里设置后，项目里统一请求工具会读取它。
    SunProxy.set('is_proxy', is_proxy)
    SunProxy.set('ip', ip)
    SunProxy.set('proxy_url', proxy_url)
    return


# set up logging
logger = logging.getLogger("adata")


def set_logger():
    """初始化 adata 统一 logger。

    这个函数在模块导入时执行一次，类似 Java 应用启动时配置公共日志格式。
    """
    format_string = "%(asctime)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(format_string, datefmt="%Y-%m-%dT%H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    logger.addHandler(handler)


set_logger()
