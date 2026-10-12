import pytest
import threading
from concurrent.futures import ThreadPoolExecutor

from thechatplace.providers.claude import ClaudeConversations
from thechatplace.session_controller import SessionController
from thechatplace.sessions import OWN, SessionInfo
from thechatplace.transcript import ChatMessage, Transcript, ASSISTANT
from records import assistant_block, lines, text_block


class FakeReader:
    def __init__(self, text):
        self.transcript = Transcript()
        self.transcript.messages = [ChatMessage(ASSISTANT, text)]
        self.refreshed = False

    def refresh(self):
        self.refreshed = True
        return True


class FakeProvider:
    def __init__(self, provider_id, text):
        self.provider_id = provider_id
        self.reader = FakeReader(text)
        self.seen = []

    def open_reader(self, session):
        self.seen.append(session.identity)
        return self.reader

    def has_history(self, session):
        return True

    def last_reply(self, session):
        self.seen.append(session.identity)
        return self.reader.transcript.messages[-1].text


def info(provider="claude"):
    return SessionInfo(OWN, f"{provider}:same", "Chat", "", "same", provider=provider)


def test_routes_colliding_native_ids_to_their_own_readers():
    claude, codex = FakeProvider("claude", "Claude reply"), FakeProvider("codex", "Codex reply")
    controller = SessionController([claude, codex])
    assert controller.read(info()).messages[-1].text == "Claude reply"
    assert controller.read(info("codex")).messages[-1].text == "Codex reply"
    assert controller.last_reply(info("codex")) == "Codex reply"
    assert claude.seen == [("claude", "same")]
    assert codex.seen == [("codex", "same"), ("codex", "same")]
    assert claude.reader.refreshed and codex.reader.refreshed


def test_unavailable_provider_never_falls_back_to_claude():
    provider = FakeProvider("claude", "Wrong history")
    controller = SessionController([provider])
    with pytest.raises(ValueError, match="Provider unavailable: codex"):
        controller.read(info("codex"))
    assert not provider.seen
    assert controller.read(info()).messages[-1].text == "Wrong history"


def test_duplicate_backend_registration_is_rejected():
    with pytest.raises(ValueError, match="Duplicate provider"):
        SessionController([FakeProvider("claude", "a"), FakeProvider("claude", "b")])


def test_missing_claude_history_returns_none(monkeypatch):
    monkeypatch.setattr(SessionInfo, "transcript_path", lambda self: None)
    controller = SessionController([ClaudeConversations()])
    assert controller.read(info()) is None
    assert not controller.has_history(info())
    assert controller.last_reply(info()) == ""


def test_claude_reader_preserves_history_and_tail_without_writing(tmp_path, monkeypatch):
    path = tmp_path / "chat.jsonl"
    path.write_text("\n".join(lines(assistant_block(text_block("Existing reply"), "answer"))) + "\n",
                    encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(SessionInfo, "transcript_path", lambda self: path)
    controller = SessionController([ClaudeConversations()])
    assert controller.read(info()).last_reply().text == "Existing reply"
    assert controller.last_reply(info()) == "Existing reply"
    assert path.read_bytes() == before


def test_claude_adapter_rejects_foreign_session_before_path_lookup(monkeypatch):
    def unexpected(self):
        pytest.fail("Foreign session attempted Claude path lookup")

    monkeypatch.setattr(SessionInfo, "transcript_path", unexpected)
    provider = ClaudeConversations()
    with pytest.raises(ValueError, match="Not a Claude session"):
        provider.open_reader(info("codex"))
    with pytest.raises(ValueError, match="Not a Claude session"):
        provider.last_reply(info("codex"))


def test_controller_shutdown_closes_every_backend_and_rejects_further_reads():
    providers = [FakeProvider("claude", "a"), FakeProvider("codex", "b")]
    closed = []
    for provider in providers:
        provider.close = lambda name=provider.provider_id: closed.append(name)
    controller = SessionController(providers)
    controller.close()
    controller.close()
    assert closed == ["claude", "codex"]
    with pytest.raises(ValueError, match="controller closed"):
        controller.read(info())


def test_controller_cleanup_failure_does_not_skip_other_backend_or_expose_error():
    claude, codex = FakeProvider("claude", "a"), FakeProvider("codex", "b")
    closed = []

    def failed_close():
        closed.append("claude")
        raise RuntimeError("private payload")

    claude.close = failed_close
    codex.close = lambda: closed.append("codex")
    controller = SessionController([claude, codex])
    with pytest.raises(RuntimeError, match="^Session provider cleanup failed$"):
        controller.close()
    assert closed == ["claude", "codex"]


def test_concurrent_controller_close_waits_for_same_backend_cleanup():
    provider = FakeProvider("claude", "reply")
    entered, release = threading.Event(), threading.Event()
    calls = []

    def close_provider():
        calls.append("close")
        entered.set()
        assert release.wait(5)

    provider.close = close_provider
    controller = SessionController([provider])
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(controller.close)
        assert entered.wait(5)
        second = pool.submit(controller.close)
        try:
            assert not first.done() and not second.done()
            with pytest.raises(ValueError, match="controller closed"):
                controller.read(info())
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert calls == ["close"]
