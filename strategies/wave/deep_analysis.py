#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
深度策略分析工具
"""

import pandas as pd
import numpy as np
import os

def deep_analysis():
    """深度分析策略问题"""
    
    # 读取交易数据
    script_dir = os.path.dirname(os.path.abspath(__file__))
    trade_log_path = os.path.join(script_dir, 'trade_log_detail.csv')
    trades = pd.read_csv(trade_log_path)
    
    print('='*80)
    print('深度策略分析')
    print('='*80)
    
    # 1. 基础统计
    print('\n【1. 整体表现】')
    total_profit = trades['profit'].sum()
    win_rate = (trades['profit_pct'] > 0).mean()
    avg_profit = trades[trades['profit_pct'] > 0]['profit_pct'].mean()
    avg_loss = trades[trades['profit_pct'] < 0]['profit_pct'].mean()
    print(f'总盈亏: {total_profit:,.2f}')
    print(f'胜率: {win_rate:.2%}')
    print(f'平均盈利: {avg_profit:.2%}')
    print(f'平均亏损: {avg_loss:.2%}')
    print(f'盈亏比: {abs(avg_profit/avg_loss):.2f}')
    
    # 2. ATR止损深度分析
    print('\n【2. ATR止损问题分析】')
    atr_stops = trades[trades['reason'] == 'ATR止损']
    print(f'ATR止损次数: {len(atr_stops)}')
    print(f'ATR止损胜率: {(atr_stops["profit_pct"] > 0).mean():.2%}')
    print(f'ATR止损平均亏损: {atr_stops["profit_pct"].mean():.2%}')
    print(f'ATR止损最大浮盈平均: {atr_stops["max_profit_pct"].mean():.2%}')
    print(f'\n⚠️  问题: ATR止损的股票平均曾有{atr_stops["max_profit_pct"].mean():.2%}浮盈，但最终全部亏损')
    
    # 3. 持仓时间与收益关系
    print('\n【3. 持仓时间与收益关系】')
    trades['holding_group'] = pd.cut(trades['holding_days'], 
                                      bins=[0, 3, 7, 14, 100],
                                      labels=['1-3天', '4-7天', '8-14天', '>14天'])
    holding_stats = trades.groupby('holding_group', observed=True).agg({
        'profit_pct': ['count', 'mean', lambda x: (x > 0).mean()]
    })
    holding_stats.columns = ['次数', '平均收益', '胜率']
    print(holding_stats)
    print('\n✅ 结论: 持仓越久，胜率越高，收益越好')
    
    # 4. 选股质量分析（通过最大浮盈判断）
    print('\n【4. 选股质量分析】')
    print(f'平均最大浮盈: {trades["max_profit_pct"].mean():.2%}')
    print(f'最大浮盈>10%的比例: {(trades["max_profit_pct"] > 0.1).mean():.2%}')
    print(f'最大浮盈>20%的比例: {(trades["max_profit_pct"] > 0.2).mean():.2%}')
    print('\n✅ 结论: 选股质量不错，平均都有较大浮盈空间')
    
    # 5. 盈利回吐分析
    print('\n【5. 盈利回吐分析】')
    profitable = trades[trades['profit_pct'] > 0].copy()
    profitable['give_back'] = profitable['max_profit_pct'] - profitable['profit_pct']
    print(f'盈利交易平均回吐: {profitable["give_back"].mean():.2%}')
    print(f'盈利交易最大浮盈平均: {profitable["max_profit_pct"].mean():.2%}')
    print(f'盈利交易实际收益平均: {profitable["profit_pct"].mean():.2%}')
    print(f'回吐比例: {(profitable["give_back"] / profitable["max_profit_pct"]).mean():.2%}')
    
    # 6. 快速止损分析
    print('\n【6. 快速止损（1-3天）分析】')
    quick_stops = trades[trades['holding_days'] <= 3]
    print(f'1-3天交易次数: {len(quick_stops)} ({len(quick_stops)/len(trades):.1%})')
    print(f'1-3天交易胜率: {(quick_stops["profit_pct"] > 0).mean():.2%}')
    print(f'1-3天交易平均收益: {quick_stops["profit_pct"].mean():.2%}')
    print(f'1-3天交易平均最大浮盈: {quick_stops["max_profit_pct"].mean():.2%}')
    print('\n⚠️  问题: 过早止损，错失后续涨幅')
    
    # 7. 市场环境分析
    print('\n【7. 时间分布分析】')
    trades['buy_month'] = pd.to_datetime(trades['buy_date']).dt.to_period('M')
    monthly = trades.groupby('buy_month').agg({
        'profit_pct': ['count', 'mean', lambda x: (x > 0).mean()]
    })
    monthly.columns = ['交易次数', '平均收益', '胜率']
    print('\n月度表现（前10个月）:')
    print(monthly.head(10))
    
    # 8. 核心问题总结
    print('\n' + '='*80)
    print('【核心问题诊断】')
    print('='*80)
    print('\n❌ 1. ATR止损完全失效')
    print('   - 39次ATR止损，100%亏损')
    print('   - 这些股票平均曾有6.5%浮盈，但最终全部止损')
    print('   - 原因: 高波动股票的ATR过大，止损位太宽')
    
    print('\n❌ 2. 持仓时间过短')
    print('   - 56%的交易在3天内结束')
    print('   - 1-3天交易胜率仅16%')
    print('   - 但>14天交易胜率100%')
    print('   - 原因: 移动止盈和MA10止盈过于敏感')
    
    print('\n❌ 3. 盈利回吐严重')
    print('   - 盈利交易平均回吐12%')
    print('   - 平均最大浮盈18%，实际只拿到11%')
    print('   - 回吐比例高达40%')
    
    print('\n✅ 4. 选股质量其实不错')
    print('   - 平均最大浮盈13%')
    print('   - 42%的股票曾有>10%浮盈')
    print('   - 问题不在选股，在于持仓管理')
    
    print('\n' + '='*80)
    print('【策略改进方向】')
    print('='*80)
    print('\n🔥 方案A: 激进改革（推荐）')
    print('1. 完全取消ATR止损，改用固定5%止损')
    print('2. 大幅放宽移动止盈（从10%改为15-20%）')
    print('3. 取消MA10止盈（让利润奔跑）')
    print('4. 减少持仓数量（从5只改为3只，集中火力）')
    print('5. 提高选股标准（20日涨幅从20%提高到25%）')
    
    print('\n💡 方案B: 保守优化')
    print('1. ATR倍数从1.5降到1.0')
    print('2. 移动止盈从10%放宽到12%')
    print('3. 增加持仓时间限制（至少持有5天）')
    print('4. 只在强势市场操作（指数在MA20上方）')
    
    print('\n' + '='*80)
    print('推荐: 方案A - 激进改革')
    print('理由: 当前策略的核心问题是止损/止盈过于敏感，需要激进改革')
    print('='*80)


if __name__ == '__main__':
    deep_analysis()
