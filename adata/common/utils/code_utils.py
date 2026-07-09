# -*- coding: utf-8 -*-
"""
@desc: readme
@author: 1nchaos
@time: 2023/12/4
@log: change log
"""
exchange_suffix = {
    '00': '.SZ',
    '20': '.SZ',
    '30': '.SZ',
    '43': '.BJ',
    '60': '.SH',
    '68': '.SH',
    '83': '.BJ',
    '87': '.BJ',
    '90': '.SH',
    '92': '.BJ',
}


def compile_exchange_by_stock_code(stock_code):
    """根据股票代码补全市场后缀。

    A 股代码前两位通常能判断交易所，例如 60/68 -> SH，00/30 -> SZ。
    返回值形如 ``600000.SH``，便于不同数据源之间统一股票代码格式。
    """

    prefix = stock_code[0:2]
    if prefix in exchange_suffix:
        return stock_code + exchange_suffix[prefix]
    return stock_code


def get_exchange_by_stock_code(stock_code):
    """根据股票代码补全市场后缀"""
    return exchange_suffix[stock_code[0:2]][1:]


if __name__ == '__main__':
    print(compile_exchange_by_stock_code('200039'))
    print(get_exchange_by_stock_code('200039'))
