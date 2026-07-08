# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd


class ThemeMonitorStrategy:
    """A lightweight market-theme radar built from hot lists and plate rankings."""

    def __init__(self, top_limit: int = 20, representative_limit: int = 5):
        self.top_limit = max(1, int(top_limit))
        self.representative_limit = max(1, int(representative_limit))

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            if pd.isna(value):
                return default
            text = str(value).replace("%", "").strip()
            return float(text)
        except Exception:
            return default

    @staticmethod
    def _to_int(value: Any, default: int = 999) -> int:
        try:
            if pd.isna(value):
                return default
            return int(float(str(value).strip()))
        except Exception:
            return default

    @staticmethod
    def _split_tags(value: Any) -> list[str]:
        if value is None:
            return []
        try:
            if pd.isna(value):
                return []
        except Exception:
            pass
        tags = []
        for item in re.split(r"[;,，、/|]+", str(value)):
            item = item.strip()
            if item and item.lower() != "nan":
                tags.append(item)
        return list(dict.fromkeys(tags))

    @staticmethod
    def _previous_theme_map(previous_snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if not previous_snapshot:
            return {}
        themes = previous_snapshot.get("themes", {})
        if isinstance(themes, dict):
            return {str(k): v for k, v in themes.items() if isinstance(v, dict)}
        if isinstance(themes, list):
            return {str(item.get("theme")): item for item in themes if isinstance(item, dict) and item.get("theme")}
        return {}

    @staticmethod
    def _rank_score(rank: int, max_rank: int = 100) -> float:
        if rank <= 0 or rank >= 999:
            return 0.0
        return max(0.0, (max_rank + 1 - rank) / max_rank * 100.0)

    def build_theme_radar(
        self,
        hot_stocks: pd.DataFrame,
        hot_concepts: pd.DataFrame,
        hot_industries: pd.DataFrame,
        popularity_stocks: pd.DataFrame,
        previous_snapshot: dict[str, Any] | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        concept_scores: dict[str, dict[str, Any]] = {}

        self._add_plate_scores(concept_scores, hot_concepts, "概念")
        self._add_plate_scores(concept_scores, hot_industries, "行业")
        self._add_hot_stock_scores(concept_scores, hot_stocks, popularity_stocks)

        rows = []
        previous_map = self._previous_theme_map(previous_snapshot)
        for theme, metrics in concept_scores.items():
            concept_score = float(metrics.get("plate_score", 0.0))
            stock_score = min(float(metrics.get("hot_stock_count", 0)) * 12.0, 100.0)
            change_score = np.clip(float(metrics.get("avg_change_pct", 0.0)) * 8.0 + 50.0, 0.0, 100.0)
            popularity_score = min(float(metrics.get("popularity_overlap_count", 0)) * 18.0, 100.0)
            flow_score = 0.0
            final_score = (
                concept_score * 0.40
                + stock_score * 0.30
                + change_score * 0.15
                + popularity_score * 0.10
                + flow_score * 0.05
            )
            prev = previous_map.get(theme, {})
            prev_rank = self._to_int(prev.get("rank"), default=999)
            prev_score = self._to_float(prev.get("score"), default=0.0)
            status = self._status_for(metrics.get("rank", 999), final_score, prev_rank, prev_score)
            reps = metrics.get("representatives", [])
            rows.append(
                {
                    "theme": theme,
                    "rank": 0,
                    "score": round(float(final_score), 2),
                    "plate_rank": metrics.get("rank", 999),
                    "plate_type": metrics.get("plate_type", ""),
                    "change_pct": round(float(metrics.get("change_pct", 0.0)), 2),
                    "hot_value": round(float(metrics.get("hot_value", 0.0)), 2),
                    "hot_stock_count": int(metrics.get("hot_stock_count", 0)),
                    "popularity_overlap_count": int(metrics.get("popularity_overlap_count", 0)),
                    "representatives": " / ".join(reps[: self.representative_limit]),
                    "status": status,
                    "previous_rank": "" if prev_rank >= 999 else prev_rank,
                    "previous_score": "" if not prev else round(prev_score, 2),
                    "note": self._note_for(metrics, status),
                }
            )

        radar = pd.DataFrame(rows)
        if radar.empty:
            radar = pd.DataFrame(
                columns=[
                    "theme",
                    "rank",
                    "score",
                    "plate_rank",
                    "plate_type",
                    "change_pct",
                    "hot_value",
                    "hot_stock_count",
                    "popularity_overlap_count",
                    "representatives",
                    "status",
                    "previous_rank",
                    "previous_score",
                    "note",
                ]
            )
            snapshot = {"themes": {}, "theme_count": 0}
            return radar, snapshot

        radar = radar.sort_values(["score", "hot_stock_count", "change_pct"], ascending=[False, False, False])
        radar = radar.head(self.top_limit).reset_index(drop=True)
        radar["rank"] = range(1, len(radar) + 1)
        snapshot = {
            "theme_count": int(len(radar)),
            "themes": {
                row["theme"]: {
                    "rank": int(row["rank"]),
                    "score": float(row["score"]),
                    "status": row["status"],
                    "hot_stock_count": int(row["hot_stock_count"]),
                }
                for _, row in radar.iterrows()
            },
        }
        return radar, snapshot

    def _add_plate_scores(self, target: dict[str, dict[str, Any]], plate_df: pd.DataFrame, plate_type: str) -> None:
        if plate_df is None or plate_df.empty:
            return
        for _, row in plate_df.iterrows():
            theme = str(row.get("concept_name") or row.get("name") or "").strip()
            if not theme:
                continue
            rank = self._to_int(row.get("rank"), default=999)
            entry = target.setdefault(theme, {"representatives": []})
            plate_score = self._rank_score(rank, max_rank=30)
            if plate_score >= float(entry.get("plate_score", 0.0)):
                entry["plate_score"] = plate_score
                entry["rank"] = rank
                entry["plate_type"] = plate_type
                entry["change_pct"] = self._to_float(row.get("change_pct"))
                entry["hot_value"] = self._to_float(row.get("hot_value"))

    def _add_hot_stock_scores(
        self,
        target: dict[str, dict[str, Any]],
        hot_stocks: pd.DataFrame,
        popularity_stocks: pd.DataFrame,
    ) -> None:
        if hot_stocks is None or hot_stocks.empty:
            return
        pop_codes = set()
        if popularity_stocks is not None and not popularity_stocks.empty and "stock_code" in popularity_stocks.columns:
            pop_codes = set(popularity_stocks["stock_code"].dropna().astype(str).str.zfill(6).tolist())
        for _, row in hot_stocks.iterrows():
            stock_code = str(row.get("stock_code") or "").strip().zfill(6)
            short_name = str(row.get("short_name") or stock_code).strip()
            change_pct = self._to_float(row.get("change_pct"))
            tags = self._split_tags(row.get("concept_tag"))
            if not tags:
                pop_tag = str(row.get("pop_tag") or "").strip()
                tags = [pop_tag] if pop_tag else []
            for theme in tags:
                entry = target.setdefault(theme, {"representatives": []})
                entry["hot_stock_count"] = int(entry.get("hot_stock_count", 0)) + 1
                entry["change_sum"] = float(entry.get("change_sum", 0.0)) + change_pct
                count = int(entry["hot_stock_count"])
                entry["avg_change_pct"] = float(entry["change_sum"]) / max(count, 1)
                if stock_code in pop_codes:
                    entry["popularity_overlap_count"] = int(entry.get("popularity_overlap_count", 0)) + 1
                reps = entry.setdefault("representatives", [])
                label = f"{stock_code} {short_name}"
                if label not in reps:
                    reps.append(label)

    @staticmethod
    def _status_for(rank: int, score: float, prev_rank: int, prev_score: float) -> str:
        if prev_rank >= 999:
            return "新晋升温" if rank <= 10 or score >= 70 else "新晋观察"
        rank_delta = prev_rank - rank
        score_delta = score - prev_score
        if rank_delta >= 5 or score_delta >= 12:
            return "快速升温"
        if rank <= 10 and score_delta >= -5:
            return "持续发酵"
        if rank_delta <= -5 or score_delta <= -12:
            return "降温"
        return "震荡观察"

    @staticmethod
    def _note_for(metrics: dict[str, Any], status: str) -> str:
        count = int(metrics.get("hot_stock_count", 0))
        overlap = int(metrics.get("popularity_overlap_count", 0))
        pieces = [status]
        if count:
            pieces.append(f"热股{count}只")
        if overlap:
            pieces.append(f"人气共振{overlap}只")
        if metrics.get("plate_type"):
            pieces.append(str(metrics["plate_type"]))
        return "，".join(pieces)
