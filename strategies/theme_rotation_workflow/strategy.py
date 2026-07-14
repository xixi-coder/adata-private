# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_BASKETS: tuple[dict[str, Any], ...] = (
    {
        "basket": "科技成长",
        "role": "进攻主线",
        "keywords": (
            "AI",
            "人工智能",
            "算力",
            "CPO",
            "光模块",
            "半导体",
            "芯片",
            "存储",
            "先进封装",
            "机器人",
            "软件",
            "信创",
            "数据要素",
            "低空经济",
            "新能源车",
        ),
        "policy_score": 78.0,
        "evidence_score": 66.0,
        "max_weight": 0.30,
        "etfs": ("159995 芯片ETF", "588000 科创50ETF", "515880 通信ETF"),
    },
    {
        "basket": "创新药",
        "role": "成长副主线",
        "keywords": (
            "创新药",
            "医药",
            "生物医药",
            "CRO",
            "CXO",
            "医疗器械",
            "疫苗",
            "细胞治疗",
            "减肥药",
            "ADC",
            "License-out",
            "出海",
        ),
        "policy_score": 82.0,
        "evidence_score": 68.0,
        "max_weight": 0.25,
        "etfs": (
            "3174.HK 南方东英恒生生物科技ETF",
            "159567 港股创新药ETF",
            "159570 港股通创新药ETF",
            "159316 恒生港股通创新药ETF",
            "159992 创新药ETF",
            "515120 创新药ETF",
            "512170 医疗ETF",
        ),
    },
    {
        "basket": "高股息",
        "role": "防守底仓",
        "keywords": ("银行", "保险", "煤炭", "电力", "公用事业", "运营商", "高速公路", "红利", "央企"),
        "policy_score": 64.0,
        "evidence_score": 62.0,
        "max_weight": 0.35,
        "etfs": ("510880 红利ETF", "515080 中证红利ETF", "512890 红利低波ETF"),
    },
    {
        "basket": "消费修复",
        "role": "顺周期观察",
        "keywords": ("消费", "白酒", "食品饮料", "旅游", "酒店", "免税", "家电", "零售", "餐饮"),
        "policy_score": 58.0,
        "evidence_score": 54.0,
        "max_weight": 0.20,
        "etfs": ("159928 消费ETF", "512690 酒ETF", "515650 消费50ETF"),
    },
    {
        "basket": "周期资源",
        "role": "通胀/供给弹性",
        "keywords": ("有色", "黄金", "铜", "铝", "稀土", "化工", "钢铁", "石油", "航运"),
        "policy_score": 55.0,
        "evidence_score": 58.0,
        "max_weight": 0.20,
        "etfs": ("512400 有色金属ETF", "518880 黄金ETF", "159930 能源ETF"),
    },
)


@dataclass(frozen=True)
class ThemeRotationConfig:
    """Weights for turning theme radar signals into a portfolio-level workflow."""

    trend_weight: float = 0.30
    fund_weight: float = 0.22
    catalyst_weight: float = 0.18
    evidence_weight: float = 0.20
    crowding_penalty_weight: float = 0.10
    min_weight: float = 0.05
    satellite_weight: float = 0.10
    cash_buffer: float = 0.20
    baskets: tuple[dict[str, Any], ...] = field(default_factory=lambda: DEFAULT_BASKETS)


class ThemeRotationWorkflow:
    """
    Rule-based A-share theme rotation workflow.

    Input is deliberately compatible with ``ThemeMonitorStrategy`` output. The
    workflow scores each strategic basket, labels it as main/satellite/watch/avoid,
    and converts the score into a target weight band.
    """

    def __init__(self, config: ThemeRotationConfig | None = None):
        self.config = config or ThemeRotationConfig()

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if pd.isna(value):
                return default
            return float(str(value).replace("%", "").strip())
        except Exception:
            return default

    @staticmethod
    def _clip_score(value: float) -> float:
        return round(float(np.clip(value, 0.0, 100.0)), 2)

    @staticmethod
    def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
        text_upper = text.upper()
        return any(keyword.upper() in text_upper for keyword in keywords)

    def build_plan(
        self,
        theme_radar: pd.DataFrame,
        market_context: dict[str, Any] | None = None,
        current_positions: dict[str, float] | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        radar = self._normalize_radar(theme_radar)
        context = market_context or {}
        current_positions = current_positions or {}
        rows = []
        for basket in self.config.baskets:
            matched = self._match_basket_rows(radar, tuple(basket["keywords"]))
            row = self._score_basket(basket, matched, context, current_positions.get(str(basket["basket"]), 0.0))
            rows.append(row)

        plan = pd.DataFrame(rows)
        if plan.empty:
            return plan, {"status": "empty", "risk_mode": "未知", "main_line": "", "satellite_line": ""}

        plan = plan.sort_values(["final_score", "trend_score"], ascending=[False, False]).reset_index(drop=True)
        plan["rank"] = range(1, len(plan) + 1)
        plan = self._assign_actions(plan)
        summary = self._build_summary(plan, context)
        return plan, summary

    def _normalize_radar(self, theme_radar: pd.DataFrame) -> pd.DataFrame:
        if theme_radar is None or theme_radar.empty:
            return pd.DataFrame(
                columns=[
                    "theme",
                    "score",
                    "change_pct",
                    "hot_stock_count",
                    "popularity_overlap_count",
                    "hot_value",
                    "status",
                    "note",
                    "representatives",
                ]
            )
        df = theme_radar.copy()
        for col in ["theme", "status", "note", "representatives"]:
            if col not in df.columns:
                df[col] = ""
            df[col] = df[col].fillna("").astype(str)
        for col in ["score", "change_pct", "hot_stock_count", "popularity_overlap_count", "hot_value"]:
            if col not in df.columns:
                df[col] = 0.0
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return df

    def _match_basket_rows(self, radar: pd.DataFrame, keywords: tuple[str, ...]) -> pd.DataFrame:
        if radar.empty:
            return radar
        text = (
            radar["theme"].astype(str)
            + " "
            + radar["note"].astype(str)
            + " "
            + radar["representatives"].astype(str)
        )
        mask = text.map(lambda value: self._contains_keyword(value, keywords))
        return radar[mask].copy()

    def _score_basket(
        self,
        basket: dict[str, Any],
        matched: pd.DataFrame,
        market_context: dict[str, Any],
        current_weight: float,
    ) -> dict[str, Any]:
        if matched.empty:
            trend_score = 35.0
            fund_score = 30.0
            breadth_score = 0.0
            avg_change = 0.0
            top_themes = ""
        else:
            top_score = float(matched["score"].max())
            avg_score = float(matched["score"].head(5).mean())
            trend_score = self._clip_score(top_score * 0.65 + avg_score * 0.35)
            hot_count = float(matched["hot_stock_count"].sum())
            overlap = float(matched["popularity_overlap_count"].sum())
            hot_value = float(matched["hot_value"].replace(0, np.nan).mean()) if "hot_value" in matched else 0.0
            fund_score = self._clip_score(min(hot_count * 10.0, 60.0) + min(overlap * 10.0, 25.0) + hot_value * 0.15)
            breadth_score = self._clip_score(min(len(matched) * 12.0, 100.0))
            avg_change = float(matched["change_pct"].mean())
            top_themes = " / ".join(matched.sort_values("score", ascending=False)["theme"].head(4).tolist())

        catalyst_score = self._context_adjusted_score(float(basket.get("policy_score", 50.0)), basket, market_context)
        evidence_score = self._clip_score(float(basket.get("evidence_score", 50.0)) * 0.65 + breadth_score * 0.25 + max(avg_change, 0.0) * 2.0)
        crowding_score = self._crowding_score(trend_score, fund_score, avg_change, current_weight)
        final_score = self._clip_score(
            trend_score * self.config.trend_weight
            + fund_score * self.config.fund_weight
            + catalyst_score * self.config.catalyst_weight
            + evidence_score * self.config.evidence_weight
            - crowding_score * self.config.crowding_penalty_weight
        )
        return {
            "rank": 0,
            "basket": basket["basket"],
            "role": basket.get("role", ""),
            "final_score": final_score,
            "trend_score": trend_score,
            "fund_score": fund_score,
            "catalyst_score": catalyst_score,
            "evidence_score": evidence_score,
            "crowding_score": crowding_score,
            "matched_theme_count": int(len(matched)),
            "avg_change_pct": round(avg_change, 2),
            "current_weight": round(float(current_weight), 4),
            "max_weight": round(float(basket.get("max_weight", 0.2)), 4),
            "etf_candidates": " / ".join(basket.get("etfs", ())),
            "top_themes": top_themes,
            "action": "",
            "target_weight": 0.0,
            "weight_band": "",
            "suggested_etfs": "",
            "note": "",
        }

    def _context_adjusted_score(
        self, base_score: float, basket: dict[str, Any], market_context: dict[str, Any]
    ) -> float:
        name = str(basket.get("basket", ""))
        score = base_score
        risk_appetite = str(market_context.get("risk_appetite", ""))
        if risk_appetite == "强":
            score += 6.0 if name in {"科技成长", "创新药"} else -2.0
        elif risk_appetite == "弱":
            score -= 8.0 if name in {"科技成长", "创新药", "消费修复", "周期资源"} else 5.0
        if name == "科技成长":
            for key in ("external_ai_tailwind", "external_semi_tailwind"):
                value = str(market_context.get(key, ""))
                if value == "强":
                    score += 4.0
                elif value == "弱":
                    score -= 4.0
        if name == "创新药" and str(market_context.get("hk_china_tailwind", "")) == "强":
            score += 3.0
        return self._clip_score(score)

    def _crowding_score(self, trend_score: float, fund_score: float, avg_change: float, current_weight: float) -> float:
        crowding = 0.0
        if trend_score >= 80:
            crowding += (trend_score - 75.0) * 1.2
        if fund_score >= 80:
            crowding += (fund_score - 75.0) * 0.9
        if avg_change >= 4:
            crowding += min((avg_change - 3.0) * 8.0, 25.0)
        if current_weight >= 0.25:
            crowding += 12.0
        return self._clip_score(crowding)

    def _assign_actions(self, plan: pd.DataFrame) -> pd.DataFrame:
        out = plan.copy()
        for idx, row in out.iterrows():
            score = float(row["final_score"])
            crowding = float(row["crowding_score"])
            max_weight = float(row["max_weight"])
            if idx == 0 and score >= 68:
                action = "主线"
                target = max_weight * (0.75 if crowding >= 55 else 1.0)
            elif score >= 58:
                action = "副主线"
                target = min(max_weight, self.config.satellite_weight + (score - 58.0) / 100.0)
            elif score >= 48:
                action = "观察"
                target = self.config.min_weight
            else:
                action = "回避"
                target = 0.0
            lower = max(0.0, target - 0.04)
            upper = min(max_weight, target + 0.04) if target > 0 else 0.0
            out.at[idx, "action"] = action
            out.at[idx, "target_weight"] = round(float(target), 4)
            out.at[idx, "weight_band"] = f"{lower:.0%}-{upper:.0%}" if target > 0 else "0%"
            out.at[idx, "suggested_etfs"] = str(row["etf_candidates"]) if target > 0 else "暂不建议"
            out.at[idx, "note"] = self._action_note(action, crowding, str(row["top_themes"]))
        return out

    @staticmethod
    def _action_note(action: str, crowding: float, top_themes: str) -> str:
        pieces = [action]
        if crowding >= 55:
            pieces.append("拥挤偏高，只低吸不追高")
        elif action in {"主线", "副主线"}:
            pieces.append("等待回撤或业绩/事件确认加仓")
        if top_themes:
            pieces.append(f"匹配主题: {top_themes}")
        return "；".join(pieces)

    def _build_summary(self, plan: pd.DataFrame, market_context: dict[str, Any]) -> dict[str, Any]:
        main = plan[plan["action"].eq("主线")]
        satellites = plan[plan["action"].eq("副主线")]
        total_risk_weight = float(plan.loc[plan["action"].isin(["主线", "副主线"]), "target_weight"].sum())
        cash_weight = max(float(self.config.cash_buffer), 1.0 - float(plan["target_weight"].sum()))
        return {
            "status": "success",
            "risk_mode": market_context.get("risk_appetite", "未知"),
            "main_line": "" if main.empty else str(main.iloc[0]["basket"]),
            "satellite_line": "" if satellites.empty else " / ".join(satellites["basket"].head(2).tolist()),
            "growth_weight": round(total_risk_weight, 4),
            "cash_or_defense_weight": round(cash_weight, 4),
            "top_actions": plan[["basket", "action", "target_weight", "final_score"]].head(5).to_dict("records"),
        }
