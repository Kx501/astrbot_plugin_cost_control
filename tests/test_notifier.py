"""``NotifierMixin`` 纯函数单测：冷却判定。"""

from cost_control.notifier import cooldown_elapsed


def test_cooldown_disabled_when_zero():
    assert cooldown_elapsed(None, 100.0, 0) is True


def test_cooldown_never_sent_allows():
    assert cooldown_elapsed(None, 100.0, 300) is True


def test_cooldown_within_window_blocks():
    assert cooldown_elapsed(100.0, 200.0, 300) is False


def test_cooldown_passed_allows():
    assert cooldown_elapsed(100.0, 500.0, 300) is True


def test_cooldown_exact_boundary_allows():
    # >= 阈值即放行
    assert cooldown_elapsed(100.0, 400.0, 300) is True


async def test_concurrent_notifications_send_once_per_session():
    import asyncio
    from types import SimpleNamespace

    from cost_control.notifier import NotifierMixin

    host = NotifierMixin()
    host.cfg = {"alerts": {"cooldown_seconds": 300}}
    preferences = {}
    sent = []

    async def read(scope, umo, key):
        value = preferences.get(umo)
        await asyncio.sleep(0)  # reproduce check/mark interleaving
        return value

    async def write(scope, umo, key, value):
        await asyncio.sleep(0)
        preferences[umo] = value

    async def send(chain):
        sent.append(chain)
        await asyncio.sleep(0)

    host.get_pref = read
    host.set_pref = write
    event = SimpleNamespace(unified_msg_origin="s", send=send)
    results = await asyncio.gather(*(host.notify(event, "alert") for _ in range(8)))
    assert sum(results) == 1
    assert len(sent) == 1
    assert len(host._notification_locks) == 0


async def test_failed_notification_does_not_consume_cooldown():
    from types import SimpleNamespace

    from cost_control.notifier import NotifierMixin

    host = NotifierMixin()
    host.cfg = {"alerts": {"cooldown_seconds": 300}}
    preferences = {}
    attempts = 0

    async def read(scope, umo, key):
        return preferences.get(umo)

    async def write(scope, umo, key, value):
        preferences[umo] = value

    async def send(chain):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary send failure")

    host.get_pref = read
    host.set_pref = write
    event = SimpleNamespace(unified_msg_origin="s", send=send)
    assert await host.notify(event, "alert") is False
    assert preferences == {}
    assert await host.notify(event, "alert") is True
    assert await host.notify(event, "alert") is False
    assert attempts == 2
