# -*- coding: utf-8 -*-
"""
@desc: 专注股票相关的数据，为量化而生
@author: 1nchaos
@time: 2023/3/29
@log: change log
"""
from adata.stock.finance import finance
from adata.stock.index import index
from adata.stock.info import info
from adata.stock.market import market


class Stock(object):
    """股票模块聚合入口。

    Python 里常把一个模块实例暴露成全局对象。文件底部的 ``stock = Stock()``
    类似给使用者准备了一个门面对象，可以通过 ``adata.stock.market``、
    ``adata.stock.info`` 访问子能力。
    """

    def __init__(self) -> None:
        # 这里挂的是子模块导出的门面对象，不是重新 new 一套业务服务。
        self.info = info
        self.index = index
        self.market = market
        self.finance = finance


# 对外暴露的股票门面实例；用户通常直接用 adata.stock。
stock = Stock()
