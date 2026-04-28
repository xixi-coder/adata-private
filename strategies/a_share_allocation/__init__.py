# -*- coding: utf-8 -*-
__all__ = ["AShareAllocationStrategy", "StrategyConfig"]


def __getattr__(name):
    if name in __all__:
        from .strategy import AShareAllocationStrategy, StrategyConfig

        return {
            "AShareAllocationStrategy": AShareAllocationStrategy,
            "StrategyConfig": StrategyConfig,
        }[name]
    raise AttributeError(name)
