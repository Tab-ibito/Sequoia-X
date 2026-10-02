"""将硬规则结果合并为模型输入，并补充与规则口径一致的行情事实。"""

import json
import math
import sqlite3
from contextlib import closing
from datetime import date, datetime

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.evaluation.models import CandidateContext, InternalEvidence, StrategyResult

logger = get_logger(__name__)


def _number(value: object) -> float | None:
    """把 Pandas 标量转为 JSON 可用数字，缺失/非有限值保留为空。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if math.isfinite(number) else None


def _ratio(numerator: object, denominator: object, percent: bool = False) -> float | None:
    """计算比值或百分比变化，分母缺失/为零时不能伪造一个有效指标。"""
    top, bottom = _number(numerator), _number(denominator)
    if top is None or bottom is None or bottom <= 0:
        return None
    return _number((top / bottom - 1) * 100 if percent else top / bottom)


class CandidateContextBuilder:
    """按股票合并命中策略，保留策略名、规则说明及行情数据质量信息。"""

    def __init__(self, engine: DataEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    def build(
        self,
        results: list[StrategyResult],
        as_of: datetime,
    ) -> list[CandidateContext]:
        """保持首次出现顺序去重，避免多策略命中造成重复模型请求。"""
        candidates: dict[str, CandidateContext] = {}
        for result in results:
            if result.status != "ok":
                continue
            for symbol in dict.fromkeys(result.symbols):
                candidate = candidates.setdefault(
                    symbol,
                    CandidateContext(symbol=symbol, matched_strategies=[]),
                )
                candidate.matched_strategies.append(result.strategy_name)
                candidate.internal_evidence.append(
                    InternalEvidence(
                        evidence_id=f"internal:{symbol}:strategy:{result.strategy_name}",
                        source=result.strategy_name,
                        description=f"本次命中规则：{result.rule_description}",
                    )
                )

        with closing(sqlite3.connect(self.engine.db_path)) as conn:
            latest = conn.execute("SELECT MAX(date) FROM stock_daily").fetchone()[0]
        market_date = pd.Timestamp(latest).date() if latest else None

        for candidate in candidates.values():
            try:
                self._fill_market(candidate, market_date, as_of)
            except Exception as exc:
                # 行情失败仍保留规则命中，但不得据此产生购入推荐。
                logger.warning(f"[{candidate.symbol}] 评估行情读取失败：{type(exc).__name__}")
                candidate.buy_eligible = False
                candidate.data_issues.append("行情读取失败，仅保留规则命中事实")
        return list(candidates.values())

    def _fill_market(
        self,
        candidate: CandidateContext,
        market_date: date | None,
        as_of: datetime,
    ) -> None:
        """窗口分别标明是否包含当日，成交额字段按元解释。"""
        df = self.engine.get_ohlcv(candidate.symbol)
        if df.empty:
            candidate.data_issues.append("没有本地行情，无法确认价格、流动性及趋势")
            return

        last = df.iloc[-1]
        candidate.market_date = pd.Timestamp(last["date"]).date()
        candidate.metrics = {
            column: _number(last[column])
            for column in ("open", "high", "low", "close", "volume", "turnover")
        }
        for window in (5, 20, 60):
            candidate.metrics[f"ma{window}"] = (
                _number(df["close"].tail(window).mean()) if len(df) >= window else None
            )
        candidate.metrics["volume_ma20_including_today"] = (
            _number(df["volume"].tail(20).mean()) if len(df) >= 20 else None
        )
        candidate.metrics["previous_20_high"] = (
            _number(df["high"].iloc[-21:-1].max()) if len(df) >= 21 else None
        )
        candidate.metrics["volume_ratio_previous_20"] = (
            _ratio(last["volume"], df["volume"].iloc[-21:-1].mean()) if len(df) >= 21 else None
        )
        candidate.metrics["daily_return_pct"] = (
            _ratio(last["close"], df["close"].iloc[-2], percent=True) if len(df) >= 2 else None
        )
        candidate.metrics["return_120_bars_pct"] = (
            _ratio(last["close"], df["close"].iloc[-121], percent=True) if len(df) >= 121 else None
        )
        for window in (10, 40, 120):
            candidate.metrics[f"high_{window}_including_today"] = (
                _number(df["high"].tail(window).max()) if len(df) >= window else None
            )
            candidate.metrics[f"low_{window}_including_today"] = (
                _number(df["low"].tail(window).min()) if len(df) >= window else None
            )

        if candidate.market_date != market_date:
            candidate.data_issues.append("个股最新行情落后于数据库的最新行情日期")
        age = (as_of.date() - candidate.market_date).days
        if age < 0 or age > self.settings.llm_max_data_age_days:
            candidate.data_issues.append("行情日期超出允许的时效范围")
        if any(
            candidate.metrics[col] is None or candidate.metrics[col] <= 0
            for col in ("open", "high", "low", "close", "volume", "turnover")
        ):
            candidate.data_issues.append("最新行情价格或成交量/额缺失或非正值")
        candidate.buy_eligible = not candidate.data_issues
        candidate.internal_evidence.append(
            InternalEvidence(
                evidence_id=f"internal:{candidate.symbol}:market",
                source="stock_daily / baostock / 后复权日K",
                description=(
                    f"行情日期={candidate.market_date}；价格为后复权分析值，不能直接作为下单价；"
                    "turnover为成交额（元），volume为成交量（股）；指标="
                    + json.dumps(candidate.metrics, ensure_ascii=False, allow_nan=False)
                ),
            )
        )
