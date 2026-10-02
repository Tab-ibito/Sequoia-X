"""兼容大模型客户端测试：请求协议、拒绝、截断及无鉴权本地服务。"""

from unittest.mock import Mock

import pytest

from sequoia_x.evaluation.client import LlmResponseError, OpenAICompatibleClient
from tests.evaluation_helpers import make_settings


def mock_response(**choice_overrides):
    choice = {"finish_reason": "stop", "message": {"content": '{"assessments": []}'}}
    choice.update(choice_overrides)
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [choice]}
    return response


def test_request_uses_configured_protocol_and_key(monkeypatch):
    post = Mock(return_value=mock_response())
    monkeypatch.setattr("sequoia_x.evaluation.client.requests.post", post)
    settings = make_settings(llm_api_key="test-secret", llm_token_parameter="max_completion_tokens")
    messages = [{"role": "system", "content": "return JSON"}]
    assert OpenAICompatibleClient(settings).complete(messages) == '{"assessments": []}'
    assert post.call_args.args[0] == "https://example.com/v1/chat/completions"
    payload = post.call_args.kwargs["json"]
    assert payload["messages"] == messages
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_completion_tokens"] == settings.llm_max_tokens
    assert "max_tokens" not in payload
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-secret"


def test_local_service_and_json_mode_disabled(monkeypatch):
    post = Mock(return_value=mock_response())
    monkeypatch.setattr("sequoia_x.evaluation.client.requests.post", post)
    OpenAICompatibleClient(make_settings(llm_api_key="", llm_json_mode=False)).complete([])
    assert "Authorization" not in post.call_args.kwargs["headers"]
    assert "response_format" not in post.call_args.kwargs["json"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"finish_reason": "length"},
        {"finish_reason": "content_filter"},
        {"message": {"content": "", "refusal": "refused"}},
        {"message": {"content": []}},
        {"message": {}},
    ],
)
def test_incomplete_or_refused_response_is_not_accepted(monkeypatch, overrides):
    monkeypatch.setattr(
        "sequoia_x.evaluation.client.requests.post", Mock(return_value=mock_response(**overrides))
    )
    with pytest.raises(LlmResponseError):
        OpenAICompatibleClient(make_settings()).complete([])


def test_http_error_does_not_expose_server_body(monkeypatch):
    response = Mock(status_code=401, text="potential-secret")
    monkeypatch.setattr("sequoia_x.evaluation.client.requests.post", Mock(return_value=response))
    with pytest.raises(LlmResponseError) as error:
        OpenAICompatibleClient(make_settings()).complete([])
    assert "potential-secret" not in str(error.value)
