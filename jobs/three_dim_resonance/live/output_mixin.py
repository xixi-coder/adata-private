# -*- coding: utf-8 -*-
import os

from jobs.common.cloud_cache_sync import upload_file_to_drive, write_json


class OutputMixin:
    @staticmethod
    def _build_email_body(summary: dict) -> str:
        diagnostics = summary.get("entry_diagnostics") or {}
        market = diagnostics.get("market_gate") or {}
        stage = diagnostics.get("stage_counts") or {}
        buy_items = summary.get("buy_suggestions") or []
        sell_items = summary.get("sell_suggestions") or []
        positions = summary.get("positions") or []

        lines = [
            f"三维共振日报 | {summary['signal_date']}",
            f"操作摘要：买入建议 {len(buy_items)} 只，卖出建议 {len(sell_items)} 只，当前持仓 {len(positions)} 只",
            f"建议执行日：{summary.get('next_trade_date') or '暂无下一交易日'}",
            "",
            "一、市场环境",
            market.get("summary", "市场状态暂无数据"),
        ]
        checks = market.get("checks") or []
        passed = [item["label"] for item in checks if item.get("ok")]
        failed = [item["label"] for item in checks if not item.get("ok")]
        if passed:
            lines.append(f"通过：{'、'.join(passed)}")
        if failed:
            lines.append(f"未通过：{'、'.join(failed)}")

        scanned = diagnostics.get("scanned_count")
        if scanned is not None:
            lines.extend(
                [
                    "",
                    "二、选股概况",
                    (
                        f"扫描 {scanned} 只 | 形态通过 {stage.get('shape_ok', 0)} | "
                        f"指标通过 {stage.get('indicator_ok', 0)} | 资金通过 {stage.get('capital_ok', 0)} | "
                        f"三维共振 {stage.get('three_dim_ok', 0)}"
                    ),
                ]
            )
        if summary.get("entry_skip_reason"):
            lines.append(f"未产生买入建议：{summary['entry_skip_reason']}")

        lines.extend(["", "三、卖出建议"])
        if sell_items:
            for idx, item in enumerate(sell_items, start=1):
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} | {item['reason']} | 收盘 {item['close_price']}"
                )
                if item.get("reason_detail"):
                    lines.append(f"   依据：{item['reason_detail']}")
        else:
            lines.append("无")

        lines.extend(["", "四、买入建议"])
        if buy_items:
            for idx, item in enumerate(buy_items, start=1):
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} | {item['entry_shape']} | 评分 {item['score']}"
                )
                lines.append(
                    f"   价格：收盘 {item['close_price']} | 参考触发 {item.get('trigger_price', '-')} | "
                    f"参考失效 {item.get('invalid_price', '-')}"
                )
                if item.get("reason_detail"):
                    lines.append(f"   依据：{item['reason_detail']}")
        else:
            lines.append("无")

        lines.extend(["", "五、当前持仓"])
        if positions:
            for idx, item in enumerate(positions, start=1):
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} | 买入 {item['buy_price']} | "
                    f"收盘 {item['close_price']} | 持有 {item['holding_days']} 天"
                )
        else:
            lines.append("无")

        if summary.get("note"):
            lines.extend(["", f"说明：{summary['note']}"])
        return "\n".join(lines) + "\n"

    def _write_outputs(self, summary: dict):
        # 统一写三份输出：
        # 1) 当日归档 summary_YYYYMMDD.json
        # 2) latest_summary.json（供下游读取）
        # 3) latest_email_body.txt（邮件正文）
        os.makedirs(self.summary_dir, exist_ok=True)
        date_key = summary["signal_date"].replace("-", "")
        summary_json_path = os.path.join(self.summary_dir, f"three_dim_summary_{date_key}.json")
        latest_summary_json_path = os.path.join(self.summary_dir, "latest_summary.json")
        email_body_path = os.path.join(self.summary_dir, "latest_email_body.txt")
        write_json(summary_json_path, summary)
        write_json(latest_summary_json_path, summary)

        with open(email_body_path, "w", encoding="utf-8") as f:
            f.write(self._build_email_body(summary))
        upload_file_to_drive(summary_json_path, os.path.basename(summary_json_path), mime_type="application/json")
        upload_file_to_drive(email_body_path, "three_dim_latest_email.txt", mime_type="text/plain")
