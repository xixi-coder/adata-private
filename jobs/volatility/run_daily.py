# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage
from typing import Any

import numpy as np
import pandas as pd


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from jobs.common.a_share_metadata import load_stock_metadata
from jobs.common.daily_job import (
    now_shanghai as _now_shanghai,
    read_bool_env as _read_bool_env,
    read_float_env as _read_float_env,
    read_int_env as _read_int_env,
    resolve_trade_date as _resolve_trade_date,
    write_json as _write_json,
)
from jobs.common.email_format import set_rich_email_content
from strategies.volatility import QualityGateConfig, VolatilityStrategy, VolatilityStrategyConfig


OUTPUT_DIR = os.path.join(CURRENT_DIR, "outputs")
SHARED_MARKET_CACHE_ARCHIVE = "three_dim_cache_bundle.tar.gz"
RISK_KEYWORDS = ("退市", "立案", "调查", "诉讼", "资金占用", "违规担保", "债务", "处罚", "冻结")


def resolve_trade_date(requested: str | None = None) -> tuple[str, bool, str]:
    return _resolve_trade_date(requested, action="执行波动结构扫描", skip_action="跳过扫描")


def _format_cell(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    if isinstance(value, float):
        if not np.isfinite(value):
            return ""
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _format_table(df: pd.DataFrame, columns: list[str], limit: int = 15) -> list[str]:
    if df.empty:
        return ["无"]
    lines = []
    for idx, (_, row) in enumerate(df.head(limit).iterrows(), start=1):
        lines.append(f"{idx}. " + " | ".join(_format_cell(row.get(col, "")) for col in columns))
    return lines


def _format_signal_lines(df: pd.DataFrame, limit: int) -> list[str]:
    if df.empty:
        return ["无"]
    lines = []
    for idx, (_, row) in enumerate(df.head(limit).iterrows(), start=1):
        code = _format_cell(row.get("股票代码", ""))
        name = _format_cell(row.get("股票名称", ""))
        tag = _format_cell(row.get("板块/主题", ""))
        score = _format_cell(row.get("评分", ""))
        risk = _format_cell(row.get("风险等级", ""))
        close = _format_cell(row.get("收盘价", ""))
        watch = _format_cell(row.get("观察价", ""))
        invalid = _format_cell(row.get("失效价", ""))
        anomaly = _format_cell(row.get("异常分类", ""))
        anomaly_text = f" {anomaly}" if anomaly else ""
        lines.append(f"{idx}. {code} {name} [{tag}]{anomaly_text} 分{score} 风险{risk} 收{close} 观察{watch} 失效{invalid}")
    return lines


def _fallback_board(code: str) -> str:
    text = str(code).zfill(6)
    if text.startswith(("300", "301")):
        return "创业板"
    if text.startswith(("688", "689")):
        return "科创板"
    if text.startswith(("600", "601", "603", "605")):
        return "沪市主板"
    if text.startswith(("000", "001", "002", "003")):
        return "深市主板"
    return "其他"


def _stock_tags_cache_path() -> str:
    return os.path.join(PROJECT_ROOT, "data", "cache", "stock_tags.csv")


def _load_stock_tags() -> dict[str, dict[str, str]]:
    candidates = [
        os.path.join(PROJECT_ROOT, "data", "cache", "stock_tags.csv"),
        os.path.join(PROJECT_ROOT, "data", "cache", "stock_industry.csv"),
    ]
    tag_path = next((path for path in candidates if os.path.exists(path)), "")
    if not tag_path:
        return {}
    try:
        df = pd.read_csv(tag_path, dtype={"stock_code": str})
    except Exception as exc:
        print(f"股票标签文件读取失败，使用代码板块兜底: {exc}")
        return {}
    if "stock_code" not in df.columns:
        return {}
    df["stock_code"] = df["stock_code"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.zfill(6)
    industry_cols = ["industry", "industry_name", "sw_industry", "申万行业", "行业"]
    concept_cols = ["concept", "concept_name", "concepts", "概念", "主题"]
    tag_map: dict[str, dict[str, str]] = {}
    for _, row in df.drop_duplicates("stock_code").iterrows():
        industry = next((str(row[col]).strip() for col in industry_cols if col in df.columns and pd.notna(row[col])), "")
        concept = next((str(row[col]).strip() for col in concept_cols if col in df.columns and pd.notna(row[col])), "")
        tag_map[str(row["stock_code"])] = {"industry": industry, "concept": concept}
    return tag_map


def _fetch_stock_tag_from_adata(adata_module: Any, code: str) -> dict[str, str]:
    industry = ""
    concept = ""
    try:
        industry_df = adata_module.stock.info.get_industry_sw(stock_code=code)
        if industry_df is not None and not industry_df.empty:
            if "industry_type" in industry_df.columns:
                first_level = industry_df[industry_df["industry_type"].astype(str).str.contains("一级", na=False)]
                source = first_level if not first_level.empty else industry_df
            else:
                source = industry_df
            if "industry_name" in source.columns:
                industry = str(source.iloc[0].get("industry_name") or "").strip()
    except Exception as exc:
        print(f"行业标签获取失败，跳过 {code}: {exc}")

    for method_name in ("get_concept_east", "get_concept_ths"):
        if concept:
            break
        try:
            concept_df = getattr(adata_module.stock.info, method_name)(stock_code=code)
            if concept_df is not None and not concept_df.empty and "name" in concept_df.columns:
                names = [str(item).strip() for item in concept_df["name"].dropna().tolist() if str(item).strip()]
                concept = ";".join(dict.fromkeys(names[:6]))
        except Exception as exc:
            print(f"概念标签获取失败，跳过 {code} {method_name}: {exc}")

    return {"industry": industry, "concept": concept}


def _save_stock_tags(tag_map: dict[str, dict[str, str]]) -> None:
    path = _stock_tags_cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = []
    for code, tags in sorted(tag_map.items()):
        rows.append(
            {
                "stock_code": str(code).zfill(6),
                "industry": str(tags.get("industry") or ""),
                "concept": str(tags.get("concept") or ""),
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def _load_or_fetch_stock_tags(codes: list[str], max_fetch: int) -> dict[str, dict[str, str]]:
    tag_map = _load_stock_tags()
    if max_fetch <= 0:
        return tag_map
    missing = []
    for code in codes:
        code = str(code).zfill(6)
        tags = tag_map.get(code, {})
        if not str(tags.get("industry") or "").strip() and not str(tags.get("concept") or "").strip():
            missing.append(code)
    if not missing:
        return tag_map

    try:
        import adata
    except Exception as exc:
        print(f"adata 导入失败，无法自动补充行业/概念标签: {exc}")
        return tag_map

    fetched = 0
    for code in missing[:max_fetch]:
        tags = _fetch_stock_tag_from_adata(adata, code)
        if tags.get("industry") or tags.get("concept"):
            tag_map[code] = tags
            fetched += 1
    if fetched:
        _save_stock_tags(tag_map)
        print(f"已补充并缓存股票行业/概念标签: {fetched} 只")
    return tag_map


def _attach_cluster_tags(candidates: pd.DataFrame, tag_map: dict[str, dict[str, str]]) -> pd.DataFrame:
    out = candidates.copy()
    if out.empty:
        out["板块/主题"] = pd.Series(dtype=str)
        return out

    def resolve_tag(code: str) -> str:
        code = str(code).zfill(6)
        tags = tag_map.get(code, {})
        industry = str(tags.get("industry") or "").strip()
        concept = str(tags.get("concept") or "").strip()
        if industry and industry.lower() != "nan":
            return industry
        if concept and concept.lower() != "nan":
            return concept.split(";")[0].split(",")[0].split("，")[0]
        return _fallback_board(code)

    out["板块/主题"] = out["股票代码"].map(resolve_tag)
    return out


def _cluster_summary(candidates: pd.DataFrame) -> list[dict[str, Any]]:
    if candidates.empty or "板块/主题" not in candidates.columns:
        return []
    source = candidates[candidates["信号类型"].eq("波动扩张")].copy()
    if source.empty:
        source = candidates.copy()
    clusters = []
    for tag, sub in source.groupby("板块/主题", dropna=False):
        tag = str(tag or "未分类")
        if not tag or tag.lower() == "nan":
            tag = "未分类"
        numeric_score = pd.to_numeric(sub["评分"], errors="coerce")
        top_names = [
            f"{row['股票代码']} {row['股票名称']}"
            for _, row in sub.assign(_score=numeric_score).sort_values("_score", ascending=False).head(5).iterrows()
        ]
        clusters.append(
            {
                "tag": tag,
                "count": int(len(sub)),
                "avg_score": round(float(numeric_score.mean()), 2),
                "max_score": round(float(numeric_score.max()), 2),
                "top_names": top_names,
            }
        )
    return sorted(clusters, key=lambda item: (-item["count"], -item["avg_score"], item["tag"]))[:8]


def _load_mine_risks(codes: list[str], max_checks: int) -> dict[str, dict[str, Any]]:
    if max_checks <= 0 or not codes:
        return {}
    try:
        import adata
    except Exception as exc:
        print(f"adata 导入失败，跳过扫雷接口: {exc}")
        return {}
    risks: dict[str, dict[str, Any]] = {}
    for code in codes[:max_checks]:
        try:
            df = adata.sentiment.mine.mine_clearance_tdx(code)
        except Exception as exc:
            print(f"扫雷接口失败，跳过 {code}: {exc}")
            continue
        if df is None or df.empty:
            continue
        score = pd.to_numeric(df.get("score"), errors="coerce").dropna()
        score_value = float(score.iloc[0]) if not score.empty else np.nan
        reasons = []
        for _, row in df.iterrows():
            reason = str(row.get("reason") or row.get("f_type") or "").strip()
            if reason and reason != "暂无风险项":
                reasons.append(reason)
        risks[code] = {"score": score_value, "reason": "；".join(reasons[:5])}
    return risks


def _to_output_df(signals: pd.DataFrame) -> pd.DataFrame:
    if signals.empty:
        return pd.DataFrame(
            columns=[
                "信号日期",
                "股票代码",
                "股票名称",
                "信号类型",
                "评分",
                "风险等级",
                "收盘价",
                "日涨跌%",
                "20日涨跌%",
                "当日振幅%",
                "20/60日振幅比",
                "20日均成交额(亿)",
                "当日/20日成交额比",
                "5/20日成交额比",
                "距20日线%",
                "距60日线%",
                "距120日线%",
                "60日线20日斜率%",
                "60日回撤%",
                "异常分类",
                "观察价",
                "失效价",
                "入选依据",
            ]
        )
    out = pd.DataFrame(
        {
            "信号日期": pd.to_datetime(signals["trade_date"]).dt.strftime("%Y-%m-%d"),
            "股票代码": signals["stock_code"],
            "股票名称": signals["short_name"].replace("", np.nan).fillna(signals["stock_code"]),
            "信号类型": signals["signal_type"],
            "评分": signals["score"].round(2),
            "风险等级": signals["risk_level"],
            "收盘价": signals["close"].round(3),
            "日涨跌%": (signals["ret_1d"] * 100).round(2),
            "20日涨跌%": (signals["ret_20d"] * 100).round(2),
            "当日振幅%": (signals["range_pct"] * 100).round(2),
            "20/60日振幅比": signals["squeeze_ratio"].round(2),
            "20日均成交额(亿)": (signals["amount_ma20"] / 100_000_000).round(2),
            "当日/20日成交额比": signals["amount_ratio1_20"].round(2),
            "5/20日成交额比": signals["amount_ratio5_20"].round(2),
            "距20日线%": (signals["close_to_ma20"] * 100).round(2),
            "距60日线%": (signals["close_to_ma60"] * 100).round(2),
            "距120日线%": (signals["close_to_ma120"] * 100).round(2),
            "60日线20日斜率%": (signals["ma60_slope20"] * 100).round(2),
            "60日回撤%": (signals["drawdown60"] * 100).round(2),
            "异常分类": signals.get("anomaly_category", pd.Series("", index=signals.index)),
            "观察价": signals["watch_price"].round(3),
            "失效价": signals["invalid_price"].round(3),
            "入选依据": signals["reason"],
        }
    )
    return out


def _build_email_body(summary: dict[str, Any], candidates: pd.DataFrame) -> str:
    report = summary.get("quality_report", {})
    clusters = summary.get("cluster_summary", [])
    squeeze_count = int(candidates["信号类型"].eq("波动收敛").sum()) if not candidates.empty else 0
    expansion_count = int(candidates["信号类型"].eq("波动扩张").sum()) if not candidates.empty else 0
    anomaly_count = int(candidates["信号类型"].eq("异常波动").sum()) if not candidates.empty else 0
    lines = [
        "波动结构扫描",
        f"{summary.get('signal_date', '')} | 候选 {summary.get('candidate_count', 0)} 只 | 收敛 {squeeze_count} / 扩张 {expansion_count} / 异常 {anomaly_count}",
        f"股票池: {report.get('accepted_stock_count', 0)}/{report.get('initial_stock_count', 0)} 只通过票质过滤",
        "",
        "一、资金聚焦",
    ]
    if clusters:
        for idx, item in enumerate(clusters[:3], start=1):
            tops = "，".join(item.get("top_names", [])[:2])
            lines.append(
                f"{idx}. {item['tag']} | {item['count']}只 | 均分{item['avg_score']}"
                + (f"，代表: {tops}" if tops else "")
            )
    else:
        lines.append("暂无明显聚焦方向")

    lines.extend(["", "二、波动扩张  重点看持续性"])
    lines.extend(_format_signal_lines(candidates[candidates["信号类型"].eq("波动扩张")], limit=5))
    lines.extend(["", "三、波动收敛  重点看突破观察价"])
    lines.extend(_format_signal_lines(candidates[candidates["信号类型"].eq("波动收敛")], limit=5))
    lines.extend(["", "四、异常波动  优先当风险提醒"])
    lines.extend(_format_signal_lines(candidates[candidates["信号类型"].eq("异常波动")], limit=3))
    lines.extend(
        [
            "",
            "提示: 扩张不追高，收敛等突破，异常先看风险。完整明细见 latest_candidates.csv。",
        ]
    )
    return "\n".join(lines)


def _write_skip_outputs(trade_date: str, note: str) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    summary = {
        "run_time": _now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": trade_date,
        "is_trade_day": False,
        "note": note,
    }
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)
    body = "\n".join(["波动结构扫描", "", f"日期: {trade_date}", f"状态: {note}", "", "非交易日不生成候选。"])
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")


def _send_email_if_configured() -> None:
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    mail_to = os.getenv("MAIL_TO", "").strip()
    if not smtp_user or not smtp_pass or not mail_to:
        print("未配置邮件参数，跳过邮件通知。")
        return

    body_file = os.path.join(OUTPUT_DIR, "latest_email_body.txt")
    with open(body_file, "r", encoding="utf-8") as f:
        body = f.read().strip()

    msg = EmailMessage()
    msg["Subject"] = "波动结构策略扫描"
    msg["From"] = smtp_user
    msg["To"] = mail_to
    set_rich_email_content(msg, body, title="波动结构策略扫描")

    recipients = [item.strip() for item in mail_to.split(",") if item.strip()]
    host = os.getenv("SMTP_HOST", "smtp.163.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg, to_addrs=recipients)


def main() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    requested_trade_date = os.getenv("TRADE_DATE", "").strip() or None
    trade_date, is_trade_day, trade_note = resolve_trade_date(requested_trade_date)
    if not is_trade_day:
        _write_skip_outputs(trade_date, trade_note)
        print(trade_note)
        return

    try:
        from jobs.common.cloud_cache_sync import sync_cache_from_drive

        sync_cache_from_drive(PROJECT_ROOT, SHARED_MARKET_CACHE_ARCHIVE, ["data/cache"])
    except Exception as exc:
        print(f"缓存同步不可用，继续使用本地缓存: {exc}")
    stock_meta = load_stock_metadata(PROJECT_ROOT)

    quality = QualityGateConfig(
        min_history_days=_read_int_env("VOLATILITY_MIN_HISTORY_DAYS", 180),
        min_listing_days=_read_int_env("VOLATILITY_MIN_LISTING_DAYS", 180),
        min_amount_ma20=_read_float_env("VOLATILITY_MIN_AMOUNT_MA20", 100_000_000),
        min_valid_days20=_read_int_env("VOLATILITY_MIN_VALID_DAYS20", 16),
        min_price=_read_float_env("VOLATILITY_MIN_PRICE", 3.0),
        max_drawdown60=_read_float_env("VOLATILITY_MAX_DRAWDOWN60", 0.40),
        min_mine_score=_read_float_env("VOLATILITY_MIN_MINE_SCORE", 75.0),
    )
    config = VolatilityStrategyConfig(
        quality=quality,
        universe_size=_read_int_env("VOLATILITY_UNIVERSE_SIZE", 2500),
        squeeze_limit=_read_int_env("VOLATILITY_SQUEEZE_LIMIT", 80),
        expansion_limit=_read_int_env("VOLATILITY_EXPANSION_LIMIT", 80),
        anomaly_limit=_read_int_env("VOLATILITY_ANOMALY_LIMIT", 40),
        min_expansion_amount_ratio1_20=_read_float_env("VOLATILITY_MIN_EXPANSION_AMOUNT_RATIO1_20", 1.5),
        min_squeeze_ma60_slope20=_read_float_env("VOLATILITY_MIN_SQUEEZE_MA60_SLOPE20", -0.02),
        min_squeeze_close_to_ma120=_read_float_env("VOLATILITY_MIN_SQUEEZE_CLOSE_TO_MA120", -0.02),
    )

    strategy = VolatilityStrategy(config)
    strategy.load_market_cache()
    strategy.compute_features(stock_meta=stock_meta)
    rough_signals = strategy.latest_signals(trade_date)

    enable_mine = _read_bool_env("VOLATILITY_ENABLE_MINE_CLEARANCE", True)
    if enable_mine and not rough_signals.empty:
        candidate_codes = rough_signals.sort_values("score", ascending=False)["stock_code"].drop_duplicates().tolist()
        mine_risks = _load_mine_risks(candidate_codes, _read_int_env("VOLATILITY_MAX_MINE_CHECKS", 120))
        if mine_risks:
            strategy.compute_features(stock_meta=stock_meta, mine_risks=mine_risks)
            final_signals = strategy.latest_signals(trade_date)
        else:
            final_signals = rough_signals
    else:
        final_signals = rough_signals

    candidates = _to_output_df(final_signals)
    tag_codes = candidates.sort_values("评分", ascending=False)["股票代码"].drop_duplicates().astype(str).tolist()
    tag_map = _load_or_fetch_stock_tags(tag_codes, _read_int_env("VOLATILITY_MAX_TAG_CHECKS", 80))
    candidates = _attach_cluster_tags(candidates, tag_map)
    cluster_summary = _cluster_summary(candidates)
    candidates.to_csv(os.path.join(OUTPUT_DIR, "latest_candidates.csv"), index=False, encoding="utf-8-sig")

    signal_date = ""
    if not final_signals.empty:
        signal_date = pd.to_datetime(final_signals["trade_date"].max()).strftime("%Y-%m-%d")
    elif not strategy.feature_df.empty:
        signal_date = pd.to_datetime(strategy.feature_df["trade_date"].max()).strftime("%Y-%m-%d")

    summary = {
        "run_time": _now_shanghai().strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date": trade_date,
        "signal_date": signal_date,
        "is_trade_day": True,
        "note": trade_note,
        "candidate_count": int(len(candidates)),
        "quality_report": strategy.quality_report,
        "signal_funnel": strategy.signal_funnel_summary(trade_date),
        "cluster_summary": cluster_summary,
    }
    _write_json(os.path.join(OUTPUT_DIR, "latest_summary.json"), summary)

    body = _build_email_body(summary, candidates)
    with open(os.path.join(OUTPUT_DIR, "latest_email_body.txt"), "w", encoding="utf-8") as f:
        f.write(body + "\n")
    print(body)

    if _read_bool_env("VOLATILITY_SEND_EMAIL_IN_SCRIPT", False):
        _send_email_if_configured()


if __name__ == "__main__":
    main()
