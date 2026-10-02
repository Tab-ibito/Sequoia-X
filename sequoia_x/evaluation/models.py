"""评估层的数据契约。

把输入事实、模型判断和运行状态分开保存，避免将模型生成的解释当作原始证据。
模型只能返回 AssessmentBatch；最终报告由程序装配，日期、来源和失败状态
均由本项目填写，不能交给模型自由生成。
"""

from datetime import date, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


def now_shanghai() -> datetime:
    """使用固定 UTC+8，避免 Windows 机器未安装时区数据库时无法运行。"""
    return datetime.now(timezone(timedelta(hours=8)))


class ReportModel(BaseModel):
    """拒绝未约定的字段，防止模型把额外内容混入结构化输出。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StrategyResult(ReportModel):
    """一次硬规则执行结果；失败与正常选出零只股票必须区分。"""

    strategy_name: str
    rule_description: str
    symbols: list[str] = Field(default_factory=list)
    status: Literal["ok", "error"] = "ok"


class InternalEvidence(ReportModel):
    """项目内部产生的事实，使用固定 ID 供模型引用。"""

    evidence_id: str
    source: str
    description: str


class ExternalEvidence(ReportModel):
    """外部数据接口契约，必须包含可追溯来源与公开/可用时间。

    published_at 为发布时间，available_at 为数据提供方实际可获取时间，均要求
    带时区。全市场证据用 symbol='*'，个股证据用六位代码。这里的 summary
    是提供方的事实摘要；本项目校验格式和时间，不宣称核实了网页原文。
    """

    evidence_id: str = Field(min_length=1, max_length=100)
    symbol: str = Field(pattern=r"^(\d{6}|\*)$")
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=100)
    url: HttpUrl
    published_at: datetime
    available_at: datetime
    summary: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_times(self) -> "ExternalEvidence":
        """拒绝含糊的本地时间，以及早于发布时刻的可用时间。"""
        for value in (self.published_at, self.available_at):
            if value.utcoffset() is None:
                raise ValueError("外部证据时间必须包含时区")
        if self.available_at < self.published_at:
            raise ValueError("available_at 不能早于 published_at")
        return self


class CandidateContext(ReportModel):
    """同一股票命中的策略合并为一个候选，保存本次模型能看到的所有事实。"""

    symbol: str = Field(pattern=r"^\d{6}$")
    matched_strategies: list[str]
    market_date: date | None = None
    # 指标以键值表示，缺失值写为 None，不能把 NaN/Infinity 发送到 JSON API。
    metrics: dict[str, float | None] = Field(default_factory=dict)
    internal_evidence: list[InternalEvidence] = Field(default_factory=list)
    external_evidence: list[ExternalEvidence] = Field(default_factory=list)
    data_issues: list[str] = Field(default_factory=list)
    buy_eligible: bool = False


class TradingPlan(ReportModel):
    """条件式交易计划；依据后复权行情时不能生成可直接下单的绝对价格。"""

    entry_condition: str = Field(min_length=1, max_length=400)
    exit_condition: str = Field(min_length=1, max_length=400)
    invalidation: str = Field(min_length=1, max_length=400)


class StockAssessment(ReportModel):
    """模型对单只股票的判断，证据 ID 必须通过程序的引用校验。"""

    symbol: str = Field(pattern=r"^\d{6}$")
    grade: Literal["A", "B", "C", "D"]
    decision: Literal["buy", "watch", "reject"]
    summary: str = Field(min_length=1, max_length=500)
    short_term: TradingPlan
    long_term: TradingPlan
    internal_reason: str = Field(min_length=1, max_length=800)
    internal_evidence_ids: list[str] = Field(min_length=1, max_length=20)
    external_reason: str = Field(min_length=1, max_length=800)
    external_evidence_ids: list[str] = Field(max_length=20)
    risks: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_grade(self) -> "StockAssessment":
        """A/B 允许推荐，C/D 只能观察或剔除；评级不是获利概率。"""
        if self.decision == "buy" and self.grade not in ("A", "B"):
            raise ValueError("购入推荐必须评为 A 或 B")
        if any(not risk.strip() or len(risk) > 400 for risk in self.risks):
            raise ValueError("风险说明必须为非空且不超过400字的文本")
        return self


class AssessmentBatch(ReportModel):
    """模型返回的 JSON 根对象，必须逐只覆盖本批所有候选。"""

    assessments: list[StockAssessment]


class EvaluationReport(ReportModel):
    """汇总报告；成功批次可保留，失败批次进入 unevaluated_symbols。"""

    generated_at: datetime
    market_date: date | None = None
    model: str = ""
    status: Literal["complete", "partial", "failed", "disabled", "no_candidates"]
    strategy_results: list[StrategyResult]
    candidates: list[CandidateContext]
    assessments: list[StockAssessment] = Field(default_factory=list)
    unevaluated_symbols: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
