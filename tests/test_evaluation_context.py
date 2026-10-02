"""使用真实临时 SQLite 检查合并、指标窗口和行情时效。"""

import sqlite3
from contextlib import closing
from datetime import timedelta

import pandas as pd
import pytest

from sequoia_x.data.engine import DataEngine
from sequoia_x.evaluation.context import CandidateContextBuilder
from sequoia_x.evaluation.models import StrategyResult
from tests.evaluation_helpers import AS_OF, make_settings


def seed_bars(engine, symbol="600000", end=AS_OF.date(), count=121):
    """构造可手算的行情，以确认均线和120根K线收益率的窗口偏移。"""
    rows = [
        {
            "symbol": symbol,
            "date": str(end - timedelta(days=count - i - 1)),
            "open": float(i + 9),
            "high": float(i + 11),
            "low": float(i + 8),
            "close": float(i + 10),
            "volume": float(1000 + i),
            "turnover": 100_000_001.0,
        }
        for i in range(count)
    ]
    with closing(sqlite3.connect(engine.db_path)) as conn:
        pd.DataFrame(rows).to_sql("stock_daily", conn, if_exists="append", index=False)


def test_merge_strategy_signals_and_real_metrics(tmp_path):
    settings = make_settings(db_path=str(tmp_path / "market.db"))
    engine = DataEngine(settings)
    seed_bars(engine)
    results = [
        StrategyResult(
            strategy_name="First", rule_description="rule one", symbols=["600000", "600000"]
        ),
        StrategyResult(strategy_name="Second", rule_description="rule two", symbols=["600000"]),
    ]
    (candidate,) = CandidateContextBuilder(engine, settings).build(results, AS_OF)
    assert candidate.matched_strategies == ["First", "Second"]
    assert len(candidate.internal_evidence) == 3
    assert candidate.buy_eligible
    assert candidate.metrics["previous_20_high"] == 130.0  # 排除今日131的高点
    assert candidate.metrics["ma5"] == 128.0
    assert candidate.metrics["return_120_bars_pct"] == 1200.0
    assert candidate.metrics["daily_return_pct"] == pytest.approx((130 / 129 - 1) * 100, abs=1e-6)


@pytest.mark.parametrize("mode", ["missing", "older_than_market", "expired", "invalid"])
def test_invalid_market_context_is_not_buy_eligible(tmp_path, mode):
    settings = make_settings(db_path=str(tmp_path / "market.db"))
    engine = DataEngine(settings)
    if mode == "older_than_market":
        seed_bars(engine, end=AS_OF.date() - timedelta(days=1))
        seed_bars(engine, symbol="600001")
    elif mode == "expired":
        seed_bars(engine, end=AS_OF.date() - timedelta(days=10))
    elif mode == "invalid":
        seed_bars(engine)
        with closing(sqlite3.connect(engine.db_path)) as conn:
            conn.execute("UPDATE stock_daily SET close=NULL WHERE date=?", (str(AS_OF.date()),))
            conn.commit()
    result = StrategyResult(strategy_name="Rule", rule_description="test", symbols=["600000"])
    (candidate,) = CandidateContextBuilder(engine, settings).build([result], AS_OF)
    assert not candidate.buy_eligible
    assert candidate.data_issues
    assert all(value is None or pd.notna(value) for value in candidate.metrics.values())
