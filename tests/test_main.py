"""主程序入口属性测试。"""

import sys
from unittest.mock import patch

import pytest
from hypothesis import given, settings as h_settings
from hypothesis import strategies as st

# 预先导入 main 模块，避免在 @given 循环中重复导入
import main as main_module


# Feature: sequoia-x-v2, Property 13: 主程序异常以非零退出码终止
@given(error_msg=st.text(min_size=1, max_size=100))
@h_settings(max_examples=30, deadline=None)
def test_main_exits_nonzero_on_exception(error_msg: str) -> None:
    """属性 13：main() 中任意未捕获异常应导致 sys.exit(1)。"""
    # patch main 模块中直接引用的 get_settings
    with patch.object(main_module, "get_settings", side_effect=RuntimeError(error_msg)):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main([])
        assert exc_info.value.code != 0


@pytest.mark.parametrize("strategy_failure", [False, True])
@pytest.mark.parametrize("llm_enabled", [False, True])
def test_daily_pipeline_uses_model_and_sends_only_unified_report(
    tmp_path, monkeypatch, strategy_failure, llm_enabled,
):
    """真实上下文+评估+留档，隔离策略网络、行情同步及飞书发送。"""
    from unittest.mock import Mock

    from sequoia_x.data.engine import DataEngine
    from sequoia_x.evaluation.service import EvaluationService
    from tests.evaluation_helpers import FakeClient, make_settings
    from tests.test_evaluation_context import seed_bars

    settings = make_settings(db_path=str(tmp_path / "market.db"), report_dir=str(tmp_path / "reports"),
                             llm_max_data_age_days=30, llm_enabled=llm_enabled)
    engine = DataEngine(settings)
    from sequoia_x.evaluation.models import now_shanghai
    seed_bars(engine, end=now_shanghai().date())
    monkeypatch.setattr(engine, "sync_today_bulk", lambda: 0)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "DataEngine", lambda _: engine)
    names = ["MaVolumeStrategy", "TurtleTradeStrategy", "HighTightFlagStrategy",
             "LimitUpShakeoutStrategy", "UptrendLimitDownStrategy", "RpsBreakoutStrategy",
             "PrivatePlacementStrategy"]

    def factory(name):
        def run(self):
            if strategy_failure and name == "RpsBreakoutStrategy":
                raise RuntimeError("simulated strategy failure")
            return ["600000"] if name in ("MaVolumeStrategy", "TurtleTradeStrategy") else []
        strategy_type = type(name, (), {"run": run, "rule_description": "test rule"})
        return lambda **kwargs: strategy_type()

    for name in names:
        monkeypatch.setattr(main_module, name, factory(name))
    client = FakeClient()
    monkeypatch.setattr(main_module, "EvaluationService", lambda s: EvaluationService(s, client))
    notifier = Mock()
    notifier.send_report.return_value = True
    monkeypatch.setattr(main_module, "FeishuNotifier", lambda _: notifier)
    if strategy_failure:
        with pytest.raises(SystemExit):
            main_module.main([])
    else:
        main_module.main([])
    notifier.send.assert_not_called()
    notifier.send_report.assert_called_once()
    report = notifier.send_report.call_args.args[0]
    assert len(client.calls) == int(llm_enabled) and len(report.candidates) == 1
    assert report.candidates[0].matched_strategies == ["MaVolumeStrategy", "TurtleTradeStrategy"]
    expected_status = ("partial" if strategy_failure else "complete") if llm_enabled else "disabled"
    assert report.status == expected_status
    assert len(list((tmp_path / "reports").glob("*.json"))) == 1


def test_backfill_does_not_call_model_or_send_report(monkeypatch):
    from unittest.mock import Mock

    from tests.evaluation_helpers import make_settings

    engine = Mock()
    engine.get_all_symbols.return_value = ["600000"]
    model, notifier = Mock(), Mock()
    monkeypatch.setattr(main_module, "get_settings", make_settings)
    monkeypatch.setattr(main_module, "DataEngine", Mock(return_value=engine))
    monkeypatch.setattr(main_module, "EvaluationService", model)
    monkeypatch.setattr(main_module, "FeishuNotifier", notifier)
    main_module.main(["--backfill"])
    engine.backfill.assert_called_once_with(["600000"])
    model.assert_not_called()
    notifier.assert_not_called()
