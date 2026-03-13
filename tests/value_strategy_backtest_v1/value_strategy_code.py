# -*- coding: utf-8 -*-
import adata
import pandas as pd
import numpy as np
import os
import datetime
import concurrent.futures
from adata.stock.market.stock_market.stock_market_baidu import StockMarketBaiDu
from adata.stock.finance.core import Core

import pickle

# 禁用代理
os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['all_proxy'] = ''

class ValueStrategyV2:
    def __init__(self, initial_capital=1000000, max_positions=10):
        self.baidu_market = StockMarketBaiDu()
        self.core_finance = Core()
        self.market_index_df = None
        self.stock_data_cache = {}  # {code: df_kline}
        self.finance_data_cache = {} # {code: df_finance}
        
        # 资金账户
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}  # {code: {'buy_date', 'buy_price', 'shares', 'cost', 'ma200'}}
        self.max_positions = max_positions
        self.completed_trades = [] # List of closed trades for CSV
        
        self.cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data', 'cache')
        os.makedirs(os.path.join(self.cache_dir, 'kline'), exist_ok=True)
        os.makedirs(os.path.join(self.cache_dir, 'finance'), exist_ok=True)
        self.full_cache_file = os.path.join(self.cache_dir, 'full_data_processed.pkl')

    def fetch_market_env(self, start_date):
        print("正在获取大盘基准数据 (沪深300)...")
        df = adata.stock.market.get_market_index(index_code='000300', start_date=start_date)
        if df is None or df.empty:
             self.market_index_df = pd.DataFrame()
             return
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        self.market_index_df = df.set_index('trade_date')
        
    def _load_stock_data(self, code, start_date):
        """加载数据：优先本地缓存，计算 MA200"""
        kline_path = os.path.join(self.cache_dir, 'kline', f'{code}.csv')
        finance_path = os.path.join(self.cache_dir, 'finance', f'{code}.csv')
        
        try:
            # 1. 加载 K 线数据 (多加载一些以便计算 MA200)
            if os.path.exists(kline_path):
                df_k = pd.read_csv(kline_path)
            else:
                # 为了计算200均线，开始日期往前推1年
                s_dt = (datetime.datetime.strptime(start_date, '%Y-%m-%d') - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
                df_k = self.baidu_market.get_market(stock_code=code, start_date=s_dt)
                if df_k is not None and not df_k.empty:
                    df_k.to_csv(kline_path, index=False)
            
            if df_k is None or df_k.empty: return None
            
            # 2. 加载财务数据
            if os.path.exists(finance_path):
                df_f = pd.read_csv(finance_path)
            else:
                df_f = self.core_finance.get_core_index(stock_code=code)
                if df_f is not None and not df_f.empty:
                    df_f.to_csv(finance_path, index=False)
                    
            if df_f is None or df_f.empty: return None

            # ETL K线
            cols = ['open', 'close', 'high', 'low', 'volume', 'pre_close']
            for col in cols:
                df_k[col] = pd.to_numeric(df_k[col], errors='coerce')
            df_k['trade_date_str'] = pd.to_datetime(df_k['trade_time']).dt.strftime('%Y-%m-%d')
            df_k = df_k.sort_values('trade_time').drop_duplicates('trade_date_str')
            
            # 计算 MA200
            df_k['ma200'] = df_k['close'].rolling(window=200).mean()
            df_k = df_k.set_index('trade_date_str')
            
            # ETL 财务数据
            df_f['notice_date'] = pd.to_datetime(df_f['notice_date']).dt.strftime('%Y-%m-%d')
            df_f = df_f.sort_values('notice_date')
            # 转换数值
            num_cols = ['basic_eps', 'net_asset_ps', 'roe_wtd', 'non_gaap_net_profit_yoy_gr', 'oper_cf_ps']
            for col in num_cols:
                df_f[col] = pd.to_numeric(df_f[col], errors='coerce')

            return code, df_k, df_f
        except Exception as e:
            return None

    def preload(self, codes, start_date):
        # 1. 尝试从全量缓存加载 (避免读取数千个小 CSV 文件)
        if os.path.exists(self.full_cache_file):
            print(f"检测到全量缓存文件，正在从磁盘加载已处理数据...")
            try:
                with open(self.full_cache_file, 'rb') as f:
                    cache_data = pickle.load(f)
                    self.stock_data_cache = cache_data['stock']
                    self.finance_data_cache = cache_data['finance']
                print(f"从缓存加载完成: 有效股票 {len(self.stock_data_cache)}")
                return
            except Exception as e:
                print(f"缓存加载失败: {e}，将重新从 CSV 加载...")

        # 2. 如果没缓存，则正常并行加载
        valid_codes = [c for c in codes if not c.startswith('2')]
        print(f"原始股票数: {len(codes)}, 过滤后 (排除2开头): {len(valid_codes)}")
        
        print(f"正在并行加载并处理数据 (并发=20)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(self._load_stock_data, c, start_date): c for c in valid_codes}
            done = 0
            for f in concurrent.futures.as_completed(futures):
                res = f.result()
                if res:
                    code, df_k, df_f = res
                    self.stock_data_cache[code] = df_k
                    self.finance_data_cache[code] = df_f
                done += 1
                if done % 500 == 0:
                    print(f"进度: {done}/{len(valid_codes)}...")
        
        # 3. 保存全量缓存以便下次秒开
        print(f"正在保存全量缓存到磁盘...")
        try:
            with open(self.full_cache_file, 'wb') as f:
                pickle.dump({
                    'stock': self.stock_data_cache,
                    'finance': self.finance_data_cache
                }, f)
        except Exception as e:
            print(f"缓存保存失败: {e}")
            
        print(f"加载完成: 有效股票 {len(self.stock_data_cache)}")

    def get_finance_indicators(self, code, today):
        """获取财务指标估算 (TTM)"""
        df_f = self.finance_data_cache.get(code)
        if df_f is None: return None
        available = df_f[df_f['notice_date'] <= today]
        if available.empty: return None
        
        latest = available.iloc[-1]
        
        # 简化计算：
        # ROE(TTM) 我们用 roe_wtd 结合报告期
        # PE(TTM) 我们用最近报告期 annualized eps
        # 实际回测中，财务数据通常用最近一期经过简单年化处理
        eps = latest['basic_eps']
        bps = latest['net_asset_ps']
        roe = latest['roe_wtd']
        profit_yoy = latest['non_gaap_net_profit_yoy_gr']
        ocf = latest['oper_cf_ps']
        
        # 判断报告期 (q1=3, q2=6, q3=9, q4=12)
        # 这里我们简单化：如果 roe 已经大于 10，则满足；
        # 如果是季度报表，我们将 roe 简单年化处理以便比较
        # 假设 basic_eps 也是本年累计
        return {
            'eps': eps, 'bps': bps, 'roe': roe, 
            'profit_yoy': profit_yoy, 'ocf': ocf
        }

    def run(self, start_date, end_date):
        if self.market_index_df is None: self.fetch_market_env(start_date)
        all_dates = sorted(self.market_index_df.index)
        dates = [d for d in all_dates if start_date <= d <= end_date]
        
        print(f"开始回测: {dates[0]} ~ {dates[-1]}")
        self.equity_curve = []
        rebalance_period = 20
        
        for idx, today in enumerate(dates):
            # 1. 每日卖出检查 (价格跌破 MA200)
            to_sell = []
            for code, pos in self.positions.items():
                df_k = self.stock_data_cache.get(code)
                if today not in df_k.index: continue
                
                curr_price = df_k.loc[today, 'close']
                ma200 = df_k.loc[today, 'ma200']
                
                if curr_price < ma200:
                    # 卖出
                    revenue = (curr_price * pos['shares']) * (1 - 0.0013)
                    self.cash += revenue
                    profit_amt = revenue - pos['cost']
                    self.completed_trades.append({
                        'code': code,
                        'buy_date': pos['buy_date'],
                        'buy_price': pos['buy_price'],
                        'sell_date': today,
                        'sell_price': round(curr_price, 2),
                        'shares': pos['shares'],
                        'amount': round(revenue, 2),
                        'profit': round(profit_amt, 2),
                        'profit_pct': f"{(profit_amt / pos['cost'] * 100):.2f}%",
                        'reason': '跌破MA200'
                    })
                    to_sell.append(code)
            
            for code in to_sell:
                del self.positions[code]

            # 2. 调仓日买入逻辑
            if idx % rebalance_period == 0:
                # 寻找符合条件的候选股
                candidates = []
                for code, df_k in self.stock_data_cache.items():
                    if today not in df_k.index or code in self.positions: continue
                    
                    row_k = df_k.loc[today]
                    price = row_k['close']
                    ma200 = row_k['ma200']
                    
                    # 价格条件
                    if pd.isna(ma200) or price <= ma200: continue
                    
                    # 财务条件
                    fin = self.get_finance_indicators(code, today)
                    if not fin or fin['eps'] <= 0 or fin['bps'] <= 0: continue
                    
                    pe = price / fin['eps']
                    pb = price / fin['bps']
                    
                    # 核心条件: 
                    # PE < 40, PB < 4, ROE > 10%, 扣非净利增长 > 0, 现金流 > 0
                    if pe < 40 and pb < 4 and fin['roe'] > 10 and fin['profit_yoy'] > 0 and fin['ocf'] > 0:
                        candidates.append({'code': code, 'price': price, 'pe': pe})
                
                # 按 PE 从低到高排序
                candidates = sorted(candidates, key=lambda x: x['pe'])
                
                # 执行买入
                while len(self.positions) < self.max_positions and candidates:
                    can = candidates.pop(0)
                    available_slot = self.max_positions - len(self.positions)
                    unit_cash = self.cash / available_slot
                    
                    shares = (int(unit_cash / can['price']) // 100) * 100
                    if shares >= 100:
                        cost = shares * can['price']
                        self.cash -= cost
                        self.positions[can['code']] = {
                            'buy_date': today,
                            'buy_price': can['price'],
                            'shares': shares,
                            'cost': cost
                        }

            # 3. 统计权益
            mkt_val = 0
            for code, pos in self.positions.items():
                df_k = self.stock_data_cache.get(code)
                p = df_k.loc[today, 'close'] if today in df_k.index else pos['buy_price']
                mkt_val += p * pos['shares']
            
            total_equity = self.cash + mkt_val
            benchmark_close = self.market_index_df.loc[today, 'close']
            if not hasattr(self, 'benchmark_base'): self.benchmark_base = benchmark_close
            
            self.equity_curve.append({
                'date': today, 
                'total': total_equity,
                'benchmark': (benchmark_close / self.benchmark_base) * self.initial_capital
            })

        return total_equity

    def save_logs(self, out_dir):
        # 1. 交易日志：合拢买卖，按代码排序
        if self.completed_trades:
            # 加上当前持仓但也记录一下（用于展示）
            all_logs = list(self.completed_trades)
            for code, pos in self.positions.items():
                # 计算浮盈
                df_k = self.stock_data_cache[code]
                last_p = df_k.iloc[-1]['close']
                profit = (last_p * pos['shares']) - pos['cost']
                all_logs.append({
                    'code': code, 'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                    'sell_date': '持仓中', 'sell_price': last_p, 'shares': pos['shares'],
                    'amount': '-', 'profit': round(profit, 2),
                    'profit_pct': f"{(profit/pos['cost']*100):.2f}%", 'reason': '未卖出'
                })
            
            df_trade = pd.DataFrame(all_logs).sort_values('code')
            df_trade.to_csv(os.path.join(out_dir, 'value_trade_log.csv'), index=False, encoding='utf-8-sig')
            print(f"✅ 交易日志已保存 (按股票代码排序，单行展示买卖)")

        # 2. 净值曲线
        pd.DataFrame(self.equity_curve).to_csv(os.path.join(out_dir, 'value_equity_curve.csv'), index=False, encoding='utf-8-sig')

    def plot_results(self, save_path):
        """生成全面的可视化回测报告"""
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import numpy as np
            plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'SimHei']
            plt.rcParams['axes.unicode_minus'] = False
            
            # 数据准备
            df_equity = pd.DataFrame(self.equity_curve)
            df_equity['date'] = pd.to_datetime(df_equity['date'])
            df_equity.set_index('date', inplace=True)
            
            # 创建画布 (2x2 布局)
            fig = plt.figure(figsize=(20, 15))
            gs = fig.add_gridspec(3, 2)
            
            # --- 1. 净值曲线图 (跨两列) ---
            ax1 = fig.add_subplot(gs[0, :])
            ax1.plot(df_equity.index, df_equity['total'], label='价值精选策略', color='#d35400', lw=2.5)
            ax1.plot(df_equity.index, df_equity['benchmark'], label='沪深300指数', color='#7f8c8d', lw=1.5, ls='--')
            ax1.set_title('策略净值曲线对比', fontsize=16, fontweight='bold')
            ax1.legend(loc='upper left')
            ax1.grid(True, alpha=0.3)
            
            # --- 2. 年度收益率 (左中) ---
            ax2 = fig.add_subplot(gs[1, 0])
            yearly_ret = df_equity['total'].resample('YE').last().pct_change()
            yearly_ret.iloc[0] = (df_equity['total'].resample('YE').last().iloc[0] / self.initial_capital) - 1
            years = [str(d.year) for d in yearly_ret.index]
            colors = ['#27ae60' if x > 0 else '#e74c3c' for x in yearly_ret]
            bars = ax2.bar(years, yearly_ret * 100, color=colors, alpha=0.8)
            ax2.set_title('年度收益率 (%)', fontsize=14)
            ax2.axhline(0, color='black', lw=0.8)
            for bar in bars:
                height = bar.get_height()
                ax2.text(bar.get_x() + bar.get_width()/2., height + (1 if height > 0 else -3),
                        f'{height:.1f}%', ha='center', va='bottom', fontsize=11)

            # --- 3. 月度收益热力图 (右中) ---
            ax3 = fig.add_subplot(gs[1, 1])
            monthly_ret = df_equity['total'].resample('ME').last().pct_change()
            monthly_ret.iloc[0] = (df_equity['total'].resample('ME').last().iloc[0] / self.initial_capital) - 1
            
            pivot_df = pd.DataFrame({
                'Year': monthly_ret.index.year,
                'Month': monthly_ret.index.month,
                'Return': monthly_ret.values
            }).pivot(index='Year', columns='Month', values='Return').fillna(0)
            
            im = ax3.imshow(pivot_df.values, cmap='RdYlGn', aspect='auto')
            ax3.set_title('月度收益热力图', fontsize=14)
            ax3.set_xticks(range(len(pivot_df.columns)))
            ax3.set_xticklabels([f'{m}月' for m in pivot_df.columns])
            ax3.set_yticks(range(len(pivot_df.index)))
            ax3.set_yticklabels(pivot_df.index)
            # 在格子里写数值
            for i in range(len(pivot_df.index)):
                for j in range(len(pivot_df.columns)):
                    val = pivot_df.values[i, j]
                    ax3.text(j, i, f'{val:.1%}', ha='center', va='center', 
                            color='black' if -0.02 < val < 0.02 else 'white', fontsize=9)

            # --- 4. 个股收益明细 (底部全宽) ---
            ax4 = fig.add_subplot(gs[2, :])
            # 统计个股总利润
            stock_profits = {}
            for t in self.completed_trades:
                stock_profits[t['code']] = stock_profits.get(t['code'], 0) + t['profit']
            for code, pos in self.positions.items():
                last_p = self.stock_data_cache[code].iloc[-1]['close']
                floating_profit = (last_p * pos['shares']) - pos['cost']
                stock_profits[code] = stock_profits.get(code, 0) + floating_profit
            
            # 取前 20 只或全部
            sorted_stocks = sorted(stock_profits.items(), key=lambda x: x[1], reverse=True)
            if len(sorted_stocks) > 30:
                top_stocks = sorted_stocks[:15] + sorted_stocks[-15:] # 展示头尾
            else:
                top_stocks = sorted_stocks
            
            codes = [x[0] for x in top_stocks]
            profits = [x[1] for x in top_stocks]
            colors = ['#2ecc71' if x > 0 else '#e74c3c' for x in profits]
            
            ax4.bar(range(len(codes)), profits, color=colors)
            ax4.set_xticks(range(len(codes)))
            ax4.set_xticklabels(codes, rotation=45, fontsize=10)
            ax4.set_title('个股累计盈亏明细 (RMB)', fontsize=14)
            ax4.grid(axis='y', alpha=0.3)

            plt.tight_layout()
            plt.savefig(save_path, dpi=120)
            print(f"� 综合分析报表已生成: {save_path}")
        except Exception as e:
            print(f"绘图失败: {e}")

if __name__ == '__main__':
    START = (datetime.datetime.now() - datetime.timedelta(days=730)).strftime('%Y-%m-%d')
    END = datetime.datetime.now().strftime('%Y-%m-%d')
    
    # 获取代码名单
    try:
        df_all = adata.stock.info.all_code()
        codes = df_all['stock_code'].tolist()
    except:
        # 兜底名单
        codes = ['600519', '000858', '601318', '600036', '000001', '000333', '600276', '600309']
 
    strategy = ValueStrategyV2(max_positions=15)
    strategy.preload(codes, START)
    strategy.run(START, END)
    
    out_dir = os.path.dirname(os.path.abspath(__file__))
    strategy.save_logs(out_dir)
    # 生成综合图表报告
    strategy.plot_results(os.path.join(out_dir, 'strategy_analysis_report.png'))
    
    print(f"\n✨ 回测任务完成！请在目录下查看 strategy_analysis_report.png 和 CSV 日志。")


