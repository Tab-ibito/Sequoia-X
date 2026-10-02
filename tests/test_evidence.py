"""外部接口契约、时效和隔离测试。"""

import json
from datetime import timedelta
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from sequoia_x.evaluation.evidence import ConfiguredEvidenceProvider, usable_evidence
from tests.evaluation_helpers import AS_OF, make_evidence, make_settings


@pytest.mark.parametrize(
    "times",
    [
        {"published_at": AS_OF.replace(tzinfo=None)},
        {"available_at": AS_OF.replace(tzinfo=None)},
        {"available_at": AS_OF - timedelta(days=1)},
    ],
)
def test_ambiguous_or_reversed_timestamps_are_rejected(times):
    with pytest.raises(ValidationError):
        make_evidence(**times)


def test_filter_rejects_future_expired_unrelated_and_duplicate_ids():
    valid = make_evidence()
    global_fact = make_evidence(evidence_id="market", symbol="*")
    duplicate = make_evidence(evidence_id="duplicate")
    bad = [
        make_evidence(evidence_id="future", available_at=AS_OF + timedelta(minutes=1)),
        make_evidence(evidence_id="expired", published_at=AS_OF - timedelta(days=31)),
        make_evidence(evidence_id="unrelated", symbol="600999"),
        duplicate,
        duplicate,
    ]
    selected, notices = usable_evidence(
        [valid, global_fact] + bad, ["600000"], AS_OF, make_settings()
    )
    assert {e.evidence_id for e in selected} == {"news:600000:1", "market"}
    assert notices


def test_local_file_preserves_valid_items_when_one_item_is_invalid(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(
        json.dumps({"evidence": [make_evidence().model_dump(mode="json"), {}]}), "utf-8"
    )
    evidence, notices = ConfiguredEvidenceProvider(
        make_settings(external_evidence_path=str(path))
    ).fetch(
        ["600000"],
        AS_OF,
    )
    assert len(evidence) == 1 and notices


def test_http_contract_and_separate_api_key(monkeypatch):
    response = Mock(status_code=200)
    response.json.return_value = {"evidence": [make_evidence().model_dump(mode="json")]}
    post = Mock(return_value=response)
    monkeypatch.setattr("sequoia_x.evaluation.evidence.requests.post", post)
    settings = make_settings(
        external_evidence_url="https://example.com/evidence",
        external_evidence_api_key="external-secret",
        llm_api_key="model-secret",
    )
    evidence, notices = ConfiguredEvidenceProvider(settings).fetch(["600000"], AS_OF)
    assert len(evidence) == 1 and not notices
    assert post.call_args.kwargs["json"] == {"symbols": ["600000"], "as_of": AS_OF.isoformat()}
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer external-secret"}
    assert "model-secret" not in str(post.call_args)


def test_broken_file_does_not_discard_api_data(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("broken JSON", "utf-8")
    response = Mock(status_code=200)
    response.json.return_value = {"evidence": [make_evidence().model_dump(mode="json")]}
    monkeypatch.setattr("sequoia_x.evaluation.evidence.requests.post", Mock(return_value=response))
    evidence, notices = ConfiguredEvidenceProvider(
        make_settings(
            external_evidence_path=str(path),
            external_evidence_url="https://example.com/evidence",
        )
    ).fetch(["600000"], AS_OF)
    assert len(evidence) == 1 and notices
