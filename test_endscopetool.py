"""Unit tests for the protocol helpers."""

import pytest
import trio

from endscopetool import (
    META_ATTEMPTS,
    META_TIMEOUT,
    EndscopeConnection,
    absolute_frame_from_raw,
    describe_system_info,
    get_battery_level,
    parse_reply,
)


def test_frame_counts_up_normally() -> None:
    assert absolute_frame_from_raw(5, 4) == 5


def test_frame_wraps_forward_past_255() -> None:
    assert absolute_frame_from_raw(1, 255) == 257
    assert absolute_frame_from_raw(3, 1000) == 1027


def test_frame_late_part_from_before_wrap() -> None:
    # A straggler from frame 255 arriving after we've moved on to 256.
    assert absolute_frame_from_raw(255, 256) == 255


def test_parse_reply_strips_device_padding() -> None:
    # The real device sends uninitialised buffer bytes after the newline
    # (often 0xaa, sometimes arbitrary bytes before the 0xaa padding).
    raw = b"type=1003&value=50\n\n" + b"\xaa" * 32
    assert parse_reply(raw) == {"type": "1003", "value": "50"}
    raw_garbage = b"type=1003&value=100\n\x04!@" + b"\xaa" * 28
    assert parse_reply(raw_garbage) == {"type": "1003", "value": "100"}


def test_battery() -> None:
    reply = parse_reply(b"type=2001&data=42\n")
    assert get_battery_level(reply) == 0.42


def test_battery_garbage() -> None:
    assert get_battery_level({"type": "2001", "data": "x"}) is None
    assert get_battery_level({"type": "2001"}) is None


def test_describe_system_info() -> None:
    info = parse_reply(b"type=2002&w=640&h=480&fps=20&firmware=123&junk=1\n")
    assert describe_system_info(info) == "firmware=123, w=640, h=480, fps=20"
    assert describe_system_info(None) == "unknown"


class ScriptedTransport:
    """Meta transport that answers each send from a script.

    Each script entry is a list of (delay, reply) pairs to deliver in
    response to the corresponding send; an empty list means "lost".
    """

    def __init__(self, nursery: trio.Nursery) -> None:
        self.nursery = nursery
        self.script: list[list[tuple[float, bytes]]] = []
        self.sent: list[bytes] = []
        self.tx, self.rx = trio.open_memory_channel[bytes](100)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)
        for delay, reply in self.script.pop(0) if self.script else []:
            self.nursery.start_soon(self._deliver, delay, reply)

    async def _deliver(self, delay: float, reply: bytes) -> None:
        await trio.sleep(delay)
        self.tx.send_nowait(reply)

    async def recv(self) -> bytes:
        return await self.rx.receive()

    async def aclose(self) -> None:
        pass


@pytest.fixture
def env(nursery: trio.Nursery):
    meta = ScriptedTransport(nursery)
    conn = EndscopeConnection(meta, meta)
    return conn, meta


async def test_immediate_reply(env, autojump_clock: trio.abc.Clock) -> None:
    conn, meta = env
    meta.script = [[(0.01, b"type=2001&data=80\n")]]
    assert await conn.query_battery() == 0.8
    assert len(meta.sent) == 1


async def test_retries_after_lost_reply(env, autojump_clock: trio.abc.Clock) -> None:
    conn, meta = env
    meta.script = [[], [(0.01, b"type=2001&data=80\n")]]
    assert await conn.query_battery() == 0.8
    assert len(meta.sent) == 2


async def test_gives_up_after_all_attempts(env, autojump_clock: trio.abc.Clock) -> None:
    conn, meta = env
    meta.script = [[]] * META_ATTEMPTS
    assert await conn.query_battery() is None
    assert len(meta.sent) == META_ATTEMPTS
    assert trio.current_time() == pytest.approx(META_ATTEMPTS * META_TIMEOUT)


async def test_discards_reply_of_wrong_type(
    env, autojump_clock: trio.abc.Clock
) -> None:
    # A late brightness reply turns up first; it must not be taken as
    # the battery reply.
    conn, meta = env
    meta.script = [[(0.01, b"type=1003&value=50\n"), (0.02, b"type=2001&data=80\n")]]
    assert await conn.query_battery() == 0.8
    assert len(meta.sent) == 1


async def test_discards_late_reply_to_earlier_brightness(
    env, autojump_clock: trio.abc.Clock
) -> None:
    # The reply to an earlier "set 50" arrives during "set 60": same
    # type, different value, so it must be skipped.
    conn, meta = env
    total = META_ATTEMPTS * META_TIMEOUT
    meta.script = [
        # "set 50" gets one reply, but only after we've given up...
        [(total + 0.05, b"type=1003&value=50\n")],
        *[[]] * (META_ATTEMPTS - 1),
        # ...so it turns up during "set 60", before 60's own reply.
        [(0.1, b"type=1003&value=60\n")],
    ]
    assert await conn.set_brightness(50) is None
    assert await conn.set_brightness(60) == 60
