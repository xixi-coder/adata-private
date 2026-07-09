# -*- coding: utf-8 -*-
import os

from jobs.common.cloud_cache_sync import upload_file_to_drive, write_json


class OutputMixin:
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

        lines = [
            "三维共振策略建议",
            f"候选依据日: {summary['candidate_reference_date']}",
            f"建议执行日: {summary['next_trade_date'] or '暂无下一交易日'}",
        ]
        # 若同日重跑，则说明不会重复成交，仅刷新建议展示。
        if summary.get("note"):
            lines.append(f"备注: {summary['note']}")
        if summary.get("entry_skip_reason"):
            lines.append(f"买入为空原因: {summary['entry_skip_reason']}")
        lines.extend(["", "建议卖出:"])
        if summary["sell_suggestions"]:
            for idx, item in enumerate(summary["sell_suggestions"], start=1):
                detail = item.get("reason_detail", "")
                detail_text = f" 明细={detail}" if detail else ""
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} "
                    f"收盘价={item['close_price']} 原因={item['reason']}{detail_text}"
                )
        else:
            lines.append("无")

        lines.extend(["", "建议买入:"])
        if summary["buy_suggestions"]:
            for idx, item in enumerate(summary["buy_suggestions"], start=1):
                reason = item.get("reason", "三维共振通过")
                detail = item.get("reason_detail", "")
                detail_text = f" 明细={detail}" if detail else ""
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} "
                    f"收盘价={item['close_price']} 分数={item['score']} 形态={item['entry_shape']} "
                    f"原因={reason}{detail_text}"
                )
        else:
            lines.append("无")

        lines.extend(["", "当前持仓:"])
        if summary["positions"]:
            for idx, item in enumerate(summary["positions"], start=1):
                lines.append(
                    f"{idx}. {item['code']} {item['short_name']} "
                    f"买入日={item['buy_date']} 买入价={item['buy_price']} "
                    f"收盘价={item['close_price']} 持仓天数={item['holding_days']}"
                )
        else:
            lines.append("无")

        with open(email_body_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        upload_file_to_drive(summary_json_path, os.path.basename(summary_json_path), mime_type="application/json")
        upload_file_to_drive(email_body_path, "three_dim_latest_email.txt", mime_type="text/plain")
