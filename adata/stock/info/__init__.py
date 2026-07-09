# -*- coding: utf-8 -*-
"""
@desc: 基本信息相关数据
@author: 1nchaos
@time: 2023/3/28
@log: change log
"""
from adata.stock.info.concept.stock_concept import StockConcept
from adata.stock.info.stock_code import StockCode
from adata.stock.info.stock_index import StockIndex
from adata.stock.info.stock_info import StockInfo
from adata.stock.info.trade_calendar import TradeCalendar


class Info(StockCode, StockConcept, TradeCalendar, StockIndex, StockInfo):
    """股票基础信息门面。

    多继承用于聚合股票代码、概念、交易日历、指数成分、公司信息等查询方法。
    对 Java 背景来说，可以把它理解为一个组合了多个查询 service 的 facade。
    """

    def __init__(self) -> None:
        super().__init__()


# 对外暴露的基础信息门面实例。
info = Info()
