"""两部分报告、本地留档及统一飞书发送测试。"""

import json
from unittest.mock import Mock

import pytest

from sequoia_x.evaluation.report import build_report_sections, save_report
from sequoia_x.evaluation.service import EvaluationService
from sequoia_x.notify.feishu import _REPORT_CARD_MAX_BYTES, FeishuNotifier
from tests.evaluation_helpers import AS_OF, FakeClient, make_candidate, make_evidence, make_settings


def test_report_contains_plans_and_traceable_internal_external_basis(tmp_path):
    provider = Mock()
    provider.fetch.return_value = ([make_evidence()], [])
    client = FakeClient(
        lambda rows: [
            dict(r, external_reason="公告事实有待核对", external_evidence_ids=["news:600000:1"])
            for r in rows
        ]
    )
    report = EvaluationService(make_settings(), client, provider).evaluate(
        [make_candidate()], [], AS_OF
    )
    first, second = build_report_sections(report)
    assert "今日购入推荐：600000" in first
    assert "短线" in first and "长线" in first and "失效" in first
    assert "内部判断" in second and "外部判断" in second
    assert "https://example.com/news/1" in second and "可用：" in second
    json_path, markdown_path = save_report(report, str(tmp_path))
    saved = json.loads(json_path.read_text("utf-8"))
    assert saved["candidates"][0]["external_evidence"][0]["summary"] == "公告披露的事实摘要"
    assert "llm_api_key" not in saved
    assert first in markdown_path.read_text("utf-8") and second in markdown_path.read_text("utf-8")
    assert save_report(report, str(tmp_path))[0] != json_path


def test_disabled_report_does_not_promote_raw_candidates():
    report = EvaluationService(make_settings(llm_enabled=False)).evaluate(
        [make_candidate()], [], AS_OF
    )
    first, second = build_report_sections(report)
    assert "今日购入推荐：无" in first
    assert "未完成评估" in first and "600000" in first
    assert "尚无模型决策" in second


def test_long_unicode_report_is_delivered_without_truncation(monkeypatch):
    settings = make_settings(strategy_webhooks={"report": "https://example.com/report"})
    report = EvaluationService(settings, FakeClient()).evaluate(
        [make_candidate(f"{i:06d}") for i in range(30)],
        [],
        AS_OF,
    )
    for assessment in report.assessments:
        assessment.risks = ["风险喵😺" * 70] * 10
    notifier = FeishuNotifier(settings)
    cards = notifier._build_report_cards(report)
    assert len(cards) > 1
    assert all(
        len(json.dumps(c, ensure_ascii=False).encode("utf-8")) <= _REPORT_CARD_MAX_BYTES
        for c in cards
    )
    full_text = "".join(
        element["text"]["content"].split("\n", 1)[1]
        for card in cards
        for element in card["card"]["elements"]
    )
    assert full_text == "".join(build_report_sections(report))
    response = Mock(status_code=200)
    response.json.return_value = {"code": 0}
    post = Mock(return_value=response)
    monkeypatch.setattr("sequoia_x.notify.feishu.requests.post", post)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert notifier.send_report(report)
    assert post.call_count == len(cards)
    assert all(call.args[0] == "https://example.com/report" for call in post.call_args_list)


def test_split_accounts_for_json_escaping():
    text = '\x00"\n中文😺' * 3000
    chunks = FeishuNotifier._split_text(text)
    assert "".join(chunks) == text
    assert all(
        len(json.dumps(chunk, ensure_ascii=False).encode("utf-8")) - 2 <= 6000 for chunk in chunks
    )


@pytest.mark.parametrize("mode", ["http", "business", "invalid_json"])
def test_report_send_failure_returns_false(monkeypatch, mode):
    settings = make_settings()
    report = EvaluationService(settings, FakeClient()).evaluate([make_candidate()], [], AS_OF)
    response = Mock(status_code=500 if mode == "http" else 200)
    if mode == "invalid_json":
        response.json.side_effect = ValueError("not JSON")
    else:
        response.json.return_value = {"code": 1 if mode == "business" else 0}
    monkeypatch.setattr("sequoia_x.notify.feishu.requests.post", Mock(return_value=response))
    assert not FeishuNotifier(settings).send_report(report)
