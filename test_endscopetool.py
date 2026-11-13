"""Unit tests for the protocol helpers."""

from io import BytesIO

import numpy as np
import pytest
import trio
from PIL import Image, ImageFile

from endscopetool import (
    META_ATTEMPTS,
    META_TIMEOUT,
    EndscopeConnection,
    FrameAssembler,
    absolute_frame_from_raw,
    decode_jpeg,
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


def make_packet(
    raw_frame: int,
    part: int,
    num_parts: int,
    payload: bytes,
    rotation: int = 90,
) -> bytes:
    is_last = part == num_parts - 1
    header = bytearray(8)
    header[0] = raw_frame % 256
    header[1] = 1 if is_last else 0
    header[2] = part
    header[3] = num_parts if is_last else 0
    header[4:6] = rotation.to_bytes(2, "big")
    return bytes(header) + payload


def test_assembler_reorders_parts_and_wraps_past_255() -> None:
    asm = FrameAssembler()
    # Jump gradually up to frame 255, then wrap to 256 (raw 0).
    for f in (100, 200, 255):
        out = asm.push(make_packet(f, 0, 1, b"ok"), now=float(f))
        assert out is not None and out.frame == f

    # Frame 256 (raw 0) arrives out of order: parts 2, 0, 1.
    assert asm.push(make_packet(0, 2, 3, b"C", rotation=180), now=10.0) is None
    assert asm.push(make_packet(0, 0, 3, b"A", rotation=180), now=10.1) is None
    out = asm.push(make_packet(0, 1, 3, b"B", rotation=180), now=10.2)
    assert out is not None
    assert out.frame == 256
    assert out.data == b"ABC"
    assert out.rotation == 180
    assert out.started_at == 10.0


def test_assembler_drops_incomplete_and_ignores_late_stragglers() -> None:
    asm = FrameAssembler()
    # Frame 0 loses part 2.
    assert asm.push(make_packet(0, 0, 3, b"a0"), now=1.0) is None
    assert asm.push(make_packet(0, 1, 3, b"a1"), now=1.01) is None

    # Frame 1 completes -> frame 0 is dropped.
    assert asm.push(make_packet(1, 0, 2, b"b0"), now=1.05) is None
    out = asm.push(make_packet(1, 1, 2, b"b1"), now=1.06)
    assert out is not None and out.frame == 1 and out.data == b"b0b1"
    assert asm.frames == {}

    # A late straggler for frame 0 must not resurrect frame 0 in asm.frames.
    assert asm.push(make_packet(0, 2, 3, b"a2"), now=1.07) is None
    assert asm.frames == {}


def test_assembler_partial_uses_only_contiguous_prefix() -> None:
    asm = FrameAssembler(min_completeness=0.6)
    # Frame 0 has 5 parts; part 3 is lost while part 4 (frame_end=1) arrives.
    # Only the contiguous prefix 0..2 (3/5 = 60%) is usable; part 4 must be
    # excluded because baseline JPEG without restart markers is garbage past
    # the first gap.
    for p, data in ((0, b"p0"), (1, b"p1"), (2, b"p2"), (4, b"p4")):
        assert asm.push(make_packet(0, p, 5, data), now=1.0) is None

    # First packet of frame 1 flushes frame 0 as a partial frame.
    out = asm.push(make_packet(1, 0, 5, b"next"), now=1.05)
    assert out is not None
    assert out.frame == 0
    assert out.partial is True
    assert out.data == b"p0p1p2"
    assert out.prefix_parts == 3
    assert out.expected_parts == 5
    assert out.expected_known is True


def test_assembler_partial_estimates_expected_when_tail_lost() -> None:
    asm = FrameAssembler(min_completeness=0.75)
    # Frame 0 completes with 8 parts so the assembler learns last_num_parts=8.
    for p in range(7):
        assert asm.push(make_packet(0, p, 8, f"{p}".encode()), now=1.0) is None
    full = asm.push(make_packet(0, 7, 8, b"7"), now=1.01)
    assert full is not None and not full.partial

    # Frame 1 loses parts 6 and 7 (including frame_end=1), leaving 6/~8 = 75%.
    for p in range(6):
        assert asm.push(make_packet(1, p, 8, f"b{p}".encode()), now=1.05) is None
    out = asm.push(make_packet(2, 0, 8, b"c0"), now=1.10)
    assert out is not None
    assert out.frame == 1
    assert out.partial is True
    assert out.prefix_parts == 6
    assert out.expected_parts == 8
    assert out.expected_known is False

    # If frame 2 actually grew to 9 parts and lost part 8 (receiving 0..7 with
    # frame_end=0), expected_parts must be at least max(parts) + 2 = 9, not 8.
    for p in range(1, 8):
        assert asm.push(make_packet(2, p, 9, f"c{p}".encode()), now=1.11) is None
    out2 = asm.push(make_packet(3, 0, 9, b"d0"), now=1.15)
    assert out2 is not None
    assert out2.frame == 2
    assert out2.partial is True
    assert out2.prefix_parts == 8
    assert out2.expected_parts == 9
    assert out2.expected_known is False


def test_decode_jpeg_composites_partial_onto_previous_frame() -> None:
    rng = np.random.default_rng(123)
    prev_bgr = np.full((480, 640, 3), (200, 50, 10), dtype=np.uint8)

    # Encode a distinct noisy RGB image as baseline JPEG.
    new_rgb = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    buf = BytesIO()
    Image.fromarray(new_rgb).save(buf, format="JPEG", quality=75)
    full_jpeg = buf.getvalue()

    full_bgr, full_rows = decode_jpeg(full_jpeg, partial=False)
    assert full_rows == 480

    # Truncate to 80% of the byte stream and composite onto prev_bgr.
    truncated = full_jpeg[: int(len(full_jpeg) * 0.8)]
    comp_bgr, valid_rows = decode_jpeg(truncated, partial=True, prev_image_cv=prev_bgr)
    assert 200 < valid_rows < 480
    # Intact top rows match the full decode pixel-for-pixel; bottom rows retain prev_bgr.
    assert np.array_equal(comp_bgr[:valid_rows], full_bgr[:valid_rows])
    assert np.array_equal(comp_bgr[valid_rows:], prev_bgr[valid_rows:])
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False


def test_decode_jpeg_rejects_header_only_truncation() -> None:
    with pytest.raises(OSError):
        decode_jpeg(b"\xff\xd8\xff\xe0\x00\x10JFIF", partial=True)
    assert ImageFile.LOAD_TRUNCATED_IMAGES is False
