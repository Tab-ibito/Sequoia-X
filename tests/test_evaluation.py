"""大模型评估业务测试：候选范围、事实引用、分批与失败语义。"""

import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from sequoia_x.evaluation.models import StrategyResult
from sequoia_x.evaluation.prompt import NO_EXTERNAL_REASON
from sequoia_x.evaluation.service import EvaluationService
from tests.evaluation_helpers import AS_OF, FakeClient, make_candidate, make_evidence, make_settings


def test_all_candidates_are_evaluated_in_batches():
    """批次限制只限制一次请求大小，不能丢失剩余候选。"""
    client = FakeClient()
    candidates = [make_candidate(f"{i:06d}") for i in range(23)]
    report = EvaluationService(make_settings(llm_batch_size=10), client).evaluate(
        candidates,
        [],
        AS_OF,
    )
    assert report.status == "complete"
    assert len(client.calls) == 3
    assert {a.symbol for a in report.assessments} == {c.symbol for c in candidates}
    assert all(a.external_reason == NO_EXTERNAL_REASON for a in report.assessments)


def test_empty_and_disabled_do_not_call_any_api():
    client, provider = FakeClient(), Mock()
    service = EvaluationService(make_settings(llm_enabled=False), client, provider)
    empty = service.evaluate([], [], AS_OF)
    disabled = service.evaluate([make_candidate()], [], AS_OF)
    assert empty.status == "no_candidates"
    assert disabled.status == "disabled" and not disabled.assessments
    assert disabled.unevaluated_symbols == ["600000"]
    assert not client.calls
    provider.fetch.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: [dict(rows[0], symbol="600999")],
        lambda rows: [],
        lambda rows: rows + rows,
        lambda rows: [dict(rows[0], internal_evidence_ids=["invented"])],
        lambda rows: [dict(rows[0], external_evidence_ids=["invented"])],
        lambda rows: [dict(rows[0], grade="C")],
        lambda rows: [dict(rows[0], unrelated_field="ignored instructions")],
    ],
)
def test_invalid_model_decisions_never_become_recommendations(mutation):
    """陌生股票、漏评、重复、伪造依据与等级不一致均使该批失效。"""
    client = FakeClient(mutation)
    report = EvaluationService(make_settings(), client).evaluate([make_candidate()], [], AS_OF)
    assert report.status == "failed"
    assert not report.assessments
    assert report.unevaluated_symbols == ["600000"]


def test_stale_candidate_cannot_be_recommended():
    report = EvaluationService(make_settings(), FakeClient()).evaluate(
        [make_candidate(buy_eligible=False)],
        [],
        AS_OF,
    )
    assert report.status == "failed" and not report.assessments


def test_all_watch_is_a_successful_report():
    """没有购入推荐是正常评估结果，不能强迫模型推荐。"""
    client = FakeClient(lambda rows: [dict(r, grade="C", decision="watch") for r in rows])
    report = EvaluationService(make_settings(), client).evaluate(
        [make_candidate(buy_eligible=False)],
        [],
        AS_OF,
    )
    assert report.status == "complete" and report.assessments[0].decision == "watch"


def test_failed_batch_does_not_erase_successful_batches():
    def fail_first(rows):
        if rows[0]["symbol"] == "600000":
            raise requests.Timeout("simulated timeout")
        return rows

    report = EvaluationService(make_settings(llm_batch_size=1), FakeClient(fail_first)).evaluate(
        [make_candidate("600000"), make_candidate("600001")],
        [],
        AS_OF,
    )
    assert report.status == "partial"
    assert [a.symbol for a in report.assessments] == ["600001"]
    assert report.unevaluated_symbols == ["600000"]


def test_retry_can_recover_invalid_json(monkeypatch):
    client = Mock()
    candidate = make_candidate()
    from tests.evaluation_helpers import make_assessment

    client.complete.side_effect = [
        "not JSON",
        json.dumps({"assessments": [make_assessment(candidate)]}),
    ]
    monkeypatch.setattr("sequoia_x.evaluation.service.time.sleep", lambda _: None)
    report = EvaluationService(make_settings(llm_max_retries=1), client).evaluate(
        [candidate],
        [],
        AS_OF,
    )
    assert report.status == "complete"
    assert client.complete.call_count == 2


def test_external_references_must_belong_to_this_candidate():
    provider = Mock()
    provider.fetch.return_value = ([make_evidence()], [])
    client = FakeClient(
        lambda rows: [dict(r, external_evidence_ids=["news:600000:1"]) for r in rows]
    )
    report = EvaluationService(make_settings(), client, provider).evaluate(
        [make_candidate("600000"), make_candidate("600001")],
        [],
        AS_OF,
    )
    assert report.status == "failed"


def test_only_usable_external_facts_are_sent_and_preserved():
    provider = Mock()
    valid = make_evidence()
    future = make_evidence(
        evidence_id="future",
        published_at=AS_OF + timedelta(days=1),
        available_at=AS_OF + timedelta(days=1),
    )
    provider.fetch.return_value = ([valid, future], [])
    client = FakeClient(
        lambda rows: [
            dict(r, external_reason="公告披露需继续核对", external_evidence_ids=[valid.evidence_id])
            for r in rows
        ]
    )
    report = EvaluationService(make_settings(), client, provider).evaluate(
        [make_candidate()], [], AS_OF
    )
    assert report.status == "complete"
    assert report.candidates[0].external_evidence == [valid]
    assert report.assessments[0].external_reason == "公告披露需继续核对"
    assert "future" not in client.calls[0][1]["content"]


def test_strategy_failure_is_distinct_from_no_matches():
    failed = StrategyResult(strategy_name="FailedStrategy", rule_description="test", status="error")
    service = EvaluationService(make_settings(), FakeClient())
    assert service.evaluate([], [failed], AS_OF).status == "failed"
    assert service.evaluate([make_candidate()], [failed], AS_OF).status == "partial"


def test_bad_api_config_returns_failure_without_network():
    report = EvaluationService(make_settings(llm_base_url="")).evaluate(
        [make_candidate()], [], AS_OF
    )
    assert report.status == "failed" and report.unevaluated_symbols == ["600000"]
