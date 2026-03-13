#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
强势回踩趋势策略 v2.2 - 优化版
改进：
1. ATR止损倍数从1.5降到1.2（更紧）
2. 移动止盈从10%收紧到8%（减少回吐）
3. 增加MA20向上趋势过滤
4. 提高选股标准：20日涨幅从20%提高到22%
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import adata
import os
import glob
import concurrent.futures
from adata.stock.market.stock_market.stock_market_baidu import StockMarketBaiDu
import warnings
warnings.filterwarnings('ignore')

os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['all_proxy'] = ''

# 创建数据存储目录
DATA_DIR = 'stock_data'
os.makedirs(DATA_DIR, exist_ok=True)


class TrendPullbackStrategy:

    def __init__(self, initial_capital=1000000, max_positions=5, risk_per_trade=0.02, 
                 use_local_data=True, atr_multiplier=1.2, trailing_stop_pct=0.08):
        self.baidu_market = StockMarketBaiDu()
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.max_positions = max_positions
        self.risk_per_trade = risk_per_trade
        self.use_local_data = use_local_data
        self.atr_multiplier = atr_multiplier  # ATR止损倍数
        self.trailing_stop_pct = trailing_stop_pct  # 移动止盈回撤比例

        self.positions = []
        self.trade_log = []
        self.equity_curve = []
        self.stock_data_cache = {}

    # ================= 本地数据管理 =================
    
    def _get_stock_file_path(self, code):
        """获取股票数据文件路径"""
        return os.path.join(DATA_DIR, f"{code}.csv")
    
    def _load_stock_from_file(self, code, start_date):
        """从本地文件加载股票数据"""
        file_path = self._get_stock_file_path(code)
        
        if not os.path.exists(file_path):
            return None
        
        try:
            df = pd.read_csv(file_path, index_col='trade_date_str')
            
            # 检查数据是否包含所需日期范围
            if df.empty:
                return None
            
            # 过滤日期
            df = df[df.index >= start_date]
            
            if len(df) < 100:
                return None
            
            return code, df
            
        except Exception as e:
            print(f"读取本地文件 {code} 失败: {str(e)}")
            return None
    
    def _save_stock_to_file(self, code, df):
        """保存股票数据到本地文件"""
        file_path = self._get_stock_file_path(code)
        try:
            df.to_csv(file_path, encoding='utf-8-sig')
        except Exception as e:
            print(f"保存文件 {code} 失败: {str(e)}")

    # ================= 数据加载 =================

    def _load_stock_data(self, code, start_date):
        """加载单只股票数据并计算指标"""
        try:
            df = self.baidu_market.get_market(stock_code=code, start_date=start_date)
            if df is None or df.empty or len(df) < 100:
                return None

            # 数据类型转换
            cols = ['open','close','high','low','volume','pre_close','turnover_ratio','change_pct']
            for col in cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            df['trade_date_str'] = pd.to_datetime(df['trade_time']).dt.strftime('%Y-%m-%d')
            df = df.sort_values('trade_time').set_index('trade_date_str')

            # 均线系统
            df['ma10'] = df['close'].rolling(10).mean()
            df['ma20'] = df['close'].rolling(20).mean()
            df['ma60'] = df['close'].rolling(60).mean()

            # 成交量指标
            df['vol_ma10'] = df['volume'].rolling(10).mean()

            # 涨幅
            df['gain_20d'] = df['close']/df['close'].shift(20)-1

            # 成交量放大（近3日有放量）
            df['vol_surge'] = (df['volume'] > df['vol_ma10']*1.5).rolling(3).max()

            # 回踩MA20（最低价触及MA20，收盘价站上MA20）
            df['near_ma20'] = (df['low'] <= df['ma20']*1.02) & (df['close'] > df['ma20'])

            # ATR（真实波动幅度）
            df['tr1'] = df['high'] - df['low']
            df['tr2'] = abs(df['high'] - df['close'].shift(1))
            df['tr3'] = abs(df['low'] - df['close'].shift(1))
            df['tr'] = df[['tr1','tr2','tr3']].max(axis=1)
            df['atr14'] = df['tr'].rolling(14).mean()

            return code, df

        except Exception as e:
            print(f"加载 {code} 失败: {str(e)}")
            return None

    def preload_stocks(self, codes, start_date):
        """并发加载所有股票数据（优先使用本地文件）"""
        
        # 统计本地文件和需要下载的股票
        local_codes = []
        download_codes = []
        
        if self.use_local_data:
            print(f"检查本地数据文件...")
            for code in codes:
                if os.path.exists(self._get_stock_file_path(code)):
                    local_codes.append(code)
                else:
                    download_codes.append(code)
            
            print(f"本地已有: {len(local_codes)} 支")
            print(f"需要下载: {len(download_codes)} 支")
            
            # 加载本地数据
            if local_codes:
                print(f"加载本地数据...")
                loaded = 0
                for code in local_codes:
                    res = self._load_stock_from_file(code, start_date)
                    if res:
                        self.stock_data_cache[res[0]] = res[1]
                        loaded += 1
                        if loaded % 100 == 0:
                            print(f"已加载: {loaded}/{len(local_codes)}")
                print(f"本地数据加载完成: {loaded} 支")
        else:
            download_codes = codes
        
        # 下载缺失的数据
        if download_codes:
            print(f"\n开始下载 {len(download_codes)} 支股票数据...")
            loaded = 0
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                futures = {executor.submit(self._load_stock_data, c, start_date): c for c in download_codes}
                
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res:
                        code, df = res
                        self.stock_data_cache[code] = df
                        # 保存到本地
                        self._save_stock_to_file(code, df)
                        loaded += 1
                        if loaded % 50 == 0:
                            print(f"已下载: {loaded}/{len(download_codes)}")
            
            print(f"下载完成: {loaded} 支")
        
        print(f"\n总计加载 {len(self.stock_data_cache)} 支股票数据")

    # ================= 选股 =================

    def screen(self, date_str):
        """
        选股逻辑：强势回踩（加强版）
        1. 20日涨幅>20%（从15%提高）
        2. 多头排列：close > ma20 > ma60
        3. 近3日成交量放大
        4. 回踩MA20支撑
        5. MA20向上（趋势确认）
        """
        candidates = []
        
        for code, df in self.stock_data_cache.items():
            if date_str not in df.index:
                continue
            
            row = df.loc[date_str]

            # 检查必要字段
            if pd.isna(row['gain_20d']) or pd.isna(row['ma20']) or pd.isna(row['ma60']):
                continue
            
            # 获取前一天的MA20（用于判断趋势）
            idx = df.index.get_loc(date_str)
            if idx < 1:
                continue
            prev_ma20 = df.iloc[idx-1]['ma20']
            ma20_trend_up = row['ma20'] > prev_ma20  # MA20向上

            # 选股条件（加强）
            if (row['gain_20d'] > 0.20 and  # 从15%提高到20%
                row['close'] > row['ma20'] and
                row['ma20'] > row['ma60'] and
                ma20_trend_up and  # 新增：MA20向上
                row['vol_surge'] and
                row['near_ma20']):

                candidates.append({
                    'code': code,
                    'rank': row['gain_20d']
                })

        # 按涨幅排序
        candidates.sort(key=lambda x: x['rank'], reverse=True)
        return candidates

    # ================= 仓位计算 =================

    def calculate_position_size(self, price, stop_loss, total_equity):
        """
        仓位计算（Kelly公式变体）
        1. 风险金额 = 总资产 × 2%
        2. 每股风险 = 买入价 - 止损价
        3. 股数 = 风险金额 / 每股风险
        4. 限制：单票最大30%
        """
        # 风险金额
        risk_amount = total_equity * self.risk_per_trade
        risk_per_share = price - stop_loss

        if risk_per_share <= 0:
            return 0

        # 根据风险计算股数
        shares = int(risk_amount / risk_per_share)
        shares = (shares // 100) * 100  # 取整到100股

        # 单票最大30%限制
        max_capital = total_equity * 0.3
        max_shares = int(max_capital / price)
        max_shares = (max_shares // 100) * 100

        return min(shares, max_shares)

    # ================= 回测 =================

    def run_backtest(self, start_date, end_date):
        """运行回测"""
        
        # 加载指数数据（用于市场环境过滤和交易日判断）
        print("加载上证指数数据...")
        index_df = adata.stock.market.get_market_index(index_code='000001', start_date=start_date)
        index_df['close'] = pd.to_numeric(index_df['close'])
        index_df['ma60'] = index_df['close'].rolling(60).mean()
        index_df = index_df.set_index('trade_date')

        # 获取有效交易日列表
        dates = sorted(index_df.index.tolist())
        dates = [d for d in dates if start_date <= d <= end_date]
        valid_trade_dates = set(dates)  # 用于快速判断是否为交易日
        
        print(f"\n开始回测: {start_date} 至 {end_date}")
        print(f"交易日数: {len(dates)}")
        print("="*60)

        for i in range(1, len(dates)-1):

            today = dates[i]
            yesterday = dates[i-1]
            tomorrow = dates[i+1]
            
            # 验证交易日有效性
            if today not in valid_trade_dates:
                print(f"  警告: {today} 不是有效交易日，跳过")
                continue
            if tomorrow not in valid_trade_dates:
                print(f"  警告: {tomorrow} 不是有效交易日，跳过开仓")
                # 可以平仓，但不能开仓
                tomorrow = None

            # ====== 平仓逻辑 ======
            to_remove = []
            for pos in self.positions:
                df = self.stock_data_cache[pos['code']]
                if today not in df.index:
                    continue

                bar = df.loc[today]
                pos['high'] = max(pos['high'], bar['high'])  # 更新最高价

                exit_price = None
                reason = ""
                
                # T+1限制：买入当天不能卖出
                holding_days = (pd.to_datetime(today) - pd.to_datetime(pos['buy_date'])).days
                if holding_days < 1:
                    continue  # 跳过当天买入的股票

                # 止损：跌破ATR止损线
                if bar['low'] < pos['stop']:
                    exit_price = pos['stop']
                    reason = "ATR止损"

                # 止盈1：从最高点回撤（收紧到8%）
                elif bar['close'] < pos['high'] * (1 - self.trailing_stop_pct):
                    exit_price = bar['close']
                    reason = "移动止盈"

                # 止盈2：跌破MA10
                elif bar['close'] < bar['ma10']:
                    exit_price = bar['close']
                    reason = "跌破MA10"

                # 执行平仓
                if exit_price:
                    revenue = exit_price * pos['shares'] * (1-0.0013)  # 扣除手续费
                    self.cash += revenue
                    profit_pct = (revenue - pos['cost']) / pos['cost']
                    
                    buy_price_actual = pos['cost'] / pos['shares'] / 1.0013
                    holding_days = (pd.to_datetime(today) - pd.to_datetime(pos['buy_date'])).days
                    
                    # 数据验证
                    if holding_days < 1:
                        print(f"  警告: {pos['code']} 持仓天数异常({holding_days}天)，跳过")
                        continue
                    
                    # 计算最大浮盈
                    max_profit_pct = (pos['high'] - buy_price_actual) / buy_price_actual

                    self.trade_log.append({
                        'trade_id': len(self.trade_log) + 1,  # 交易序号
                        'code': pos['code'],
                        'buy_date': pos['buy_date'],
                        'exit_date': today,
                        'holding_days': holding_days,
                        'buy_price': buy_price_actual,
                        'exit_price': exit_price,
                        'shares': pos['shares'],  # 修复：使用持仓的股数
                        'cost': pos['cost'],
                        'revenue': revenue,
                        'profit': revenue - pos['cost'],
                        'profit_pct': profit_pct,
                        'stop_loss': pos['stop'],
                        'stop_loss_pct': (buy_price_actual - pos['stop']) / buy_price_actual,  # 止损幅度
                        'max_high': pos['high'],
                        'max_profit_pct': max_profit_pct,  # 最大浮盈
                        'reason': reason
                    })
                    
                    # 实时打印交易信息
                    print(f"[{today}] 卖出 {pos['code']} | "
                          f"买入价:{buy_price_actual:.2f} 卖出价:{exit_price:.2f} | "
                          f"持仓{holding_days}天 | 收益:{profit_pct:.2%} | {reason}")

                    to_remove.append(pos)

            # 移除已平仓的持仓
            for p in to_remove:
                self.positions.remove(p)

            # ====== 开仓逻辑 ======
            if len(self.positions) < self.max_positions and tomorrow is not None:

                # 市场环境过滤：指数在MA60上方才开仓
                if yesterday in index_df.index:
                    if index_df.loc[yesterday]['close'] < index_df.loc[yesterday]['ma60']:
                        continue

                # 选股
                candidates = self.screen(yesterday)

                # 开仓
                for stock in candidates[:self.max_positions-len(self.positions)]:

                    code = stock['code']
                    
                    # 避免重复持仓
                    if any(p['code']==code for p in self.positions):
                        continue

                    df = self.stock_data_cache[code]
                    if tomorrow not in df.index:
                        continue

                    # T+1开盘价成交
                    buy_price = df.loc[tomorrow]['open']
                    yesterday_close = df.loc[yesterday]['close']
                    atr = df.loc[yesterday]['atr14']
                    
                    if pd.isna(buy_price) or pd.isna(atr) or pd.isna(yesterday_close):
                        continue
                    
                    # 过滤条件1：跌停开盘不买（买不进去）
                    limit_down = yesterday_close * 0.90  # 跌停价
                    if buy_price <= limit_down * 1.001:  # 允许0.1%误差
                        print(f"  跳过 {code}: 跌停开盘({buy_price:.2f})")
                        continue
                    
                    # 过滤条件2：涨停开盘不买（追高风险大）
                    limit_up = yesterday_close * 1.10  # 涨停价
                    if buy_price >= limit_up * 0.999:  # 允许0.1%误差
                        print(f"  跳过 {code}: 涨停开盘({buy_price:.2f})")
                        continue
                    
                    # 过滤条件3：开盘价异常（低开超过5%或高开超过5%）
                    open_change = (buy_price - yesterday_close) / yesterday_close
                    if abs(open_change) > 0.05:
                        print(f"  跳过 {code}: 开盘异常({open_change:.2%})")
                        continue
                    
                    # 止损价：买入价 - ATR倍数×ATR
                    stop = buy_price - self.atr_multiplier * atr
                    
                    # 验证止损价合理性（止损不应超过10%）
                    stop_loss_pct = (buy_price - stop) / buy_price
                    if stop_loss_pct > 0.10:  # 止损超过10%，调整为10%
                        stop = buy_price * 0.90
                        print(f"  警告: {code} ATR止损过大({stop_loss_pct:.1%})，调整为10%")

                    # 计算当前总资产
                    market_value = sum(
                        p['shares']*self.stock_data_cache[p['code']].loc[today,'close']
                        for p in self.positions
                        if today in self.stock_data_cache[p['code']].index
                    )
                    total_equity = self.cash + market_value

                    # 计算仓位
                    shares = self.calculate_position_size(buy_price, stop, total_equity)

                    if shares >= 100:
                        cost = shares * buy_price * (1+0.0013)  # 含手续费
                        
                        if cost > self.cash:
                            continue
                        
                        self.cash -= cost

                        self.positions.append({
                            'code': code,
                            'buy_date': tomorrow,
                            'shares': shares,
                            'cost': cost,
                            'stop': stop,
                            'high': buy_price
                        })
                        
                        # 实时打印买入信息
                        position_pct = cost / total_equity * 100
                        print(f"[{tomorrow}] 买入 {code} | "
                              f"价格:{buy_price:.2f} 股数:{shares} | "
                              f"仓位:{position_pct:.1f}% 止损:{stop:.2f}")

            # ====== 记录资产曲线 ======
            market_value = sum(
                p['shares']*self.stock_data_cache[p['code']].loc[today,'close']
                for p in self.positions
                if today in self.stock_data_cache[p['code']].index
            )

            total = self.cash + market_value

            self.equity_curve.append({
                'date': today,
                'total': total,
                'cash': self.cash,
                'market_value': market_value,
                'positions': len(self.positions)
            })

        # ===== 绩效统计 =====
        self._print_performance()
        
        return self.equity_curve[-1]['total'] if self.equity_curve else self.initial_capital

    def _print_performance(self):
        """打印绩效统计"""
        
        eq = pd.DataFrame(self.equity_curve)
        
        if eq.empty:
            print("无交易数据")
            return
        
        # 最大回撤
        eq['cummax'] = eq['total'].cummax()
        eq['dd'] = eq['total']/eq['cummax'] - 1
        max_dd = eq['dd'].min()

        # 收益率
        total_return = eq['total'].iloc[-1] / self.initial_capital - 1
        years = len(eq) / 250
        annual_return = (1 + total_return) ** (1/years) - 1 if years > 0 else 0

        # 夏普比率（假设无风险利率3%）
        eq['daily_return'] = eq['total'].pct_change()
        sharpe = (eq['daily_return'].mean() * 250 - 0.03) / (eq['daily_return'].std() * np.sqrt(250))

        # 交易统计
        trades = pd.DataFrame(self.trade_log)

        print("\n" + "="*60)
        print("回测统计")
        print("="*60)
        
        print(f"总收益率: {total_return:.2%}")
        print(f"年化收益率: {annual_return:.2%}")
        print(f"最大回撤: {max_dd:.2%}")
        print(f"夏普比率: {sharpe:.2f}")
        print(f"交易次数: {len(trades)}")

        if not trades.empty:
            win_rate = (trades['profit_pct'] > 0).mean()
            avg_win = trades[trades['profit_pct'] > 0]['profit_pct'].mean()
            avg_loss = trades[trades['profit_pct'] < 0]['profit_pct'].mean()
            profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else np.nan
            avg_holding = trades['holding_days'].mean()
            avg_stop_loss_pct = trades['stop_loss_pct'].mean()
            avg_max_profit = trades['max_profit_pct'].mean()
            
            print(f"胜率: {win_rate:.2%}")
            print(f"平均盈利: {avg_win:.2%}")
            print(f"平均亏损: {avg_loss:.2%}")
            print(f"盈亏比: {profit_factor:.2f}")
            print(f"平均持仓天数: {avg_holding:.1f}天")
            print(f"平均止损幅度: {avg_stop_loss_pct:.2%}")
            print(f"平均最大浮盈: {avg_max_profit:.2%}")
            
            # 数据质量检查
            print(f"\n数据质量检查:")
            zero_holding = (trades['holding_days'] == 0).sum()
            if zero_holding > 0:
                print(f"  ⚠️  持仓0天的交易: {zero_holding}笔（异常）")
            else:
                print(f"  ✓ 无持仓0天的交易")
            
            large_stop = (trades['stop_loss_pct'] > 0.10).sum()
            if large_stop > 0:
                print(f"  ⚠️  止损>10%的交易: {large_stop}笔")
            else:
                print(f"  ✓ 止损幅度合理")
            
            # 显示最佳和最差交易
            print(f"\n最佳交易:")
            best = trades.nlargest(3, 'profit_pct')[['trade_id', 'code', 'buy_date', 'exit_date', 'holding_days', 'profit_pct', 'max_profit_pct', 'reason']]
            for idx, row in best.iterrows():
                print(f"  #{row['trade_id']} {row['code']}: {row['buy_date']} -> {row['exit_date']} ({row['holding_days']}天) | "
                      f"收益:{row['profit_pct']:.2%} 最高:{row['max_profit_pct']:.2%} | {row['reason']}")
            
            print(f"\n最差交易:")
            worst = trades.nsmallest(3, 'profit_pct')[['trade_id', 'code', 'buy_date', 'exit_date', 'holding_days', 'profit_pct', 'stop_loss_pct', 'reason']]
            for idx, row in worst.iterrows():
                print(f"  #{row['trade_id']} {row['code']}: {row['buy_date']} -> {row['exit_date']} ({row['holding_days']}天) | "
                      f"收益:{row['profit_pct']:.2%} 止损:{row['stop_loss_pct']:.2%} | {row['reason']}")
            
            # 按退出原因统计
            print(f"\n退出原因统计:")
            reason_stats = trades.groupby('reason').agg({
                'profit_pct': ['count', 'mean'],
                'holding_days': 'mean'
            }).round(4)
            reason_stats.columns = ['次数', '平均收益率', '平均持仓天数']
            print(reason_stats)
            
            # 保存详细交易记录
            output_dir = os.path.dirname(os.path.abspath(__file__))
            trade_log_path = os.path.join(output_dir, "trade_log_detail.csv")
            trades.to_csv(trade_log_path, index=False, encoding='utf-8-sig')
            print(f"\n交易明细已保存: {trade_log_path}")
        
        # 保存资金曲线
        output_dir = os.path.dirname(os.path.abspath(__file__))
        equity_curve_path = os.path.join(output_dir, "equity_curve.csv")
        eq.to_csv(equity_curve_path, index=False, encoding='utf-8-sig')
        print(f"资金曲线已保存: {equity_curve_path}")


# ================= 主程序 =================

if __name__ == '__main__':

    # 回测参数
    START = (datetime.now()-timedelta(days=365*2)).strftime('%Y-%m-%d')  # 最近2年
    END = datetime.now().strftime('%Y-%m-%d')

    print("="*60)
    print("强势回踩趋势策略 - 中证1000（优化版v2.2）")
    print("="*60)
    print(f"回测区间: {START} 至 {END}")
    print(f"初始资金: 1,000,000")
    print(f"最大持仓: 5只")
    print(f"单笔风险: 2%")
    print(f"选股条件: 20日涨幅>20%（加强）")
    print(f"ATR止损: 1.2倍ATR（收紧，最大10%）")
    print(f"移动止盈: 8%回撤（收紧）")
    print(f"T+1限制: 买入次日才能卖出")
    print("="*60)

    # 关闭代理
    adata.proxy(False)

    # 使用本地缓存的股票列表
    print("\n从本地缓存获取股票列表...")
    import glob
    stock_files = glob.glob(os.path.join(DATA_DIR, '*.csv'))
    codes = [os.path.basename(f).replace('.csv', '') for f in stock_files]
    print(f"股票数量: {len(codes)}")

    # 创建策略实例
    strategy = TrendPullbackStrategy(
        initial_capital=1000000,
        max_positions=5,
        risk_per_trade=0.02,
        use_local_data=True,     # 使用本地数据
        atr_multiplier=1.2,      # ATR止损倍数（从1.5优化为1.2）
        trailing_stop_pct=0.08   # 移动止盈（从10%优化为8%）
    )

    # 预加载数据（优先使用本地文件，缺失的自动下载）
    strategy.preload_stocks(codes, START)

    # 运行回测
    final = strategy.run_backtest(START, END)

    # 最终结果
    print("\n" + "="*60)
    print("最终结果")
    print("="*60)
    print(f"初始资金: 1,000,000")
    print(f"最终资产: {final:,.2f}")
    print(f"收益率: {(final/1000000-1):.2%}")
    print(f"成交次数: {len(strategy.trade_log)}")
    print("="*60)
