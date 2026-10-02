"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：增量行情 + 硬规则 + 大模型评估 + 综合飞书报告
  python main.py --backfill    # 回填模式：baostock 拉全市场历史K线（首次/补数据用，约12分钟）
"""

import argparse
import socket
import sys

from dotenv import load_dotenv

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.evaluation.context import CandidateContextBuilder
from sequoia_x.evaluation.models import StrategyResult, now_shanghai
from sequoia_x.evaluation.report import save_report
from sequoia_x.evaluation.service import EvaluationService
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy


def main(argv: list[str] | None = None) -> None:
    """argv 用于离线测试注入参数；命令行运行时默认读取 sys.argv。"""
    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：通过 baostock 拉取全市场历史 K 线（约12分钟）",
    )
    args = parser.parse_args(argv)

    try:
        # 环境变量在运行入口加载，避免导入模块进行离线测试时产生配置副作用。
        load_dotenv()
        socket.setdefaulttimeout(10.0)
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 日常模式：补齐行情 → 硬规则 → 模型评估 → 两部分综合报告 ──
        logger.info("开始同步增量行情...")
        count = engine.sync_today_bulk()
        logger.info(f"行情同步完成，写入 {count} 条行情记录")

        # 4. 策略列表（新增策略在此追加即可）
        strategies: list[BaseStrategy] = [
            MaVolumeStrategy(engine=engine, settings=settings),
            TurtleTradeStrategy(engine=engine, settings=settings),
            HighTightFlagStrategy(engine=engine, settings=settings),
            LimitUpShakeoutStrategy(engine=engine, settings=settings),
            UptrendLimitDownStrategy(engine=engine, settings=settings),
            RpsBreakoutStrategy(engine=engine, settings=settings),
            PrivatePlacementStrategy(engine=engine, settings=settings),
        ]

        # 5. 先收集全部硬规则结果，保留每个策略的名称和条件说明。
        # 单个策略失败时继续其他策略，最终报告明确标出候选覆盖不完整。
        results: list[StrategyResult] = []
        for strategy in strategies:
            strategy_name = type(strategy).__name__
            logger.info(f"执行策略：{strategy_name}")

            try:
                selected: list[str] = strategy.run()
                results.append(
                    StrategyResult(
                        strategy_name=strategy_name,
                        rule_description=strategy.rule_description,
                        symbols=selected,
                    )
                )
                logger.info(f"{strategy_name} 选出 {len(selected)} 只股票")
            except Exception as exc:
                logger.error(f"{strategy_name} 执行失败：{type(exc).__name__}")
                results.append(
                    StrategyResult(
                        strategy_name=strategy_name,
                        rule_description=strategy.rule_description,
                        status="error",
                    )
                )

        # 6. 合并重复股票，冻结本次评估的行情与证据截止时刻，再分批调用模型。
        # 不在这里发送逐策略消息，保证机器人收到的是统一评估后的报告。
        as_of = now_shanghai()
        candidates = CandidateContextBuilder(engine, settings).build(results, as_of)
        report = EvaluationService(settings).evaluate(candidates, results, as_of)

        # 7. 先保存完整事实与判断，再发送两部分报告；即使推送失败也能查看留档。
        json_path, markdown_path = save_report(report, settings.report_dir)
        logger.info(f"评估状态：{report.status}；报告已保存：{json_path} / {markdown_path}")
        if not FeishuNotifier(settings).send_report(report):
            raise RuntimeError("综合报告推送失败，完整报告已保存在本地")
        if report.status in ("failed", "partial") or any(r.status == "error" for r in results):
            raise RuntimeError("本次评估未完整完成，详情见已保存及推送的报告")

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback

            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()
