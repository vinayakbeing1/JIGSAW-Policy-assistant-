from app.config import get_settings
from app.rag import rerank
from tests.fakes import OverlapReranker


def test_reranking_can_be_switched_off():
    assert rerank.reranker() is None  # RERANKER_ENABLED=false in the test environment
    assert rerank.status() == {"ok": True, "enabled": False}


def test_reranker_can_be_replaced():
    fake = OverlapReranker()
    rerank.use_reranker(fake)
    assert rerank.reranker() is fake
    assert rerank.status() == {"ok": True, "enabled": True, "model": fake.name}
    passages = ["The escape of water excess is £350.", "Travel"]
    assert fake.score("escape of water excess", passages) == [1.0, 0.0]


def test_model_that_cannot_load_is_reported_and_not_retried(monkeypatch):
    attempts = []

    class Missing:
        def __init__(self, *args):
            attempts.append(args)
            raise OSError("model files not found in /opt/models")

    monkeypatch.setattr(rerank, "CrossEncoderReranker", Missing)
    monkeypatch.setenv("RERANKER_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert rerank.reranker() is None
        assert rerank.reranker() is None
        assert len(attempts) == 1
        assert rerank.status() == {
            "ok": False,
            "enabled": True,
            "model": get_settings().reranker_model,
            "error": "OSError",  # no message text: /ready is public
        }
    finally:
        monkeypatch.setenv("RERANKER_ENABLED", "false")
        get_settings.cache_clear()
