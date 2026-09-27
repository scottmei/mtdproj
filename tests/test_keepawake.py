from busapp import keepawake
from busapp.keepawake import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, prevent_sleep


def test_requests_then_releases(monkeypatch):
    calls = []
    monkeypatch.setattr(keepawake, "_set_state", lambda f: calls.append(f) or True)
    with prevent_sleep() as active:
        assert active and calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    assert calls[-1] == ES_CONTINUOUS


def test_released_even_on_error(monkeypatch):
    calls = []
    monkeypatch.setattr(keepawake, "_set_state", lambda f: calls.append(f) or True)
    try:
        with prevent_sleep():
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    assert calls[-1] == ES_CONTINUOUS


def test_unsupported_platform_is_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(keepawake, "_set_state", lambda f: calls.append(f) or False)
    with prevent_sleep() as active:
        assert active is False
    assert calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]  # no release call needed
