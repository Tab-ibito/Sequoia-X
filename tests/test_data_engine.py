"""数据引擎属性测试。"""

import sqlite3
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path

import pandas as pd
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    return engine, settings


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with closing(sqlite3.connect(engine.db_path)) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        assert count == 1


def test_incremental_sync_preserves_already_updated_stock(tmp_path, monkeypatch):
    """只拉取落后的股票时，不能删除同日其他股票已有的行情。"""
    from datetime import timedelta
    from unittest.mock import MagicMock

    from tests.evaluation_helpers import make_settings

    engine = DataEngine(make_settings(db_path=str(tmp_path / "market.db")))
    today = date.today()
    yesterday = today - timedelta(days=1)
    rows = [
        {"symbol": "600000", "date": str(today), "open": 10.0, "high": 11.0,
         "low": 9.0, "close": 10.5, "volume": 1000.0, "turnover": 10500.0},
        {"symbol": "600001", "date": str(yesterday), "open": 10.0, "high": 11.0,
         "low": 9.0, "close": 10.5, "volume": 1000.0, "turnover": 10500.0},
    ]
    with closing(sqlite3.connect(engine.db_path)) as conn:
        pd.DataFrame(rows).to_sql("stock_daily", conn, if_exists="append", index=False)
    pool = MagicMock()
    pool.__enter__.return_value.map.return_value = [[
        ["600001", str(today), "10", "11", "9", "10.5", "1000", "10500"],
    ]]
    monkeypatch.setattr("multiprocessing.Pool", lambda _: pool)
    assert engine.sync_today_bulk() == 1
    assert engine.get_ohlcv("600000")["date"].tolist() == [str(today)]
    assert engine.get_ohlcv("600001")["date"].tolist() == [str(yesterday), str(today)]
