# -*- coding: utf-8 -*-
"""
@desc: 异常-空值处理
@author: 1nchaos
@time: 2023/8/14
@log: change log
"""

import pandas as pd


def handler_null(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            func_name = getattr(func, "__qualname__", getattr(func, "__name__", "unknown"))
            print(f"[handler_null] {func_name} failed: {exc}", flush=True)
            return pd.DataFrame(data=[], columns=[])

    return wrapper
