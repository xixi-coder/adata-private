# -*- coding: utf-8 -*-
"""
@desc: 行情相关的数据
@author: 1nchaos
@time: 2023/3/29
@log: change log
"""
from adata.stock.market.capital_flow import StockCapitalFlow
from adata.stock.market.concept_capital_flow import ConceptCapitalFlow
from adata.stock.market.concepth_market import StockMarketConcept
from adata.stock.market.index_market import StockMarketIndex
from adata.stock.market.stock_dividend import StockDividend
from adata.stock.market.stock_market import StockMarket


class Market(StockCapitalFlow, ConceptCapitalFlow, StockMarket, StockMarketConcept, StockDividend, StockMarketIndex):
    """行情模块门面。

    这里用多继承把几个数据能力组合到一个对象上，效果接近 Java 里一个 Facade
    同时委托多个 service。调用方可以通过 ``adata.stock.market.get_market(...)``
    或 ``adata.stock.market.get_capital_flow(...)`` 使用不同能力。
    """

    def __init__(self) -> None:
        super().__init__()


# 对外暴露的行情门面实例。
market = Market()
