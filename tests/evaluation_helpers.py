"""评估链路测试用事实与客户端，所有网络均由测试替身隔离。"""

import json
from datetime import datetime

from sequoia_x.core.config import Settings
from sequoia_x.evaluation.client import BaseLlmClient
from sequoia_x.evaluation.models import CandidateContext, ExternalEvidence, InternalEvidence

AS_OF = datetime.fromisoformat("2026-10-02T19:00:00+08:00")


def make_settings(**overrides) -> Settings:
    values = {
        "feishu_webhook_url": "https://example.com/default",
        "llm_enabled": True,
        "llm_model": "test-model",
        "llm_base_url": "https://example.com/v1",
        "llm_max_retries": 0,
        "external_evidence_path": "",
        "external_evidence_url": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_candidate(symbol: str = "600000", buy_eligible: bool = True) -> CandidateContext:
    return CandidateContext(
        symbol=symbol,
        matched_strategies=["TestStrategy"],
        market_date=AS_OF.date(),
        buy_eligible=buy_eligible,
        internal_evidence=[
            InternalEvidence(
                evidence_id=f"internal:{symbol}:rule",
                source="TestStrategy",
                description="本次满足测试突破条件",
            )
        ],
    )


def make_evidence(**overrides) -> ExternalEvidence:
    values = {
        "evidence_id": "news:600000:1",
        "symbol": "600000",
        "title": "测试公告",
        "source": "测试来源",
        "url": "https://example.com/news/1",
        "published_at": AS_OF.replace(hour=16),
        "available_at": AS_OF.replace(hour=17),
        "summary": "公告披露的事实摘要",
    }
    values.update(overrides)
    return ExternalEvidence(**values)


def make_assessment(candidate: CandidateContext, **overrides) -> dict:
    plan = {
        "entry_condition": "确认突破后再考虑",
        "exit_condition": "趋势转弱退出",
        "invalidation": "跌破整理区间则判断失效",
    }
    values = {
        "symbol": candidate.symbol,
        "grade": "B",
        "decision": "buy",
        "summary": "突破具备条件，等待确认",
        "short_term": plan,
        "long_term": plan,
        "internal_reason": "本次规则命中支持进一步观察",
        "internal_evidence_ids": [candidate.internal_evidence[0].evidence_id],
        "external_reason": "无外部资料",
        "external_evidence_ids": [],
        "risks": ["假突破风险"],
    }
    values.update(overrides)
    return values


class FakeClient(BaseLlmClient):
    """从输入事实自动生成测试响应；可用 transform 注入违规输出或故障。"""

    def __init__(self, transform=None) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.transform = transform

    def complete(self, messages):
        self.calls.append(messages)
        data = json.loads(messages[1]["content"])
        candidates = [CandidateContext.model_validate(c) for c in data["candidates"]]
        assessments = [make_assessment(c) for c in candidates]
        if self.transform:
            assessments = self.transform(assessments)
        return json.dumps({"assessments": assessments}, ensure_ascii=False)
