"""Retry on an empty/unparseable LLM response body.

An OpenAI-compatible endpoint that returns HTTP 200 with no JSON body makes the
openai SDK parse ``None``, and langchain-openai then raises ``ValueError:
Unexpected response type ... got NoneType: None``. The SDK's ``max_retries``
covers only retryable HTTP statuses and connection errors, so this transient
backend blank used to bubble straight up and kill the whole multi-agent run.

``NormalizedChatOpenAI.invoke`` retries that specific fault a few times with
backoff, and lets every other ValueError (a genuine API error) through at once.
"""

import pytest

import tradingagents.llm_clients.openai_client as oc
from tradingagents.llm_clients.openai_client import NormalizedChatOpenAI, _is_empty_response


def _empty_response_error():
    return ValueError(
        "Unexpected response type from OpenAI-compatible endpoint. "
        "Expected a dict or openai.BaseModel, got NoneType: None"
    )


def _client_with(retries):
    llm = NormalizedChatOpenAI(model="m", api_key="x")
    llm.empty_response_retries = retries
    return llm


@pytest.mark.unit
def test_is_empty_response_matches_the_known_message():
    assert _is_empty_response(_empty_response_error())


@pytest.mark.unit
def test_is_empty_response_rejects_other_value_errors():
    assert not _is_empty_response(ValueError("some other error"))
    assert not _is_empty_response(ValueError("{'error': 'rate limited'}"))


@pytest.mark.unit
def test_empty_response_retries_then_succeeds(monkeypatch):
    llm = _client_with(retries=3)
    results = iter([_empty_response_error(), _empty_response_error(), "OK"])

    def flaky(*a, **k):
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(oc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm, "_invoke_once", flaky)
    assert llm.invoke("hi") == "OK"


@pytest.mark.unit
def test_empty_response_exhausted_retries_raises(monkeypatch):
    llm = _client_with(retries=2)

    def always_empty(*a, **k):
        raise _empty_response_error()

    monkeypatch.setattr(oc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm, "_invoke_once", always_empty)
    with pytest.raises(ValueError, match="got NoneType"):
        llm.invoke("hi")


@pytest.mark.unit
def test_non_empty_value_error_not_retried(monkeypatch):
    llm = _client_with(retries=3)
    calls = []

    def fail(*a, **k):
        calls.append(1)
        raise ValueError("a genuine API error, not a blank body")

    monkeypatch.setattr(oc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm, "_invoke_once", fail)
    with pytest.raises(ValueError, match="genuine"):
        llm.invoke("hi")
    assert len(calls) == 1


@pytest.mark.unit
def test_success_returns_without_retry(monkeypatch):
    llm = _client_with(retries=3)
    calls = []

    def ok(*a, **k):
        calls.append(1)
        return "GOOD"

    monkeypatch.setattr(oc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(llm, "_invoke_once", ok)
    assert llm.invoke("hi") == "GOOD"
    assert len(calls) == 1
