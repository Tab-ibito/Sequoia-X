"""评估编排：分批调用模型，验证股票与引用，汇总完整/部分失败报告。"""

import time
from datetime import datetime

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger
from sequoia_x.evaluation.client import BaseLlmClient, OpenAICompatibleClient
from sequoia_x.evaluation.evidence import (
    BaseEvidenceProvider,
    ConfiguredEvidenceProvider,
    usable_evidence,
)
from sequoia_x.evaluation.models import (
    AssessmentBatch,
    CandidateContext,
    EvaluationReport,
    StockAssessment,
    StrategyResult,
)
from sequoia_x.evaluation.prompt import NO_EXTERNAL_REASON, build_messages

logger = get_logger(__name__)


class EvaluationService:
    """通过依赖注入预留客户端和证据接口，也方便离线测试整个评估过程。"""

    def __init__(
        self,
        settings: Settings,
        client: BaseLlmClient | None = None,
        evidence_provider: BaseEvidenceProvider | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.evidence_provider = evidence_provider or ConfiguredEvidenceProvider(settings)

    def evaluate(
        self,
        candidates: list[CandidateContext],
        results: list[StrategyResult],
        as_of: datetime,
    ) -> EvaluationReport:
        """不截掉超出一批的候选；失败批次单独标记，其余批次仍可完成。"""
        report = EvaluationReport(
            generated_at=as_of,
            market_date=max((c.market_date for c in candidates if c.market_date), default=None),
            model=self.settings.llm_model,
            status="no_candidates",
            strategy_results=results,
            candidates=candidates,
        )
        failed_strategies = [r.strategy_name for r in results if r.status == "error"]
        if failed_strategies:
            report.notices.append("硬规则执行失败：" + "、".join(failed_strategies))
        if not candidates:
            if failed_strategies:
                report.status = "failed"
            return report
        if not self.settings.llm_enabled:
            report.status = "disabled"
            report.unevaluated_symbols = [c.symbol for c in candidates]
            report.notices.append("大模型接口未启用，规则候选尚未完成筛选评级。")
            return report

        try:
            client = self.client or OpenAICompatibleClient(self.settings)
        except ValueError:
            report.status = "failed"
            report.unevaluated_symbols = [c.symbol for c in candidates]
            report.notices.append("模型接口配置无效，请检查 LLM_BASE_URL 和 LLM_MODEL。")
            return report

        try:
            evidence, notices = self.evidence_provider.fetch([c.symbol for c in candidates], as_of)
            evidence, filtered_notices = usable_evidence(
                evidence,
                [c.symbol for c in candidates],
                as_of,
                self.settings,
            )
            report.notices.extend(notices + filtered_notices)
        except Exception as exc:
            logger.warning(f"外部证据接口异常：{type(exc).__name__}")
            evidence = []
            report.notices.append("外部证据接口异常，本次仅使用内部依据。")

        for candidate in candidates:
            relevant = [e for e in evidence if e.symbol in (candidate.symbol, "*")]
            candidate.external_evidence = relevant[: self.settings.external_evidence_max_per_symbol]
            if len(relevant) > len(candidate.external_evidence):
                report.notices.append(f"{candidate.symbol} 外部依据超出上限，保留最新资料。")

        size = self.settings.llm_batch_size
        for offset in range(0, len(candidates), size):
            batch = candidates[offset : offset + size]
            assessments = self._evaluate_batch(client, batch, as_of)
            if assessments is None:
                report.unevaluated_symbols.extend(c.symbol for c in batch)
                report.notices.append(f"第 {offset // size + 1} 批模型调用或输出校验失败。")
            else:
                report.assessments.extend(assessments)

        # 所有批次采用统一评级口径；报告按等级和决策排序，不进行隐式全局 Top-K。
        decision_order = {"buy": 0, "watch": 1, "reject": 2}
        report.assessments.sort(key=lambda a: (decision_order[a.decision], a.grade, a.symbol))
        if not report.assessments:
            report.status = "failed"
        elif report.unevaluated_symbols or failed_strategies:
            report.status = "partial"
        else:
            report.status = "complete"
        return report

    def _evaluate_batch(
        self,
        client: BaseLlmClient,
        candidates: list[CandidateContext],
        as_of: datetime,
    ) -> list[StockAssessment] | None:
        """传输错误和非法输出均有限重试；日志不包含 API 响应、密钥或原始提示词。"""
        messages = build_messages(candidates, as_of)
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                raw = client.complete(messages)
                batch = AssessmentBatch.model_validate_json(raw)
                self._validate_references(batch.assessments, candidates)
                return batch.assessments
            except (requests.RequestException, ValueError) as exc:
                logger.warning(f"模型评估第 {attempt + 1} 次失败：{type(exc).__name__}")
                if attempt < self.settings.llm_max_retries:
                    time.sleep(min(2**attempt, 8))
        return None

    @staticmethod
    def _validate_references(
        assessments: list[StockAssessment],
        candidates: list[CandidateContext],
    ) -> None:
        """拒绝漏评、重复股票、越界股票、跨股票引用和不存在的来源。

        这是结构与来源一致性检查，不能机械证明自然语言解释已经正确理解事实。
        最终报告保留原始证据，供人工核对解释是否得到事实支持。
        """
        contexts = {c.symbol: c for c in candidates}
        output_symbols = [a.symbol for a in assessments]
        if len(output_symbols) != len(contexts) or set(output_symbols) != set(contexts):
            raise ValueError("模型必须逐只覆盖本批候选且不重复")
        for assessment in assessments:
            candidate = contexts[assessment.symbol]
            internal_ids = {e.evidence_id for e in candidate.internal_evidence}
            external_ids = {e.evidence_id for e in candidate.external_evidence}
            if not set(assessment.internal_evidence_ids).issubset(internal_ids):
                raise ValueError("模型引用了不存在的内部证据")
            if not set(assessment.external_evidence_ids).issubset(external_ids):
                raise ValueError("模型引用了不存在或属于其他股票的外部证据")
            if assessment.decision == "buy" and not candidate.buy_eligible:
                raise ValueError("行情无效或过旧的候选不可成为购入推荐")
            if not assessment.external_evidence_ids:
                # 用固定文本覆盖无引用的外部解释，不能把无来源的断言发布成外部依据。
                assessment.external_reason = NO_EXTERNAL_REASON
