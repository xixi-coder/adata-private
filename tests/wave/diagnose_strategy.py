#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
策略诊断工具
分析策略表现，找出问题所在
"""

import pandas as pd
import numpy as np
import os

def diagnose():
    """诊断策略表现"""
    
    # 读取交易记录
    script_dir = os.path.dirname(os.path.abspath(__file__))
    trade_log_path = os.path.join(script_dir, 'trade_log_detail.csv')
    
    try:
        trades = pd.read_csv(trade_log_path)
    except FileNotFoundError:
        print("未找到 trade_log_detail.csv，请先运行策略")
        return
    
    if trades.empty:
        print("没有交易记录")
        return
    
    print("="*80)
    print("策略诊断报告")
    print("="*80)
    
    # 1. 基础统计
    print("\n【基础统计】")
    total_trades = len(trades)
    win_trades = (trades['profit_pct'] > 0).sum()
    loss_trades = (trades['profit_pct'] < 0).sum()
    win_rate = win_trades / total_trades
    
    print(f"总交易次数: {total_trades}")
    print(f"盈利次数: {win_trades} ({win_trades/total_trades:.1%})")
    print(f"亏损次数: {loss_trades} ({loss_trades/total_trades:.1%})")
    print(f"胜率: {win_rate:.2%}")
    
    # 2. 收益分析
    print("\n【收益分析】")
    total_profit = trades['profit'].sum()
    avg_profit = trades['profit'].mean()
    avg_win = trades[trades['profit_pct'] > 0]['profit_pct'].mean()
    avg_loss = trades[trades['profit_pct'] < 0]['profit_pct'].mean()
    
    print(f"总盈亏: {total_profit:,.2f}")
    print(f"平均盈亏: {avg_profit:,.2f}")
    print(f"平均盈利: {avg_win:.2%}")
    print(f"平均亏损: {avg_loss:.2%}")
    print(f"盈亏比: {abs(avg_win/avg_loss):.2f}")
    
    # 3. 退出原因分析
    print("\n【退出原因分析】")
    reason_stats = trades.groupby('reason').agg({
        'profit_pct': ['count', 'mean', lambda x: (x > 0).sum()],
        'profit': 'sum'
    }).round(4)
    reason_stats.columns = ['次数', '平均收益率', '盈利次数', '总盈亏']
    reason_stats['胜率'] = (reason_stats['盈利次数'] / reason_stats['次数'] * 100).round(2)
    print(reason_stats)
    
    # 4. 问题诊断
    print("\n【问题诊断】")
    
    # 4.1 ATR止损分析
    atr_stops = trades[trades['reason'] == 'ATR止损']
    if len(atr_stops) > 0:
        atr_loss_rate = (atr_stops['profit_pct'] < 0).mean()
        atr_avg_loss = atr_stops['profit_pct'].mean()
        print(f"\n⚠️  ATR止损问题:")
        print(f"  - ATR止损次数: {len(atr_stops)} ({len(atr_stops)/total_trades:.1%})")
        print(f"  - ATR止损亏损率: {atr_loss_rate:.1%}")
        print(f"  - ATR止损平均收益: {atr_avg_loss:.2%}")
        if atr_loss_rate > 0.8:
            print(f"  ❌ ATR止损效果差，建议调整参数")
    
    # 4.2 移动止盈分析
    trailing_stops = trades[trades['reason'] == '移动止盈']
    if len(trailing_stops) > 0:
        trailing_win_rate = (trailing_stops['profit_pct'] > 0).mean()
        trailing_avg = trailing_stops['profit_pct'].mean()
        # 计算盈利回吐
        trailing_stops['give_back'] = trailing_stops['max_profit_pct'] - trailing_stops['profit_pct']
        avg_give_back = trailing_stops['give_back'].mean()
        
        print(f"\n📊 移动止盈分析:")
        print(f"  - 移动止盈次数: {len(trailing_stops)} ({len(trailing_stops)/total_trades:.1%})")
        print(f"  - 移动止盈胜率: {trailing_win_rate:.1%}")
        print(f"  - 移动止盈平均收益: {trailing_avg:.2%}")
        print(f"  - 平均盈利回吐: {avg_give_back:.2%}")
        if avg_give_back > 0.05:
            print(f"  ⚠️  盈利回吐较大，建议调整止盈参数")
    
    # 4.3 跌破MA10分析
    ma10_stops = trades[trades['reason'] == '跌破MA10']
    if len(ma10_stops) > 0:
        ma10_win_rate = (ma10_stops['profit_pct'] > 0).mean()
        ma10_avg = ma10_stops['profit_pct'].mean()
        
        print(f"\n📉 跌破MA10分析:")
        print(f"  - 跌破MA10次数: {len(ma10_stops)} ({len(ma10_stops)/total_trades:.1%})")
        print(f"  - 跌破MA10胜率: {ma10_win_rate:.1%}")
        print(f"  - 跌破MA10平均收益: {ma10_avg:.2%}")
    
    # 4.4 持仓时间分析
    print(f"\n⏱️  持仓时间分析:")
    avg_holding = trades['holding_days'].mean()
    print(f"  - 平均持仓天数: {avg_holding:.1f}天")
    
    # 按持仓时间分组
    trades['holding_group'] = pd.cut(trades['holding_days'], 
                                      bins=[0, 3, 7, 14, 999],
                                      labels=['1-3天', '4-7天', '8-14天', '>14天'])
    holding_analysis = trades.groupby('holding_group').agg({
        'profit_pct': ['count', 'mean', lambda x: (x > 0).mean()]
    }).round(4)
    holding_analysis.columns = ['次数', '平均收益', '胜率']
    print(holding_analysis)
    
    # 4.5 止损幅度分析
    print(f"\n🛡️  止损幅度分析:")
    avg_stop_loss = trades['stop_loss_pct'].mean()
    print(f"  - 平均止损幅度: {avg_stop_loss:.2%}")
    
    large_stops = trades[trades['stop_loss_pct'] > 0.08]
    if len(large_stops) > 0:
        print(f"  - 止损>8%的交易: {len(large_stops)} ({len(large_stops)/total_trades:.1%})")
        print(f"  - 这些交易平均收益: {large_stops['profit_pct'].mean():.2%}")
    
    # 5. 改进建议
    print("\n【改进建议】")
    
    suggestions = []
    
    # 建议1：胜率低
    if win_rate < 0.45:
        suggestions.append("❌ 胜率过低(<45%)，建议:")
        suggestions.append("   - 加强选股条件（提高gain_20d阈值）")
        suggestions.append("   - 增加技术指标过滤")
        suggestions.append("   - 考虑市场环境（只在强势市场操作）")
    
    # 建议2：盈亏比差
    if abs(avg_win/avg_loss) < 1.5:
        suggestions.append("❌ 盈亏比过低(<1.5)，建议:")
        suggestions.append("   - 放宽止盈条件（从10%改为15%）")
        suggestions.append("   - 收紧止损条件（从1.5倍ATR改为1.2倍）")
    
    # 建议3：ATR止损效果差
    if len(atr_stops) > 0 and atr_loss_rate > 0.8:
        suggestions.append("❌ ATR止损效果差，建议:")
        suggestions.append("   - 减小ATR倍数（从1.5改为1.2）")
        suggestions.append("   - 或改用固定止损（如5%）")
    
    # 建议4：盈利回吐大
    if len(trailing_stops) > 0 and avg_give_back > 0.05:
        suggestions.append("⚠️  盈利回吐较大，建议:")
        suggestions.append("   - 收紧移动止盈（从10%改为8%）")
        suggestions.append("   - 或使用分批止盈")
    
    # 建议5：持仓时间短
    if avg_holding < 5:
        suggestions.append("⚠️  平均持仓时间过短，建议:")
        suggestions.append("   - 放宽止盈条件")
        suggestions.append("   - 让利润充分奔跑")
    
    if suggestions:
        for s in suggestions:
            print(s)
    else:
        print("✅ 策略表现良好，继续保持")
    
    # 6. 最差交易分析
    print("\n【最差交易TOP5】")
    worst = trades.nsmallest(5, 'profit_pct')[
        ['trade_id', 'code', 'buy_date', 'exit_date', 'holding_days', 
         'profit_pct', 'stop_loss_pct', 'reason']
    ]
    for idx, row in worst.iterrows():
        print(f"#{row['trade_id']} {row['code']}: "
              f"{row['buy_date']} -> {row['exit_date']} ({row['holding_days']}天) | "
              f"亏损:{row['profit_pct']:.2%} 止损:{row['stop_loss_pct']:.2%} | {row['reason']}")
    
    print("\n" + "="*80)


if __name__ == '__main__':
    diagnose()
