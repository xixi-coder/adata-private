# -*- coding: utf-8 -*-
import adata
import pandas as pd
import numpy as np
import os
import datetime
import concurrent.futures
import itertools
from adata.stock.market.stock_market.stock_market_baidu import StockMarketBaiDu
from adata.stock.finance.core import Core
import pickle

# 禁用代理
os.environ['http_proxy'] = ''
os.environ['https_proxy'] = ''
os.environ['all_proxy'] = ''
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''
os.environ['ALL_PROXY'] = ''

class ValueStrategyV2:
    def __init__(
        self,
        initial_capital=1000000,
        max_positions=15,
        local_only=False,
        min_hold_days=15,
        cooldown_days=20,
        ret20_threshold=0.03,
        take_profit_pct=0.12,
        breakeven_arm_pct=0.08,
        breakeven_buffer=0.002,
        regime_filter=True
    ):
        self.baidu_market = StockMarketBaiDu()
        self.core_finance = Core()
        self.market_index_df = None
        self.stock_data_cache = {}
        self.finance_data_cache = {}
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}
        self.max_positions = max_positions
        self.local_only = local_only
        self.min_hold_days = min_hold_days
        self.cooldown_days = cooldown_days
        self.ret20_threshold = ret20_threshold
        self.take_profit_pct = take_profit_pct
        self.breakeven_arm_pct = breakeven_arm_pct
        self.breakeven_buffer = breakeven_buffer
        self.regime_filter = regime_filter
        self.cooldown_until = {}
        self.completed_trades = []
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.cache_dir = os.path.join(project_root, 'data', 'cache')
        os.makedirs(os.path.join(self.cache_dir, 'kline'), exist_ok=True)
        os.makedirs(os.path.join(self.cache_dir, 'finance'), exist_ok=True)
        self.full_cache_file = os.path.join(self.cache_dir, 'full_data_v2_5year.pkl')
        self.code_cache_file = os.path.join(self.cache_dir, 'all_codes.csv')

    def _ensure_tech_columns(self, df_k):
        if df_k is None or df_k.empty:
            return df_k
        if 'ma200' not in df_k.columns:
            df_k['ma200'] = df_k['close'].rolling(window=200).mean()
        if 'ma60' not in df_k.columns:
            df_k['ma60'] = df_k['close'].rolling(window=60).mean()
        if 'ma200_slope20' not in df_k.columns:
            df_k['ma200_slope20'] = df_k['ma200'] - df_k['ma200'].shift(20)
        if 'ret20' not in df_k.columns:
            df_k['ret20'] = df_k['close'] / df_k['close'].shift(20) - 1
        return df_k

    def fetch_market_env(self, start_date):
        cache_path = os.path.join(self.cache_dir, 'benchmark_000300.csv')
        df = None
        if os.path.exists(cache_path):
            df = pd.read_csv(cache_path)
            if not df.empty:
                if self.local_only:
                    print("使用本地缓存的沪深300指数数据（本地优先模式）")
                    df['close'] = pd.to_numeric(df['close'], errors='coerce')
                    df = df.sort_values('trade_date')
                    df['ma200'] = df['close'].rolling(window=200).mean()
                    self.market_index_df = df.set_index('trade_date')
                    return
                last_date = df['trade_date'].max()
                today_str = datetime.datetime.now().strftime('%Y-%m-%d')
                if last_date >= today_str:
                    print("使用本地缓存的沪深300指数数据")
                    df['close'] = pd.to_numeric(df['close'], errors='coerce')
                    df = df.sort_values('trade_date')
                    df['ma200'] = df['close'].rolling(window=200).mean()
                    self.market_index_df = df.set_index('trade_date')
                    return

        print("正在获取大盘基准数据 (沪深300)...")
        try:
            new_df = adata.stock.market.get_market_index(index_code='000300', start_date=start_date)
            if new_df is not None and not new_df.empty:
                if df is not None:
                    df = pd.concat([df, new_df]).drop_duplicates('trade_date').sort_values('trade_date')
                else:
                    df = new_df
                df.to_csv(cache_path, index=False)
        except Exception as e:
            print(f"获取指数数据失败: {e}")
        
        if df is None or df.empty:
             self.market_index_df = pd.DataFrame()
             return
        df['close'] = pd.to_numeric(df['close'], errors='coerce')
        df = df.sort_values('trade_date')
        df['ma200'] = df['close'].rolling(window=200).mean()
        self.market_index_df = df.set_index('trade_date')
        
    def _load_stock_data(self, code, start_date):
        kline_path = os.path.join(self.cache_dir, 'kline', f'{code}.csv')
        finance_path = os.path.join(self.cache_dir, 'finance', f'{code}.csv')
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        try:
            df_k = None
            if os.path.exists(kline_path):
                df_k = pd.read_csv(kline_path)
                if not df_k.empty:
                    last_date = pd.to_datetime(df_k['trade_time'].max()).strftime('%Y-%m-%d')
                    if (not self.local_only) and last_date < today_str:
                        new_data = self.baidu_market.get_market(stock_code=code, start_date=last_date)
                        if new_data is not None and not new_data.empty:
                            df_k = pd.concat([df_k, new_data]).drop_duplicates('trade_time').sort_values('trade_time')
                            df_k.to_csv(kline_path, index=False)
            if (df_k is None or df_k.empty) and (not self.local_only):
                s_dt = (datetime.datetime.strptime(start_date, '%Y-%m-%d') - datetime.timedelta(days=365)).strftime('%Y-%m-%d')
                df_k = self.baidu_market.get_market(stock_code=code, start_date=s_dt)
                if df_k is not None and not df_k.empty:
                    df_k.to_csv(kline_path, index=False)
            if df_k is None or df_k.empty: return None
            
            df_f = None
            need_fetch_finance = True
            if os.path.exists(finance_path):
                file_time = datetime.datetime.fromtimestamp(os.path.getmtime(finance_path))
                if (datetime.datetime.now() - file_time).days < 7:
                    df_f = pd.read_csv(finance_path)
                    need_fetch_finance = False
            if need_fetch_finance and (not self.local_only):
                df_f = self.core_finance.get_core_index(stock_code=code)
                if df_f is not None and not df_f.empty:
                    df_f.to_csv(finance_path, index=False)
            if df_f is None and os.path.exists(finance_path):
                df_f = pd.read_csv(finance_path)
            if df_f is None or df_f.empty: return None

            cols = ['open', 'close', 'high', 'low', 'volume', 'pre_close']
            for col in cols: df_k[col] = pd.to_numeric(df_k[col], errors='coerce')
            df_k['trade_date_str'] = pd.to_datetime(df_k['trade_time']).dt.strftime('%Y-%m-%d')
            df_k = df_k.sort_values('trade_time').drop_duplicates('trade_date_str')
            df_k['ma200'] = df_k['close'].rolling(window=200).mean()
            df_k['ma60'] = df_k['close'].rolling(window=60).mean()
            df_k['ma200_slope20'] = df_k['ma200'] - df_k['ma200'].shift(20)
            df_k['ret20'] = df_k['close'] / df_k['close'].shift(20) - 1
            df_k = df_k.set_index('trade_date_str')
            df_k = self._ensure_tech_columns(df_k)
            
            df_f['notice_date'] = pd.to_datetime(df_f['notice_date']).dt.strftime('%Y-%m-%d')
            df_f = df_f.sort_values('notice_date')
            num_cols = ['basic_eps', 'net_asset_ps', 'roe_wtd', 'non_gaap_net_profit_yoy_gr', 
                        'oper_cf_ps', 'asset_liab_ratio', 'net_profit_yoy_gr']
            for col in num_cols: df_f[col] = pd.to_numeric(df_f[col], errors='coerce')
            return code, df_k, df_f
        except Exception as e:
            return None

    def get_all_codes(self):
        if os.path.exists(self.code_cache_file):
            try:
                df_codes = pd.read_csv(self.code_cache_file, dtype={'stock_code': str})
                if not df_codes.empty and 'stock_code' in df_codes.columns:
                    print("使用本地缓存的股票列表")
                    return df_codes['stock_code'].astype(str).tolist()
            except Exception:
                pass

        if self.local_only:
            print("未找到本地股票列表缓存，使用内置股票列表")
            return ['600519', '000858', '601318', '600036', '000001']

        try:
            df_codes = adata.stock.info.all_code()
            if df_codes is not None and not df_codes.empty and 'stock_code' in df_codes.columns:
                df_codes[['stock_code']].to_csv(self.code_cache_file, index=False, encoding='utf-8-sig')
                return df_codes['stock_code'].astype(str).tolist()
        except Exception as e:
            print(f"获取股票列表失败: {e}")

        if os.path.exists(self.code_cache_file):
            try:
                df_codes = pd.read_csv(self.code_cache_file, dtype={'stock_code': str})
                if not df_codes.empty and 'stock_code' in df_codes.columns:
                    print("使用本地缓存的股票列表")
                    return df_codes['stock_code'].astype(str).tolist()
            except Exception:
                pass
        return ['600519', '000858', '601318', '600036', '000001']

    def preload(self, codes, start_date, refresh=False):
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        if os.path.exists(self.full_cache_file):
            cache_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(self.full_cache_file)).strftime('%Y-%m-%d')
            if (not refresh) and (self.local_only or cache_mtime == today_str):
                msg = "加载缓存..."
                if self.local_only and cache_mtime != today_str:
                    msg = f"加载历史缓存（{cache_mtime}）..."
                elif cache_mtime == today_str:
                    msg = "加载今日缓存..."
                print(msg)
                try:
                    with open(self.full_cache_file, 'rb') as f:
                        cache_data = pickle.load(f)
                        # 兼容性检查：剔除 8/9 开头的存量数据
                        self.stock_data_cache = {k: v for k, v in cache_data['stock'].items() if not (k.startswith('8') or k.startswith('9'))}
                        self.finance_data_cache = {k: v for k, v in cache_data['finance'].items() if not (k.startswith('8') or k.startswith('9'))}
                        for code in list(self.stock_data_cache.keys()):
                            self.stock_data_cache[code] = self._ensure_tech_columns(self.stock_data_cache[code])
                    return
                except: pass
        
        # 核心过滤：剔除北交所 (8和9开头)
        valid_codes = [c for c in codes if not (c.startswith('2') or c.startswith('8') or c.startswith('9'))]
        print(f"正在并行处理 {len(valid_codes)} 只股票数据 (已剔除北交所)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(self._load_stock_data, c, start_date): c for c in valid_codes}
            for f in concurrent.futures.as_completed(futures):
                res = f.result()
                if res:
                    code, df_k, df_f = res
                    self.stock_data_cache[code] = self._ensure_tech_columns(df_k)
                    self.finance_data_cache[code] = df_f
        try:
            with open(self.full_cache_file, 'wb') as f:
                pickle.dump({'stock': self.stock_data_cache, 'finance': self.finance_data_cache}, f)
        except: pass

    def get_finance_indicators(self, code, today):
        df_f = self.finance_data_cache.get(code)
        if df_f is None: return None
        available = df_f[df_f['notice_date'] <= today]
        if available.empty: return None
        l = available.iloc[-1]
        return {'eps': l['basic_eps'], 'bps': l['net_asset_ps'], 'roe': l['roe_wtd'], 
                'profit_yoy': l['non_gaap_net_profit_yoy_gr'], 'ocf': l['oper_cf_ps'],
                'debt_ratio': l['asset_liab_ratio'], 'net_profit_yoy': l['net_profit_yoy_gr']}

    def run(self, start_date, end_date, rebalance_period=20):
        if self.market_index_df is None: self.fetch_market_env(start_date)
        all_dates = sorted(self.market_index_df.index)
        dates = [d for d in all_dates if start_date <= d <= end_date]
        self.cash, self.positions, self.completed_trades, self.equity_curve = self.initial_capital, {}, [], []
        self.cooldown_until = {}
        if hasattr(self, 'benchmark_base'): delattr(self, 'benchmark_base')
        
        for idx, today in enumerate(dates):
            to_sell = []
            for code, pos in self.positions.items():
                df_k = self.stock_data_cache.get(code)
                if today not in df_k.index: continue
                today_idx = df_k.index.get_loc(today)
                p = df_k.loc[today, 'close']
                ma200 = df_k.loc[today, 'ma200']
                hold_days = (pd.to_datetime(today) - pd.to_datetime(pos['buy_date'])).days
                if p > pos.get('peak_price', pos['buy_price']):
                    pos['peak_price'] = p

                # 先做固定止盈：减少利润回撤导致的重新转亏
                if (p / pos['buy_price'] - 1) >= self.take_profit_pct:
                    to_sell.append({'code': code, 'reason': f'固定止盈({self.take_profit_pct:.0%})'})
                    continue

                # 盈利达到阈值后触发保本逻辑：回落到成本附近直接退出
                if pos.get('peak_price', pos['buy_price']) >= pos['buy_price'] * (1 + self.breakeven_arm_pct):
                    if p <= pos['buy_price'] * (1 + self.breakeven_buffer):
                        to_sell.append({'code': code, 'reason': '保本止损'})
                        continue

                # 避免单日噪声：至少持有 min_hold_days，且连续两日收盘都在 MA200 下方才触发趋势卖出
                if hold_days >= self.min_hold_days and today_idx > 0 and not pd.isna(ma200):
                    prev_row = df_k.iloc[today_idx - 1]
                    if p < ma200 and prev_row['close'] < prev_row['ma200']:
                        to_sell.append({'code': code, 'reason': '跌破MA200(确认)'})
                else:
                    fin = self.get_finance_indicators(code, today)
                    if fin and fin['eps'] > 0 and (p / fin['eps']) > 60:
                        to_sell.append({'code': code, 'reason': '估值过高(PE>60)'})
            for item in to_sell:
                code = item['code']
                if code in self.positions:
                    pos, cp = self.positions[code], self.stock_data_cache[code].loc[today, 'close']
                    rev = (cp * pos['shares']) * (1 - 0.0013)
                    self.cash += rev
                    self.completed_trades.append({'code': code, 'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                        'sell_date': today, 'sell_price': round(cp, 2), 'shares': pos['shares'], 'amount': round(rev, 2),
                        'profit': round(rev - pos['cost'], 2), 'profit_pct': f"{((rev - pos['cost'])/pos['cost']*100):.2f}%", 'reason': item['reason']})
                    if self.cooldown_days > 0:
                        next_ok_date = (pd.to_datetime(today) + pd.Timedelta(days=self.cooldown_days)).strftime('%Y-%m-%d')
                        self.cooldown_until[code] = next_ok_date
                    del self.positions[code]

            if idx % rebalance_period == 0:
                benchmark_ok = True
                if self.regime_filter:
                    if today not in self.market_index_df.index:
                        benchmark_ok = False
                    else:
                        b_row = self.market_index_df.loc[today]
                        b_close, b_ma200 = b_row['close'], b_row['ma200']
                        benchmark_ok = (not pd.isna(b_ma200)) and (b_close > b_ma200)
                if not benchmark_ok:
                    # 大盘弱势期不新增仓位，优先降低交易次数和亏损占比
                    pass
                else:
                    candidates = []
                    for code, df_k in self.stock_data_cache.items():
                        if today not in df_k.index or code in self.positions: continue
                        if code in self.cooldown_until and today <= self.cooldown_until[code]:
                            continue
                        row = df_k.loc[today]
                        price, ma200, ma60, ma200_slope20 = row['close'], row['ma200'], row['ma60'], row['ma200_slope20']
                        ret20 = row['ret20']
                        if pd.isna(ma200) or pd.isna(ma60) or pd.isna(ma200_slope20) or pd.isna(ret20):
                            continue
                        if not (price > ma60 > ma200 and ma200_slope20 > 0 and ret20 > self.ret20_threshold):
                            continue
                        f = self.get_finance_indicators(code, today)
                        if not f or f['eps'] <= 0 or f['bps'] <= 0: continue
                        pe = price / f['eps']
                        if pe < 25 and price/f['bps'] < 3 and f['roe'] > 8 and f['profit_yoy'] > 0 and f['ocf'] > 0 and f['debt_ratio'] < 70:
                            score = (500.0 / pe) + f['roe'] + ret20 * 100
                            candidates.append({'code': code, 'price': price, 'pe': pe, 'score': score})
                    candidates = sorted(candidates, key=lambda x: x['score'], reverse=True)
                    while len(self.positions) < self.max_positions and candidates:
                        can = candidates.pop(0)
                        available = self.max_positions - len(self.positions)
                        sh = (int((self.cash / available) / can['price']) // 100) * 100
                        if sh >= 100:
                            cost = sh * can['price']
                            self.cash -= cost
                            self.positions[can['code']] = {
                                'buy_date': today, 'buy_price': can['price'], 'shares': sh,
                                'cost': cost, 'peak_price': can['price']
                            }

            mv = 0
            for c, pos in self.positions.items():
                df_k = self.stock_data_cache.get(c)
                if df_k is not None and today in df_k.index:
                    p = df_k.loc[today, 'close']
                    if p <= 0: p = df_k.iloc[max(0, df_k.index.get_loc(today)-1)]['close']
                    mv += p * pos['shares']
                else: mv += pos['buy_price'] * pos['shares']
            
            te = self.cash + mv
            bc = self.market_index_df.loc[today, 'close']
            if not hasattr(self, 'benchmark_base'): self.benchmark_base = bc
            self.equity_curve.append({'date': today, 'total': te, 'benchmark': (bc / self.benchmark_base) * self.initial_capital})
        return te

    def evaluate_current_run(self, final_value):
        closed = pd.DataFrame(self.completed_trades)
        if closed.empty:
            return {
                'total_return': final_value / self.initial_capital - 1,
                'closed_trades': 0,
                'win_rate': 0.0,
                'loss_ratio': 1.0
            }
        closed['profit'] = pd.to_numeric(closed['profit'], errors='coerce')
        win_rate = (closed['profit'] > 0).mean()
        return {
            'total_return': final_value / self.initial_capital - 1,
            'closed_trades': len(closed),
            'win_rate': float(win_rate),
            'loss_ratio': float(1 - win_rate)
        }

    def optimize_for_low_loss(self, start_date, end_date, out_dir):
        grid = {
            'rebalance_period': [10, 20],
            'min_hold_days': [10, 15],
            'cooldown_days': [10, 20],
            'ret20_threshold': [0.02, 0.03],
            'take_profit_pct': [0.10, 0.12],
            'breakeven_arm_pct': [0.06]
        }
        keys = list(grid.keys())
        combos = list(itertools.product(*[grid[k] for k in keys]))
        rows = []

        old_params = {
            'min_hold_days': self.min_hold_days,
            'cooldown_days': self.cooldown_days,
            'ret20_threshold': self.ret20_threshold,
            'take_profit_pct': self.take_profit_pct,
            'breakeven_arm_pct': self.breakeven_arm_pct
        }

        for combo in combos:
            params = dict(zip(keys, combo))
            self.min_hold_days = params['min_hold_days']
            self.cooldown_days = params['cooldown_days']
            self.ret20_threshold = params['ret20_threshold']
            self.take_profit_pct = params['take_profit_pct']
            self.breakeven_arm_pct = params['breakeven_arm_pct']

            final_val = self.run(start_date, end_date, rebalance_period=params['rebalance_period'])
            eva = self.evaluate_current_run(final_val)
            rows.append({
                **params,
                'total_return': eva['total_return'],
                'closed_trades': eva['closed_trades'],
                'win_rate': eva['win_rate'],
                'loss_ratio': eva['loss_ratio']
            })

        self.min_hold_days = old_params['min_hold_days']
        self.cooldown_days = old_params['cooldown_days']
        self.ret20_threshold = old_params['ret20_threshold']
        self.take_profit_pct = old_params['take_profit_pct']
        self.breakeven_arm_pct = old_params['breakeven_arm_pct']

        df_opt = pd.DataFrame(rows)
        # 目标：先最小化亏损占比，再最大化总收益；并要求至少有一定平仓样本
        valid = df_opt[df_opt['closed_trades'] >= 60].copy()
        if valid.empty:
            valid = df_opt.copy()
        best = valid.sort_values(['loss_ratio', 'total_return'], ascending=[True, False]).iloc[0].to_dict()

        df_opt = df_opt.sort_values(['loss_ratio', 'total_return'], ascending=[True, False])
        df_opt.to_csv(os.path.join(out_dir, 'v2_param_optimization.csv'), index=False, encoding='utf-8-sig')
        return best

    def save_results(self, out_dir):
        # 清理历史版本遗留的净值导出文件，避免误以为本次回测仍在生成它
        legacy_equity_file = os.path.join(out_dir, 'value_v2_equity_curve.csv')
        if os.path.exists(legacy_equity_file):
            try:
                os.remove(legacy_equity_file)
            except Exception:
                pass

        all_logs = list(self.completed_trades)
        for code, pos in self.positions.items():
            df_k = self.stock_data_cache.get(code)
            lp = df_k.iloc[-1]['close'] if df_k is not None else pos['buy_price']
            profit = (lp * pos['shares']) - pos['cost']
            all_logs.append({'code': code, 'buy_date': pos['buy_date'], 'buy_price': pos['buy_price'],
                'sell_date': '持仓中', 'sell_price': round(lp, 2), 'shares': pos['shares'], 'amount': '-', 
                'profit': round(profit, 2), 'profit_pct': f"{(profit/pos['cost']*100):.2f}%", 'reason': '未卖出'})
        df_trade = pd.DataFrame(all_logs)
        if not df_trade.empty:
            df_trade['sort_date'] = df_trade['sell_date'].replace('持仓中', '9999-12-31')
            df_trade = df_trade.sort_values(['sort_date', 'buy_date'], ascending=False).drop(columns=['sort_date'])
            df_trade.to_csv(os.path.join(out_dir, 'value_v2_trade_log.csv'), index=False, encoding='utf-8-sig')

        df_equity = pd.DataFrame(self.equity_curve)
        if not df_equity.empty:
            df_equity['date'] = pd.to_datetime(df_equity['date'])
            df_equity.set_index('date', inplace=True)
            total_days = (df_equity.index[-1] - df_equity.index[0]).days
            ann_ret = (df_equity['total'].iloc[-1] / self.initial_capital) ** (365.0 / max(total_days, 1)) - 1
            df_equity['roll_max'] = df_equity['total'].cummax()
            df_equity['drawdown'] = (df_equity['total'] - df_equity['roll_max']) / df_equity['roll_max']
            closed = [t for t in self.completed_trades]
            win_rate = len([t for t in closed if t['profit'] > 0]) / len(closed) if closed else 0
            dr = df_equity['total'].pct_change().dropna()
            sharpe = (dr.mean() * 252 - 0.02) / (dr.std() * np.sqrt(252)) if not dr.empty else 0
            metrics = {'项目': ['总收益率', '年化收益率', '最大回撤', '夏普比率', '交易胜率', '总交易笔数'],
                       '数值': [f"{(df_equity['total'].iloc[-1]/self.initial_capital-1)*100:.2f}%", f"{ann_ret*100:.2f}%",
                              f"{df_equity['drawdown'].min()*100:.2f}%", f"{sharpe:.2f}", f"{win_rate*100:.2f}%", len(self.completed_trades)]}
            pd.DataFrame(metrics).to_csv(os.path.join(out_dir, 'strategy_metrics.csv'), index=False, encoding='utf-8-sig')

    def plot_results(self, save_path):
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.gridspec as gridspec
            import numpy as np

            # 设置中文字体
            plt.rcParams['font.sans-serif'] = ['Heiti TC', 'STHeiti', 'PingFang SC', 'Arial Unicode MS', 'SimHei', 'Microsoft YaHei', 'DejaVu Sans']
            plt.rcParams['axes.unicode_minus'] = False

            df = pd.DataFrame(self.equity_curve)
            df['date'] = pd.to_datetime(df['date'])
            df.set_index('date', inplace=True)

            # 创建画布
            fig = plt.figure(figsize=(16, 12), facecolor='white')
            gs = gridspec.GridSpec(3, 2, height_ratios=[1.5, 1, 1.2], hspace=0.3)

            # 1. 策略净值曲线对比 (Top, Full Width)
            ax1 = fig.add_subplot(gs[0, :])
            ax1.plot(df.index, df['total'], label='价值精选策略', color='#E67E22', lw=2.5)
            ax1.plot(df.index, df['benchmark'], label='沪深300指数', color='#7F8C8D', ls='--', alpha=0.8, lw=1.5)
            ax1.set_title('策略净值曲线对比', fontsize=14, fontweight='bold', pad=15)
            ax1.legend(loc='upper left', frameon=True, fontsize=10)
            ax1.grid(True, alpha=0.15, linestyle='-')
            ax1.set_facecolor('#F9F9F9')
            # 移除上右边框
            ax1.spines['top'].set_visible(False)
            ax1.spines['right'].set_visible(False)

            # 2. 年度收益率 (%) (Middle Left)
            ax2 = fig.add_subplot(gs[1, 0])
            annual_res = df['total'].resample('YE').last()
            years = annual_res.index.year
            vals = annual_res.values
            annual_rets = []
            for i in range(len(years)):
                start_val = self.initial_capital if i ==0 else vals[i-1]
                annual_rets.append((vals[i] / start_val - 1) * 100)
            
            bars = ax2.bar(years.astype(str), annual_rets, color='#52BE80', width=0.6, alpha=0.9)
            ax2.set_title('年度收益率 (%)', fontsize=12, fontweight='bold', pad=10)
            for bar in bars:
                height = bar.get_height()
                ax2.text(bar.get_x() + bar.get_width()/2., height + 0.5 if height > 0 else height - 2, 
                         f'{height:.1f}%', ha='center', va='bottom' if height > 0 else 'top', fontsize=9)
            ax2.grid(axis='y', alpha=0.2, linestyle='--')
            ax2.spines['top'].set_visible(False)
            ax2.spines['right'].set_visible(False)

            # 3. 月度收益热力图 (Middle Right)
            ax3 = fig.add_subplot(gs[1, 1])
            m_res = df['total'].resample('ME').last()
            mr = m_res.pct_change()
            if not mr.empty:
                mr.iloc[0] = (m_res.iloc[0] / self.initial_capital) - 1
            
            rdf = pd.DataFrame({'Year': mr.index.year, 'Month': mr.index.month, 'Ret': mr.values})
            pv = rdf.pivot(index='Year', columns='Month', values='Ret').fillna(0)
            for m in range(1, 13):
                if m not in pv.columns: pv[m] = 0.0
            pv = pv[sorted(pv.columns)]
            
            im = ax3.imshow(pv.values, cmap='RdYlGn', aspect='auto', vmin=-max(0.1, abs(pv.values).max()), vmax=max(0.1, abs(pv.values).max()))
            ax3.set_title('月度收益热力图', fontsize=12, fontweight='bold', pad=10)
            ax3.set_xticks(range(12))
            ax3.set_xticklabels([f'{m}月' for m in range(1, 13)], fontsize=8)
            ax3.set_yticks(range(len(pv.index)))
            ax3.set_yticklabels(pv.index, fontsize=8)
            # 在格子中间写数值
            for i in range(len(pv.index)):
                for j in range(len(pv.columns)):
                    v = pv.values[i, j]
                    ax3.text(j, i, f'{v:.1%}', ha='center', va='center', color='white' if abs(v) > 0.04 else 'black', fontsize=7)

            # 4. 个股累计盈亏明细 (Bottom, Full Width)
            ax4 = fig.add_subplot(gs[2, :])
            stock_profits = {}
            for t in self.completed_trades:
                stock_profits[t['code']] = stock_profits.get(t['code'], 0) + t['profit']
            for code, pos in self.positions.items():
                df_k = self.stock_data_cache.get(code)
                lp = df_k.loc[df.index[-1].strftime('%Y-%m-%d'), 'close'] if (df_k is not None and df.index[-1].strftime('%Y-%m-%d') in df_k.index) else pos['buy_price']
                profit = (lp * pos['shares']) - pos['cost']
                stock_profits[code] = stock_profits.get(code, 0) + profit
            
            if stock_profits:
                sorted_profits = sorted(stock_profits.items(), key=lambda x: x[1], reverse=True)
                # 如果股票太多，只取前30和后10？不，看图里好像挺多的。
                s_codes, s_vals = zip(*sorted_profits)
                colors = ['#2ECC71' if v > 0 else '#E74C3C' for v in s_vals]
                ax4.bar(s_codes, s_vals, color=colors, alpha=0.85)
                ax4.set_title('个股累计盈亏明细 (RMB)', fontsize=12, fontweight='bold', pad=10)
                plt.setp(ax4.get_xticklabels(), rotation=45, ha='right', fontsize=8)
                ax4.grid(axis='y', alpha=0.2, linestyle='--')
                ax4.spines['top'].set_visible(False)
                ax4.spines['right'].set_visible(False)

            plt.tight_layout()
            plt.savefig(save_path, dpi=150, bbox_inches='tight')
            plt.close()
        except Exception as e:
            print(f"画图出错: {e}")
            pass

if __name__ == '__main__':
    START = (datetime.datetime.now() - datetime.timedelta(days=1825)).strftime('%Y-%m-%d')
    END = datetime.datetime.now().strftime('%Y-%m-%d')
    # 默认启用“有最新就用、没有就补拉”的自动更新模式
    strategy = ValueStrategyV2(local_only=False)
    codes = strategy.get_all_codes()
    strategy.preload(codes, START)

    out_dir = os.path.dirname(os.path.abspath(__file__))
    print("\n🔍 开始参数寻优（目标：降低亏损交易占比）...")
    best = strategy.optimize_for_low_loss(START, END, out_dir)
    print(f"  ● 最优参数: {best}")

    strategy.min_hold_days = int(best['min_hold_days'])
    strategy.cooldown_days = int(best['cooldown_days'])
    strategy.ret20_threshold = float(best['ret20_threshold'])
    strategy.take_profit_pct = float(best['take_profit_pct'])
    strategy.breakeven_arm_pct = float(best['breakeven_arm_pct'])
    fixed_period = int(best['rebalance_period'])

    print(f"\n🔍 使用最优参数回测，调仓周期 {fixed_period} 天...")
    final_val = strategy.run(START, END, rebalance_period=fixed_period)
    print(f"  ● 最终资产 {final_val:,.2f}")
    print("\n🏆 回测结束，正在生成最终报告...")

    strategy.save_results(out_dir)
    strategy.plot_results(os.path.join(out_dir, 'v2_strategy_report.png'))
    print(f"\n✨ 回测完成！调仓周期: {fixed_period}天。报告已更新至 {out_dir}")
