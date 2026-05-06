from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "history"
BJ_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
UTC = timezone.utc

PREDICTION_KEYS = [
    "情绪值",
    "上涨概率",
    "下跌概率",
    "指数判断",
    "涨跌幅预期",
    "最强板块",
    "最弱板块",
    "仓位",
    "操作",
    "理由",
]


def now_bj() -> datetime:
    override = os.getenv("RUN_DATE")
    if override:
        day = datetime.strptime(override, "%Y-%m-%d").date()
        return datetime.combine(day, time(9, 0), tzinfo=BJ_TZ)
    return datetime.now(BJ_TZ)


def day_key(dt_or_day: datetime | str) -> str:
    if isinstance(dt_or_day, datetime):
        return dt_or_day.strftime("%Y%m%d")
    return dt_or_day.replace("-", "")


def day_dir(day: str) -> Path:
    path = HISTORY_DIR / day_key(day)
    path.mkdir(parents=True, exist_ok=True)
    return path


def scores_path(day: str) -> Path:
    return day_dir(day) / "scores.csv"


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def clamp_number(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number):
        return default
    return max(low, min(high, number))


def fetch_json(url: str, params: dict[str, Any] | None = None, timeout: int = 20) -> dict[str, Any]:
    response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "market-bot/1.0"})
    response.raise_for_status()
    return response.json()


def fetch_text(url: str, timeout: int = 20) -> str:
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "market-bot/1.0"})
    response.raise_for_status()
    return response.text


def gdelt_datetime(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def fetch_gdelt_articles(query: str, start: datetime, end: datetime, max_records: int = 20) -> list[dict[str, Any]]:
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_records,
        "sort": "HybridRel",
        "startdatetime": gdelt_datetime(start),
        "enddatetime": gdelt_datetime(end),
    }
    try:
        data = fetch_json("https://api.gdeltproject.org/api/v2/doc/doc", params=params)
    except Exception as exc:
        return [{"source": "GDELT", "error": str(exc), "query": query}]

    articles = []
    for item in data.get("articles", []):
        articles.append(
            {
                "source": "GDELT",
                "title": item.get("title", ""),
                "domain": item.get("domain", ""),
                "url": item.get("url", ""),
                "published": item.get("seendate", ""),
                "language": item.get("language", ""),
            }
        )
    return articles


def fetch_yahoo_rss(symbols: str, label: str) -> list[dict[str, Any]]:
    query = urlencode({"s": symbols, "region": "US", "lang": "en-US"})
    url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?{query}"
    try:
        root = ElementTree.fromstring(fetch_text(url))
    except Exception as exc:
        return [{"source": "Yahoo Finance RSS", "error": str(exc), "market": label}]

    items = []
    for item in root.findall(".//item")[:20]:
        items.append(
            {
                "source": "Yahoo Finance RSS",
                "market": label,
                "title": (item.findtext("title") or "").strip(),
                "url": (item.findtext("link") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
            }
        )
    return items


def collect_market_quotes() -> list[dict[str, Any]]:
    try:
        import yfinance as yf
    except Exception as exc:
        return [{"error": f"yfinance unavailable: {exc}"}]

    symbols = {
        "^GSPC": "标普500",
        "^IXIC": "纳斯达克",
        "^DJI": "道琼斯",
        "^N225": "日经225",
        "^KS11": "韩国KOSPI",
        "000001.SS": "上证指数",
        "399001.SZ": "深证成指",
        "399006.SZ": "创业板指",
    }
    quotes: list[dict[str, Any]] = []
    for symbol, name in symbols.items():
        try:
            history = yf.Ticker(symbol).history(period="5d", interval="1d")
            if history.empty:
                continue
            last = history.iloc[-1]
            prev = history.iloc[-2] if len(history) > 1 else None
            close = float(last["Close"])
            pct_change = None
            if prev is not None and float(prev["Close"]) != 0:
                pct_change = (close / float(prev["Close"]) - 1) * 100
            quotes.append({"symbol": symbol, "name": name, "close": round(close, 4), "pct_change": round(pct_change, 4) if pct_change is not None else None})
        except Exception as exc:
            quotes.append({"symbol": symbol, "name": name, "error": str(exc)})
    return quotes


def collect_news_data(run_time: datetime) -> dict[str, Any]:
    us_start = datetime.combine((run_time - timedelta(days=1)).date(), time(20, 0), tzinfo=BJ_TZ)
    asia_start = datetime.combine(run_time.date(), time(6, 0), tzinfo=BJ_TZ)
    end = run_time

    us_query = '(United States stock market OR Wall Street OR Nasdaq OR "S&P 500" OR Dow) (earnings OR Federal Reserve OR inflation OR semiconductor OR AI OR oil OR banks)'
    asia_query = '(Japan stock market OR Nikkei OR South Korea stock market OR KOSPI OR yen OR won) (chips OR exports OR central bank OR inflation OR technology)'

    return {
        "run_time_bj": run_time.isoformat(),
        "windows": {
            "us_news_bj": {"start": us_start.isoformat(), "end": end.isoformat()},
            "japan_korea_news_bj": {"start": asia_start.isoformat(), "end": end.isoformat()},
        },
        "market_quotes": collect_market_quotes(),
        "us_news": fetch_gdelt_articles(us_query, us_start, end) + fetch_yahoo_rss("^DJI,^GSPC,^IXIC", "US"),
        "japan_korea_news": fetch_gdelt_articles(asia_query, asia_start, end) + fetch_yahoo_rss("^N225,^KS11", "Japan/Korea"),
    }


def load_history_summary(limit: int = 20) -> dict[str, Any]:
    summary: dict[str, Any] = {"recent_scores": [], "recent_results": []}
    if HISTORY_DIR.exists():
        for folder in sorted([p for p in HISTORY_DIR.iterdir() if p.is_dir()])[-limit:]:
            score_path = folder / "scores.csv"
            if score_path.exists():
                with score_path.open("r", encoding="utf-8", newline="") as fh:
                    summary["recent_scores"].extend(list(csv.DictReader(fh))[-1:])
            result = read_json(folder / "results.json")
            if result:
                summary["recent_results"].append(result)
    return summary


def prediction_prompt(news_data: dict[str, Any], history_summary: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "你是A股短线交易策略系统的一部分。你的目标不是解释市场，而是输出可以被验证、打分、优化的交易预测。"
        "你必须只输出一个JSON对象，不要Markdown，不要代码块，不要多余文字。"
    )
    user = f"""
# 输入数据
{json.dumps(news_data, ensure_ascii=False)}

# 历史表现
{json.dumps(history_summary, ensure_ascii=False)}

# 分析任务
一、核心事件：提取最重要的5条事件并压缩到理由字段中。
二、市场状态量化：情绪值0-100；波动预期和风险等级也压缩到理由字段中。
三、A股预测：
1. 大盘判断：上涨概率0-100%，下跌概率0-100%。
2. 指数预期必须选：上涨 / 震荡 / 下跌。
3. 涨跌幅预期必须给区间，例如：-1% ~ +0.5%。
四、板块预测：最强板块1-3个；最弱板块1-3个。
五、交易策略：仓位0-100%；操作必须选：低吸 / 观望 / 减仓 / 追涨。

# 输出格式
{{
  "情绪值": 0,
  "上涨概率": 0,
  "下跌概率": 0,
  "指数判断": "",
  "涨跌幅预期": "",
  "最强板块": [],
  "最弱板块": [],
  "仓位": 0,
  "操作": "",
  "理由": ""
}}

# 约束
1. 所有预测必须可验证。
2. 不允许模糊表达。
3. 不允许废话。
4. 如果不确定，降低概率，而不是乱猜。
5. JSON必须包含且只包含输出格式中的10个字段。
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def extract_json_object(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def call_openai_prediction(news_data: dict[str, Any], history_summary: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for prediction generation.")

    model = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
    payload = {
        "model": model,
        "messages": prediction_prompt(news_data, history_summary),
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return extract_json_object(content)


def normalize_prediction(raw: dict[str, Any]) -> dict[str, Any]:
    data = {key: raw.get(key) for key in PREDICTION_KEYS}
    data["情绪值"] = round(clamp_number(data["情绪值"], 0, 100, 50))
    data["上涨概率"] = round(clamp_number(data["上涨概率"], 0, 100, 50))
    data["下跌概率"] = round(clamp_number(data["下跌概率"], 0, 100, 50))

    if data["指数判断"] not in {"上涨", "震荡", "下跌"}:
        data["指数判断"] = "震荡"
    if data["操作"] not in {"低吸", "观望", "减仓", "追涨"}:
        data["操作"] = "观望"
    data["涨跌幅预期"] = str(data["涨跌幅预期"] or "-0.5% ~ +0.5%")
    data["仓位"] = round(clamp_number(data["仓位"], 0, 100, 30))

    for key in ("最强板块", "最弱板块"):
        value = data[key]
        if not isinstance(value, list):
            value = [str(value)] if value else []
        data[key] = [str(item).strip() for item in value if str(item).strip()][:3]
    data["理由"] = str(data["理由"] or "")[:500]
    return data


def run_predict() -> Path:
    run_time = now_bj()
    day = run_time.strftime("%Y-%m-%d")
    news_data = collect_news_data(run_time)
    raw_prediction = call_openai_prediction(news_data, load_history_summary())
    prediction = normalize_prediction(raw_prediction)
    path = day_dir(day) / "predictions.json"
    write_json(path, prediction)
    return path


def direction_from_pct(pct: float | None) -> str:
    if pct is None:
        return "未知"
    if pct > 0.2:
        return "上涨"
    if pct < -0.2:
        return "下跌"
    return "震荡"


def parse_ak_float(value: Any) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(str(value).replace("%", "").replace(",", ""))
    except Exception:
        return None


def fetch_akshare_result(day: str) -> dict[str, Any]:
    import akshare as ak

    index_symbols = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
    }
    indices: dict[str, Any] = {}
    for symbol, name in index_symbols.items():
        df = ak.stock_zh_index_daily_em(symbol=symbol)
        if df.empty:
            continue
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        row = df[df["date"] == day]
        if row.empty:
            row = df.tail(1)
        item = row.iloc[-1]
        close = parse_ak_float(item.get("close"))
        open_price = parse_ak_float(item.get("open"))
        prev_close = parse_ak_float(item.get("pre_close"))
        if prev_close is None:
            before = df[df["date"] < item["date"]].tail(1)
            if not before.empty:
                prev_close = parse_ak_float(before.iloc[-1].get("close"))
        pct = None
        if close is not None and prev_close:
            pct = (close / prev_close - 1) * 100
        indices[name] = {
            "date": str(item["date"]),
            "open": round(open_price, 4) if open_price is not None else None,
            "close": round(close, 4) if close is not None else None,
            "pct_change": round(pct, 4) if pct is not None else None,
            "direction": direction_from_pct(pct),
        }

    boards = ak.stock_board_industry_name_em()
    pct_col = "涨跌幅" if "涨跌幅" in boards.columns else None
    name_col = "板块名称" if "板块名称" in boards.columns else "名称"
    if pct_col:
        boards = boards.copy()
        boards[pct_col] = boards[pct_col].map(parse_ak_float)
        boards = boards.dropna(subset=[pct_col]).sort_values(pct_col, ascending=False)
        strong = boards.head(3)[name_col].astype(str).tolist()
        weak = boards.tail(3).sort_values(pct_col)[name_col].astype(str).tolist()
    else:
        strong, weak = [], []

    return {
        "date": day,
        "source": "akshare/eastmoney",
        "market_status": "trading_data_found" if indices else "no_trading_data",
        "indices": indices,
        "指数判断": indices.get("上证指数", {}).get("direction", "未知"),
        "上证涨跌幅": indices.get("上证指数", {}).get("pct_change"),
        "最强板块": strong,
        "最弱板块": weak,
    }


def fetch_yfinance_result(day: str) -> dict[str, Any]:
    import yfinance as yf

    mapping = {"000001.SS": "上证指数", "399001.SZ": "深证成指", "399006.SZ": "创业板指"}
    indices: dict[str, Any] = {}
    for symbol, name in mapping.items():
        history = yf.Ticker(symbol).history(period="7d", interval="1d")
        if history.empty:
            continue
        last = history.iloc[-1]
        prev = history.iloc[-2] if len(history) > 1 else None
        close = float(last["Close"])
        pct = (close / float(prev["Close"]) - 1) * 100 if prev is not None and float(prev["Close"]) else None
        indices[name] = {"close": round(close, 4), "pct_change": round(pct, 4) if pct is not None else None, "direction": direction_from_pct(pct)}
    return {
        "date": day,
        "source": "yfinance",
        "market_status": "trading_data_found" if indices else "no_trading_data",
        "indices": indices,
        "指数判断": indices.get("上证指数", {}).get("direction", "未知"),
        "上证涨跌幅": indices.get("上证指数", {}).get("pct_change"),
        "最强板块": [],
        "最弱板块": [],
    }


def run_result() -> Path:
    day = now_bj().strftime("%Y-%m-%d")
    try:
        result = fetch_akshare_result(day)
    except Exception as exc:
        result = fetch_yfinance_result(day)
        result["fallback_reason"] = str(exc)

    path = day_dir(day) / "results.json"
    write_json(path, result)
    prediction_path = day_dir(day) / "predictions.json"
    if result.get("market_status") == "trading_data_found" and prediction_path.exists():
        score_day(day)
    return path


def parse_range(text: str) -> tuple[float, float] | None:
    numbers = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    if len(numbers) < 2:
        return None
    low, high = float(numbers[0]), float(numbers[1])
    return (min(low, high), max(low, high))


def overlap_score(predicted: list[Any], actual: list[Any]) -> float:
    if not predicted or not actual:
        return 0.0
    pred = {str(x).strip() for x in predicted}
    act = {str(x).strip() for x in actual}
    return len(pred & act) / min(len(pred), len(act)) * 100


def score_prediction(prediction: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    actual_direction = result.get("指数判断", "未知")
    actual_pct = result.get("上证涨跌幅")
    predicted_direction = prediction.get("指数判断", "震荡")
    up_prob = clamp_number(prediction.get("上涨概率"), 0, 100, 50)

    if actual_direction == "上涨":
        target_prob = 100
    elif actual_direction == "下跌":
        target_prob = 0
    else:
        target_prob = 50
    probability_score = max(0, 100 - abs(up_prob - target_prob))

    if predicted_direction == actual_direction:
        direction_score = 100
    elif "震荡" in {predicted_direction, actual_direction}:
        direction_score = 50
    else:
        direction_score = 0

    expected_range = parse_range(str(prediction.get("涨跌幅预期", "")))
    if expected_range and actual_pct is not None:
        low, high = expected_range
        if low <= actual_pct <= high:
            range_score = 100
        else:
            distance = min(abs(actual_pct - low), abs(actual_pct - high))
            range_score = max(0, 100 - distance * 40)
    else:
        range_score = 0

    strong_score = overlap_score(prediction.get("最强板块", []), result.get("最强板块", []))
    weak_score = overlap_score(prediction.get("最弱板块", []), result.get("最弱板块", []))
    sector_score = (strong_score + weak_score) / 2
    total = direction_score * 0.35 + probability_score * 0.25 + range_score * 0.20 + sector_score * 0.20

    return {
        "actual_direction": actual_direction,
        "actual_pct": actual_pct,
        "predicted_direction": predicted_direction,
        "predicted_up_probability": round(up_prob, 2),
        "direction_score": round(direction_score, 2),
        "probability_score": round(probability_score, 2),
        "range_score": round(range_score, 2),
        "sector_score": round(sector_score, 2),
        "total_score": round(total, 2),
    }


def score_day(day: str) -> dict[str, Any]:
    prediction = read_json(day_dir(day) / "predictions.json")
    result = read_json(day_dir(day) / "results.json")
    if not prediction:
        raise FileNotFoundError(f"Missing prediction.json for {day}")
    if not result:
        raise FileNotFoundError(f"Missing result.json for {day}")

    score = {"date": day, **score_prediction(prediction, result)}
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    path = scores_path(day)
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        rows = [row for row in rows if row.get("date") != day]
    rows.append({k: str(v) for k, v in score.items()})
    rows.sort(key=lambda row: row["date"])

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(score.keys()))
        writer.writeheader()
        writer.writerows(rows)
    return score


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["predict", "result", "score"])
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to Beijing today")
    args = parser.parse_args()

    if args.date:
        os.environ["RUN_DATE"] = args.date

    if args.command == "predict":
        path = run_predict()
        print(f"prediction written: {path}")
    elif args.command == "result":
        path = run_result()
        print(f"result written: {path}")
    else:
        day = args.date or now_bj().strftime("%Y-%m-%d")
        score = score_day(day)
        print(json.dumps(score, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
