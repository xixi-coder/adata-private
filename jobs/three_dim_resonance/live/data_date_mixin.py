# -*- coding: utf-8 -*-
import datetime
import os
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from jobs.common.a_share_metadata import is_excluded_short_name


class DataDateMixin:
    @staticmethod
    def _now_shanghai() -> datetime.datetime:
        return datetime.datetime.now(ZoneInfo("Asia/Shanghai"))

    def _filter_non_st(self):
        filtered = {}
        for code, df in self.stock_data.items():
            meta = self.metadata.get(code, {})
            short_name = meta.get("short_name", code)
            if is_excluded_short_name(short_name):
                continue
            filtered[code] = df
            self.stock_names[code] = short_name
        self.stock_data = filtered

    def load_data(self):
        if not os.path.exists(self.full_cache_file):
            raise FileNotFoundError(f"未找到缓存: {self.full_cache_file}")
        super().load_data()
        self._filter_non_st()
        print(f"非ST股票池: {len(self.stock_data)}")

    def _day_k_coverage(self, trade_date: str) -> tuple[int, int, float]:
        # 统计某交易日在股票池中的有效日K覆盖率，用于判断是否可作为候选依据日。
        total = len(self.stock_data)
        if total <= 0:
            return 0, 0, 0.0
        available = 0
        for df in self.stock_data.values():
            if df is None or trade_date not in df.index:
                continue
            row = df.loc[trade_date]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[-1]
            open_price = pd.to_numeric(row.get("open"), errors="coerce")
            close_price = pd.to_numeric(row.get("close"), errors="coerce")
            if np.isfinite(open_price) and np.isfinite(close_price):
                available += 1
        ratio = available / total
        return available, total, ratio

    def _resolve_trade_date(self, requested_date: str = "") -> str:
        # 优先级：
        # 1) 显式传入 requested_date（若在基准交易日中）；
        # 2) 否则优先今天（但需满足日K覆盖率阈值）；
        # 3) 若今天覆盖不足，则回退到上一个交易日。
        if self.benchmark_df is None:
            raise RuntimeError("请先加载基准数据")
        all_dates = list(self.benchmark_df.index)
        if not all_dates:
            raise RuntimeError("基准交易日为空")
        if requested_date and requested_date in self.benchmark_df.index:
            return requested_date
        now_date = self._now_shanghai().strftime("%Y-%m-%d")
        upper_bound_date = requested_date or now_date
        valid_dates = [d for d in all_dates if d <= upper_bound_date]
        if not valid_dates:
            raise RuntimeError("未找到可用交易日")
        if requested_date:
            return valid_dates[-1]

        # 默认优先今天；若今日日K覆盖不足，再自动回退到上一交易日。
        if now_date in self.benchmark_df.index:
            available, total, ratio = self._day_k_coverage(now_date)
            if ratio >= self.today_k_coverage_min:
                print(
                    f"今日日K覆盖率 {available}/{total} ({ratio:.1%})，"
                    f"使用今天 {now_date} 作为候选依据日。"
                )
                return now_date
            print(
                f"今日日K覆盖率 {available}/{total} ({ratio:.1%})，"
                "回退到上个交易日。"
            )

        prev_dates = [d for d in valid_dates if d < now_date]
        if prev_dates:
            return prev_dates[-1]
        return valid_dates[-1]

    def _next_trade_date(self, trade_date: str) -> str:
        # 仅基于 benchmark 的交易日序列求“下一交易日”。
        # 若已是最后一个交易日，则返回空串。
        all_dates = list(self.benchmark_df.index)
        idx = all_dates.index(trade_date)
        if idx >= len(all_dates) - 1:
            return ""
        return all_dates[idx + 1]
