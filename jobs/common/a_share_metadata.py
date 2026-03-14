import os

import pandas as pd


def normalize_code(value) -> str:
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text


def is_supported_a_share_code(code: str) -> bool:
    if not (isinstance(code, str) and len(code) == 6 and code.isdigit()):
        return False
    return code.startswith(("600", "601", "603", "605", "000", "001", "002", "003", "300", "301", "688"))


def is_excluded_short_name(short_name: str) -> bool:
    if not isinstance(short_name, str):
        return False
    name = short_name.strip().upper().replace(" ", "")
    if not name:
        return False
    excluded_prefixes = ("ST", "*ST", "SST", "S*ST", "PT", "*PT")
    if name.startswith(excluded_prefixes):
        return True
    excluded_keywords = ("退", "摘牌", "指数", "ETF", "LOF", "基金", "转债")
    return any(keyword in short_name for keyword in excluded_keywords)


def load_stock_metadata(project_root: str) -> dict:
    metadata_file = os.path.join(project_root, "tests", "utils", "all_code.csv")
    if not os.path.exists(metadata_file):
        return {}
    meta = pd.read_csv(metadata_file, dtype={"stock_code": str})
    if "stock_code" not in meta.columns:
        return {}
    meta["stock_code"] = meta["stock_code"].map(normalize_code)
    if "short_name" not in meta.columns:
        meta["short_name"] = ""
    meta["short_name"] = meta["short_name"].fillna("").astype(str).str.strip()
    if "list_date" in meta.columns:
        meta["list_date"] = pd.to_datetime(meta["list_date"], errors="coerce")
    else:
        meta["list_date"] = pd.NaT
    meta = meta.drop_duplicates("stock_code").set_index("stock_code")
    return meta[["short_name", "list_date"]].to_dict("index")
