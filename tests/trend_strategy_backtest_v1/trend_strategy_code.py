# -*- coding: utf-8 -*-
import adata
import pandas as pd
import numpy as np
import os
import datetime
import concurrent.futures
import pickle
import matplotlib.pyplot as plt

# 禁用代理
os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''

class WeeklyTrendValueStrategy:
    def __init__(self, initial_capital=1000000, max_positions=15):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.max_positions = max_positions
        
        # 路径设置
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = os.path.join(base_dir, 'data', 'cache')
        self.kline_dir = os.path.join(self.cache_dir, 'kline')
        self.finance_dir = os.path.join(self.cache_dir, 'finance')
        self.full_cache_file = os.path.join(self.cache_dir, 'trend_value_enhanced.pkl')
        
        self.stock_data = {}  # {code: {'kline': df, 'finance': df}}
        self.positions = {}   # {code: {'buy_date', 'buy_price', 'shares', 'cost'}}
        self.completed_trades = []
        self.equity_curve = []
        self.market_index_df = None

    def fetch_benchmark(self, start_date):
        print("正在获取沪深300基准数据...")
        df = adata.stock.market.get_market_index(index_code='000300', start_date=start_date)
        if df is not None:
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            self.market_index_df = df.set_index('trade_date').sort_index()

    def _process_single_stock(self, filename):
        code = filename.replace('.csv', '')
        k_path = os.path.join(self.kline_dir, filename)
        f_path = os.path.join(self.finance_dir, filename)
        
        if not os.path.exists(f_path): return None
        
        try:
            # 1. 处理 K 线 (3周线=15日, 16周线=80日)
            df_k = pd.read_csv(k_path)
            if df_k.empty or len(df_k) < 100: return None
            df_k['close'] = pd.to_numeric(df_k['close'], errors='coerce')
            df_k['trade_date'] = pd.to_datetime(df_k['trade_time']).dt.strftime('%Y-%m-%d')
            df_k = df_k.sort_values('trade_time').drop_duplicates('trade_date')
            df_k['ma_fast'] = df_k['close'].rolling(window=15).mean()
            df_k['ma_slow'] = df_k['close'].rolling(window=80).mean()
            df_k = df_k.set_index('trade_date')
            
            # 2. 处理财务数据 (ROE, PE)
            df_f = pd.read_csv(f_path)
            df_f['notice_date'] = pd.to_datetime(df_f['notice_date']).dt.strftime('%Y-%m-%d')
            df_f = df_f.sort_values('notice_date')
            num_cols = ['basic_eps', 'net_asset_ps', 'roe_wtd']
            for col in num_cols:
                df_f[col] = pd.to_numeric(df_f[col], errors='coerce')
                
            return code, {'kline': df_k[['close', 'ma_fast', 'ma_slow']], 'finance': df_f}
        except:
            return None

    def load_data(self):
        if os.path.exists(self.full_cache_file):
            print("从磁盘加载已处理的【价值+趋势】增强版数据...")
            with open(self.full_cache_file, 'rb') as f:
                self.stock_data = pickle.load(f)
            print(f"完成，有效股票：{len(self.stock_data)}")
            return

        files = [f for f in os.listdir(self.kline_dir) if f.endswith('.csv')]
        print(f"开始并行处理基本面+技术面数据...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(self._process_single_stock, files))
            for res in results:
                if res:
                    code, data = res
                    self.stock_data[code] = data
        
        with open(self.full_cache_file, 'wb') as f:
            pickle.dump(self.stock_data, f)
        print(f"数据处理完成，总计 {len(self.stock_data)} 只股票已就绪。")

    def run_backtest(self, start_date, end_date):
        if self.market_index_df is None: self.fetch_benchmark(start_date)
        all_dates = sorted(self.market_index_df.index)
        dates = [d for d in all_dates if start_date <= d <= end_date]
        
        print(f"开始【价值增强趋势策略】回测...")
        
        for i, today in enumerate(dates):
            # 1. 卖出逻辑 (维持原样：死叉或跌破 16周线)
            to_sell = []
            for code, pos in self.positions.items():
                data = self.stock_data.get(code)
                df_k = data['kline']
                if today not in df_k.index: continue
                
                curr = df_k.loc[today]
                # 获取昨日判断死叉
                prev_date = dates[i-1] if i > 0 else None
                death_cross = False
                if prev_date and prev_date in df_k.index:
                    prev = df_k.loc[prev_date]
                    if prev['ma_fast'] >= prev['ma_slow'] and curr['ma_fast'] < curr['ma_slow']:
                        death_cross = True
                
                if death_cross or curr['close'] < curr['ma_slow']:
                    revenue = (curr['close'] * pos['shares']) * (1 - 0.0013)
                    self.cash += revenue
                    self.completed_trades.append({
                        'code': code, 'buy_date': pos['buy_date'], 'sell_date': today,
                        'profit_pct': f"{(revenue-pos['cost'])/pos['cost']*100:.2f}%",
                        'reason': '死叉' if death_cross else '股价跌破16周线'
                    })
                    to_sell.append(code)
            for c in to_sell: del self.positions[c]

            # 2. 买入逻辑 (增加基本面过滤)
            if len(self.positions) < self.max_positions:
                candidates = []
                for code, data in self.stock_data.items():
                    df_k = data['kline']
                    df_f = data['finance']
                    if today not in df_k.index or code in self.positions: continue
                    
                    # A. 财务指标筛选
                    fin = df_f[df_f['notice_date'] <= today]
                    if fin.empty: continue
                    latest_f = fin.iloc[-1]
                    if latest_f['roe_wtd'] < 10 or latest_f['basic_eps'] <= 0: continue
                    
                    curr_k = df_k.loc[today]
                    pe = curr_k['close'] / latest_f['basic_eps']
                    if pe > 45 or pe < 0: continue # 剔除过贵或亏损
                    
                    # B. 技术面金叉触发
                    prev_date = dates[i-1] if i > 0 else None
                    if not prev_date or prev_date not in df_k.index: continue
                    prev_k = df_k.loc[prev_date]
                    
                    golden_cross = (prev_k['ma_fast'] <= prev_k['ma_slow'] and curr_k['ma_fast'] > curr_k['ma_slow'])
                    stay_solid = (curr_k['close'] > curr_k['ma_fast'] and curr_k['close'] > curr_k['ma_slow'])
                    
                    if golden_cross and stay_solid:
                        candidates.append({'code': code, 'price': curr_k['close'], 'pe': pe})
                
                # 按 PE 从低到高买入 (买性价比最高的)
                candidates = sorted(candidates, key=lambda x: x['pe'])
                for can in candidates:
                    if len(self.positions) >= self.max_positions: break
                    unit_cash = self.cash / (self.max_positions - len(self.positions))
                    shares = (int(unit_cash / can['price']) // 100) * 100
                    if shares >= 100:
                        cost = shares * can['price']
                        self.cash -= cost
                        self.positions[can['code']] = {
                            'buy_date': today, 'buy_price': can['price'], 'shares': shares, 'cost': cost
                        }

            # 3. 统计权益
            mkt_val = 0
            for code, pos in self.positions.items():
                df_k = self.stock_data[code]['kline']
                p = df_k.loc[today, 'close'] if today in df_k.index else pos['buy_price']
                mkt_val += p * pos['shares']
            
            total = self.cash + mkt_val
            bench = self.market_index_df.loc[today, 'close']
            if i == 0: self.bench_base = bench
            self.equity_curve.append({
                'date': today, 'total': total,
                'benchmark': (bench / self.bench_base) * self.initial_capital
            })

    def plot_report(self, save_path):
        df = pd.DataFrame(self.equity_curve)
        df['date'] = pd.to_datetime(df['date'])
        plt.figure(figsize=(15, 8))
        plt.rcParams['font.sans-serif'] = ['Arial Unicode MS', 'SimHei']
        plt.plot(df['date'], df['total'], label='价值趋势增强策略', color='#e67e22', lw=2)
        plt.plot(df['date'], df['benchmark'], label='沪深300指数', color='gray', ls='--')
        plt.title('【价值+趋势】增强型策略回测报告 (3周/16周金叉+ROE筛选)', fontsize=14)
        plt.legend()
        plt.grid(True, alpha=0.3)
        plt.savefig(save_path)
        print(f"增强版报告已生成: {save_path}")

if __name__ == '__main__':
    strategy = WeeklyTrendValueStrategy(max_positions=15)
    strategy.load_data()
    
    START = (datetime.datetime.now() - datetime.timedelta(days=730)).strftime('%Y-%m-%d')
    END = datetime.datetime.now().strftime('%Y-%m-%d')
    
    strategy.run_backtest(START, END)
    
    out_dir = os.path.dirname(os.path.abspath(__file__))
    strategy.plot_report(os.path.join(out_dir, 'trend_strategy_report.png'))
    
    final_equity = strategy.equity_curve[-1]['total']
    print(f"\n✨ 策略优化完成！")
    print(f"最终资产: {final_equity:,.2f} RMB")
    print(f"优化后总收益率: {((final_equity/1000000)-1)*100:.2f}%")
