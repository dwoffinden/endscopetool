#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
#
# Python implementation of the endscopetool (sic!) Android application used for the Vitcoco ear wax remover camera thingy.
#
# Original version released at https://gist.github.com/RaphaelWimmer/5bcb286414e6cd38ed38724f9a6a6129
# under CC0 / Public Domain (0) 2023 Raphael Wimmer.
# Contributions by https://github.com/Aghei2 and https://gist.github.com/jamaggs.
# See README.md for usage and keyboard shortcuts.

import cv2
import logging
import math
import sys
import numpy as np
import trio
import argparse
from dataclasses import dataclass
from datetime import datetime
from PIL import Image, ImageFile
from io import BytesIO
from urllib.parse import parse_qs
from transports import AsyncDatagramTransport, UdpDatagramTransport

cv2.setNumThreads(1)

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

log = logging.getLogger("endscope")

# Gaps between displayed frames longer than this are logged at DEBUG.
STALL_THRESHOLD = 0.25

# Poll for keys and window events at least this often.
UI_POLL_INTERVAL = 0.05

# Give up if no video arrives for this long.
VIDEO_TIMEOUT = 5.0

# How long to wait for each meta reply, and how many times to send the command.
META_TIMEOUT = 0.4
META_ATTEMPTS = 3

# Default completeness ratio when partial-frame decoding is enabled without a
# specific value (via bare --min-completeness or the 'p' key).
DEFAULT_PARTIAL_COMPLETENESS = 0.75

# When libjpeg decodes a truncated JPEG (LOAD_TRUNCATED_IMAGES = True),
# undecoded MCU rows are filled with neutral grey (128, 128, 128). The single
# MCU row where truncation occurred has height <= 16 pixels (16 for 4:2:0,
# 8 for 4:2:2), and 4:2:0 vertical chroma upsampling blends 1 pixel across the
# MCU row boundary. Stepping back 17 rows from the last non-grey row leaves
# only completely intact scanlines.
TRUNCATED_ROW_MARGIN = 17


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if record.levelno != logging.INFO:
            msg = f"{record.levelname}: {msg}"
        return f"{self.formatTime(record, '%H:%M:%S')}.{int(record.msecs):03d} {msg}"


def setup_logging(level: int) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_Formatter())
    log.addHandler(handler)
    log.setLevel(level)


def _is_window_closed(win_name: str) -> bool:
    """Return True iff the OpenCV window has been destroyed by the user.

    After destroy, getWindowImageRect raises on both Windows (NULL HWND)
    and GTK (window removed from g_windows).  On macOS the close button
    is disabled with WINDOW_GUI_NORMAL, so this path is unreachable.
    """
    try:
        cv2.getWindowImageRect(win_name)
    except cv2.error:
        return True
    return False


class EndscopeConnection:
    def __init__(
        self, meta_channel: AsyncDatagramTransport, vid_channel: AsyncDatagramTransport
    ):
        self.meta = meta_channel
        self.vid = vid_channel

    async def _request(
        self, command: str, expect: dict[str, str], describe: str
    ) -> dict[str, str] | None:
        """Send a meta command and return its parsed reply, or None.

        UDP may lose the command or the reply, so retry a few times. A reply
        that turns up after we gave up waiting for it would otherwise be read
        as the reply to the next command, so discard replies that don't
        contain everything in expect.
        """
        for attempt in range(1, META_ATTEMPTS + 1):
            await self.meta.send(command.encode())
            with trio.move_on_after(META_TIMEOUT):
                while True:
                    raw = await self.meta.recv()
                    reply = parse_reply(raw)
                    if expect.items() <= reply.items():
                        log.log(TRACE, "%s reply: %r", describe, raw)
                        return reply
                    log.debug("%s: discarding unexpected reply %r", describe, reply)
            log.debug(
                "%s: no reply after %.1fs (attempt %d/%d)",
                describe,
                META_TIMEOUT,
                attempt,
                META_ATTEMPTS,
            )
        log.warning("%s: no reply, giving up", describe)
        return None

    async def query_battery(self) -> float | None:
        reply = await self._request("type=1001\n", {"type": "2001"}, "battery")
        return None if reply is None else get_battery_level(reply)

    async def set_brightness(self, level: int) -> int | None:
        reply = await self._request(
            f"type=1003&value={level}\n",
            # A late reply to an earlier brightness change has the same type.
            {"type": "1003", "value": str(level)},
            "brightness",
        )
        if reply is None:
            return None
        try:
            return int(reply["value"])
        except (KeyError, ValueError):
            log.warning("failed to parse brightness reply: %r", reply)
            return None

    async def get_system_info(self) -> dict[str, str] | None:
        return await self._request("type=1002\n", {"type": "2002"}, "system info")

    async def start_video(self) -> None:
        # Sent three times in the captured traffic. Once usually works, but
        # repeating it covers a lost datagram.
        data = "\x20\x36\x00\x02".encode()
        for _ in range(3):
            await self.vid.send(data)
            await trio.sleep(0.05)

    async def stop_video(self) -> None:
        data = "\x20\x37".encode()
        await self.vid.send(data)

    async def recv_video(self) -> bytes:
        return await self.vid.recv()

    async def aclose(self) -> None:
        await self.meta.aclose()
        await self.vid.aclose()


def parse_reply(reply: bytes) -> dict[str, str]:
    """Parse a meta reply like b'type=2002&w=640&h=480\\n' into a dict.

    The device sends a fixed-size buffer with trailing uninitialised bytes
    (often 0xaa, sometimes arbitrary garbage) after the newline.
    """
    line = reply.split(b"\n", 1)[0].split(b"\x00", 1)[0]
    text = line.decode(errors="replace")
    return {k: v[0].strip() for k, v in parse_qs(text).items()}


def describe_system_info(info: dict[str, str] | None) -> str:
    """Summarise the interesting bits of a type=2002 system info reply."""
    if info is None:
        return "unknown"
    keys = ["company", "dn", "hardware", "firmware", "ssid", "w", "h", "fps"]
    return ", ".join(f"{k}={info[k]}" for k in keys if k in info) or repr(info)


def get_battery_level(reply: dict[str, str]) -> float | None:
    """
    Extracts the battery level from a parsed reply like 'type=2001&data=23'.
    Returns an float in the range 0-1, or None if not found or invalid.
    """
    try:
        return int(reply["data"]) / 100
    except (KeyError, ValueError):
        log.warning("failed to extract battery from reply: %r", reply)
        return None


def format_battery(level: float | None) -> str:
    return "unknown" if level is None else f"{level:.0%}"


def draw_battery(
    img: cv2.typing.MatLike,
    x: int,
    y: int,
    width: int,
    height: int,
    level: float,
    thickness: int,
) -> None:
    """
    Draw a battery icon at (x, y) with given width, height and charge level (0 to 1).
    """
    # Clamp level to [0, 1]
    level = max(0, min(level, 1.0))

    # Colors
    border_color = (255, 255, 255)
    fill_color = (0, 255, 0) if level > 0.3 else (0, 0, 255)  # Red if low battery

    # Draw battery outline
    cv2.rectangle(img, (x, y), (x + width, y + height), border_color, thickness)

    # Draw battery tip
    tip_width = int(width * 0.08)
    tip_x = x + width
    tip_y = y + int(height * 0.3)
    tip_height = int(height * 0.4)
    cv2.rectangle(
        img, (tip_x, tip_y), (tip_x + tip_width, tip_y + tip_height), border_color, -1
    )

    # Fill battery level
    fill_width = int((width - 4) * level)
    cv2.rectangle(
        img, (x + 2, y + 2), (x + 2 + fill_width, y + height - 2), fill_color, -1
    )


def absolute_frame_from_raw(raw_frame: int, latest_abs_frame: int) -> int:
    # Find the multiple of 256 that makes raw_frame closest to latest_abs_frame
    base = (latest_abs_frame // 256) * 256
    candidates = [base - 256 + raw_frame, base + raw_frame, base + 256 + raw_frame]
    # pick the candidate closest to latest_abs_frame
    abs_frame = min(candidates, key=lambda x: abs(x - latest_abs_frame))
    return abs_frame


@dataclass
class AssembledFrame:
    frame: int
    data: bytes
    rotation: int
    started_at: float
    partial: bool = False
    prefix_parts: int = 0
    expected_parts: int = 0
    expected_known: bool = True


class FrameAssembler:
    """Reassemble fragmented JPEG frames from 8-byte-header UDP datagrams.

    Each datagram carries one slice of a baseline sequential JPEG (with no
    restart markers). Complete frames are emitted as soon as all parts arrive.
    When a newer frame starts arriving, any older buffered frames will never
    receive more parts; if min_completeness < 1.0 and the newest such frame
    has a contiguous prefix from part 0 meeting that ratio, its prefix is
    emitted as a partial frame instead of being dropped.
    """

    def __init__(self, min_completeness: float = 1.0) -> None:
        self.min_completeness = min_completeness
        self.latest_frame = 0
        self.last_finalized_frame = -1
        self.last_num_parts: int | None = None
        self.frames: dict[int, dict[int, bytes]] = {}
        self.expected_parts: dict[int, int] = {}
        self.frame_rotation: dict[int, int] = {}
        self.frame_start: dict[int, float] = {}

    def _discard(self, frame: int) -> None:
        self.frames.pop(frame, None)
        self.expected_parts.pop(frame, None)
        self.frame_rotation.pop(frame, None)
        self.frame_start.pop(frame, None)

    def _log_dropped(self, frame: int) -> None:
        expected = self.expected_parts.get(frame)
        log.debug(
            "dropped incomplete frame %d: got parts %s of %s",
            frame,
            sorted(self.frames[frame]),
            expected if expected is not None else "?",
        )

    def _try_partial(self, frame: int) -> AssembledFrame | None:
        parts = self.frames[frame]
        prefix_len = 0
        while prefix_len in parts:
            prefix_len += 1
        if prefix_len == 0:
            return None

        known_expected = self.expected_parts.get(frame)
        if known_expected is not None:
            expected = known_expected
        elif self.last_num_parts is not None:
            # Since frame_end=1 was not among `parts`, none of `parts` was the
            # final part (num_parts - 1), so there were at least max(parts) + 2.
            expected = max(self.last_num_parts, max(parts) + 2)
        else:
            return None

        if prefix_len / expected < self.min_completeness:
            return None

        return AssembledFrame(
            frame=frame,
            data=b"".join(parts[i] for i in range(prefix_len)),
            rotation=self.frame_rotation[frame],
            started_at=self.frame_start[frame],
            partial=True,
            prefix_parts=prefix_len,
            expected_parts=expected,
            expected_known=known_expected is not None,
        )

    def push(self, packet: bytes, now: float) -> AssembledFrame | None:
        if len(packet) <= 8:
            return None

        raw_frame = packet[0]
        frame_end = packet[1]
        part = packet[2]
        part_end = packet[3]
        rotation = int.from_bytes(packet[4:6], "big")
        pic_data = packet[8:]

        frame = absolute_frame_from_raw(raw_frame, self.latest_frame)
        log.log(
            TRACE,
            "raw_frame=%d, frame=%d, frame_end=%d, part=%d, part_end=%d",
            raw_frame,
            frame,
            frame_end,
            part,
            part_end,
        )

        if frame <= self.last_finalized_frame:
            log.log(TRACE, "ignoring late part %d for finalized frame %d", part, frame)
            return None

        if frame > self.latest_frame:
            self.latest_frame = frame

        if frame not in self.frames:
            self.frames[frame] = {}
            self.frame_start[frame] = now
        self.frames[frame][part] = pic_data
        self.frame_rotation[frame] = rotation

        if frame_end == 1 and part_end > 0:
            self.expected_parts[frame] = part_end
            self.last_num_parts = part_end

        if frame in self.expected_parts:
            num_parts = self.expected_parts[frame]
            parts = self.frames[frame]
            if all(p in parts for p in range(num_parts)):
                assembled = AssembledFrame(
                    frame=frame,
                    data=b"".join(parts[i] for i in range(num_parts)),
                    rotation=self.frame_rotation[frame],
                    started_at=self.frame_start[frame],
                    partial=False,
                    prefix_parts=num_parts,
                    expected_parts=num_parts,
                    expected_known=True,
                )
                for f in sorted(k for k in self.frames if k < frame):
                    self._log_dropped(f)
                    self._discard(f)
                self._discard(frame)
                self.last_finalized_frame = frame
                return assembled

        older = sorted(f for f in self.frames if f < frame)
        if not older:
            return None

        chosen: AssembledFrame | None = None
        if self.min_completeness < 1.0:
            for f in reversed(older):
                chosen = self._try_partial(f)
                if chosen is not None:
                    break

        for f in older:
            if chosen is None or f != chosen.frame:
                self._log_dropped(f)
            self._discard(f)

        self.last_finalized_frame = max(self.last_finalized_frame, frame - 1)
        return chosen


def decode_jpeg(
    pic_buf: bytes,
    partial: bool = False,
    prev_image_cv: cv2.typing.MatLike | None = None,
) -> tuple[cv2.typing.MatLike, int]:
    """Decode a complete or truncated JPEG buffer into a BGR image.

    Returns (image_cv, valid_rows). For a partial JPEG, composites the intact
    top rows onto prev_image_cv (or black if there is no previous frame yet)
    so neither the truncated MCU row nor libjpeg's grey fill is shown.
    """
    if partial:
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            image = Image.open(BytesIO(pic_buf))
            if image.mode != "RGB":
                image = image.convert("RGB")
            image_np = np.array(image)
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = False
    else:
        image = Image.open(BytesIO(pic_buf))
        if image.mode != "RGB":
            image = image.convert("RGB")
        image_np = np.array(image)

    image_cv = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)
    num_rows = image_cv.shape[0]
    if not partial:
        return image_cv, num_rows

    last_non_grey = -1
    for r in range(num_rows - 1, -1, -1):
        if np.any(image_np[r] != 128):
            last_non_grey = r
            break

    valid_rows = max(0, last_non_grey - TRUNCATED_ROW_MARGIN)
    if valid_rows == 0:
        raise OSError("no complete scanlines in partial JPEG")

    if prev_image_cv is not None and prev_image_cv.shape == image_cv.shape:
        composited = prev_image_cv.copy()
        composited[:valid_rows] = image_cv[:valid_rows]
        image_cv = composited
    else:
        image_cv[valid_rows:] = 0

    return image_cv, valid_rows


def render_image(
    image_cv: cv2.typing.MatLike, fullframe: bool, rotation: int
) -> tuple[cv2.typing.MatLike, int]:
    """Render a decoded BGR frame, applying mask, rotation, and cropping or padding."""
    num_rows, num_cols = image_cv.shape[:2]

    if not fullframe:
        # Case 1: Masked circle. The window will be a square of the SHORTER dimension.
        square_size = min(num_rows, num_cols)

        # Create a circular mask on the original image dimensions
        mask = np.zeros((num_rows, num_cols), np.uint8)
        cv2.circle(
            mask,
            (num_cols // 2, num_rows // 2),
            square_size // 2,
            255,
            -1,
        )
        image_masked = cv2.bitwise_and(image_cv, image_cv, mask=mask)

        # Get rotation matrix for the original image
        rotation_matrix = cv2.getRotationMatrix2D(
            (num_cols / 2, num_rows / 2), rotation + 90, 1
        )
        # Rotate the masked image within its original frame
        image_rotated = cv2.warpAffine(
            image_masked, rotation_matrix, (num_cols, num_rows)
        )

        # Crop the center square from the rotated image
        center_x, center_y = num_cols // 2, num_rows // 2
        half_size = square_size // 2
        image_to_show = image_rotated[
            center_y - half_size : center_y + half_size,
            center_x - half_size : center_x + half_size,
        ]

    else:
        # Case 2: Full frame, ensuring no corners are ever cropped.
        # The window will be a square with side length equal to the image diagonal.

        # Calculate the length of the image diagonal
        diagonal = np.sqrt(num_cols**2 + num_rows**2)

        # The new square size is the diagonal, rounded up to the nearest integer
        square_size = int(np.ceil(diagonal))

        # Get the rotation matrix centered on the original image
        rotation_matrix = cv2.getRotationMatrix2D(
            (num_cols / 2, num_rows / 2), rotation + 90, 1
        )

        # Adjust the matrix's translation component to center the image on the new, larger canvas
        tx = (square_size - num_cols) / 2
        ty = (square_size - num_rows) / 2
        rotation_matrix[0, 2] += tx
        rotation_matrix[1, 2] += ty

        # Warp the original image onto the new square canvas
        image_to_show = cv2.warpAffine(
            image_cv,
            rotation_matrix,
            (square_size, square_size),
        )

    log.log(
        TRACE,
        "image %dx%d, using window %dx%d",
        num_rows,
        num_cols,
        square_size,
        square_size,
    )

    return image_to_show, square_size


BATTERY_POLL_INTERVAL = 10.0


@dataclass
class DeviceState:
    """Device state shared between the video loop and meta_task.

    Trio runs everything on one thread, so no locking is needed.
    """

    battery_level: float | None = None


async def initial_queries(conn: EndscopeConnection, state: DeviceState) -> None:
    log.info("Device: %s", describe_system_info(await conn.get_system_info()))

    state.battery_level = await conn.query_battery()
    log.info("Battery: %s", format_battery(state.battery_level))

    # set led brightness to 100%
    log.info("Brightness: %s", await conn.set_brightness(100))


async def meta_task(
    conn: EndscopeConnection,
    state: DeviceState,
    brightness_rx: trio.MemoryReceiveChannel[int],
) -> None:
    """Handle all meta traffic, so the video loop never waits on the device.

    Polls the battery periodically (which also keeps the device awake), and
    applies brightness changes requested by the video loop.
    """
    next_poll = trio.current_time() + BATTERY_POLL_INTERVAL
    while True:
        level: int | None = None
        with trio.move_on_at(next_poll):
            level = await brightness_rx.receive()
            # Several key presses may have queued up; only the latest matters.
            while True:
                try:
                    level = brightness_rx.receive_nowait()
                except trio.WouldBlock:
                    break

        if level is not None:
            log.info("Brightness: %s", await conn.set_brightness(level))
        else:
            next_poll = trio.current_time() + BATTERY_POLL_INTERVAL
            battery_level = await conn.query_battery()
            # On failure keep showing the last known level.
            if battery_level is not None and battery_level != state.battery_level:
                state.battery_level = battery_level
                log.info("Battery: %s", format_battery(battery_level))


async def video_loop(
    conn: EndscopeConnection,
    state: DeviceState,
    brightness_tx: trio.MemorySendChannel[int],
    headless: bool = False,
    duration: float | None = None,
    min_completeness: float = 1.0,
) -> int:
    brightness = 100
    win_name = "Video Stream"
    firstframe = True
    frames_shown = 0
    deadline = math.inf if duration is None else (trio.current_time() + duration)
    partial_threshold = (
        min_completeness if min_completeness < 1.0 else DEFAULT_PARTIAL_COMPLETENESS
    )

    try:
        if not headless:
            cv2.namedWindow(win_name, flags=cv2.WINDOW_GUI_NORMAL)

        # Build help image once
        help_lines = [
            "Keyboard shortcuts:",
            "",
            "  1/2/3/4  Lock rotation 0/90/180/270",
            "  r        Unlock rotation (use sensor)",
            "  +/-      Brightness up/down 10%",
            "  f        Toggle full frame / circle",
            "  p        Toggle partial frames",
            "  w        Save snapshot (timestamped .jpg)",
            "  d        Toggle debug output",
            "  h        Toggle this help",
            "  q / Esc  Quit",
            "",
            "Click either window to toggle this help.",
        ]
        help_font = cv2.FONT_HERSHEY_DUPLEX
        help_scale = 0.7
        help_thickness = 1
        help_padding = 20
        help_line_h = 32
        help_img_h = help_line_h * len(help_lines) + help_padding * 2
        help_img_w = 560
        help_img = np.zeros((help_img_h, help_img_w, 3), dtype=np.uint8)
        for i, line in enumerate(help_lines):
            color = (255, 255, 255) if i == 0 else (180, 180, 180)
            cv2.putText(
                help_img,
                line,
                (help_padding, help_padding + 20 + i * help_line_h),
                help_font,
                help_scale,
                color,
                help_thickness,
                cv2.LINE_AA,
            )
        help_win_base = "Help"
        help_win = help_win_base
        help_win_counter = 0
        help_visible = False

        # Mouse callback: any click toggles help window
        mouse_clicked = [False]

        def on_mouse(event, x, y, flags, param):
            if event == cv2.EVENT_LBUTTONUP:
                mouse_clicked[0] = True

        if not headless:
            cv2.setMouseCallback(win_name, on_mouse)

        rotation_lock = False
        rotation = 0
        fullframe = False

        prev_image_cv: cv2.typing.MatLike | None = None
        last_complete_buf = b""
        last_frame_partial = False
        assembler = FrameAssembler(min_completeness=min_completeness)
        last_shown_time: float | None = None

        frame_shown = False
        next_ui_poll = trio.current_time()
        last_packet_time = trio.current_time()

        while True:
            if trio.current_time() >= deadline:
                break
            try:
                # Process UI events (e.g. window closing) and poll for a
                # keypress right after showing a frame (imshow needs it to
                # draw), and also on a timer, so the UI stays responsive even
                # while frames are being dropped.
                if frame_shown or trio.current_time() >= next_ui_poll:
                    frame_shown = False
                    next_ui_poll = trio.current_time() + UI_POLL_INTERVAL
                    key = -1 if headless else (cv2.pollKey() & 0xFF)

                    # Toggle help window on mouse click
                    if mouse_clicked[0]:
                        mouse_clicked[0] = False

                        # Sync help_visible just-in-time in case user closed help via window chrome
                        if help_visible and _is_window_closed(help_win):
                            help_visible = False

                        if help_visible:
                            try:
                                cv2.destroyWindow(help_win)
                            except cv2.error:
                                pass
                            help_visible = False
                        else:
                            # Use a unique name for the help window to avoid issues when
                            # closing the window natively on the GTK backend. If we quickly
                            # relaunch, or query _is_window_closed, while the backend is
                            # still asynchronously tearing down the window, we can segfault.
                            help_win_counter += 1
                            help_win = f"{help_win_base}_{help_win_counter}"
                            cv2.namedWindow(help_win, flags=cv2.WINDOW_GUI_NORMAL)
                            cv2.setWindowTitle(help_win, "Help")
                            cv2.imshow(help_win, help_img)
                            cv2.setMouseCallback(help_win, on_mouse)
                            help_visible = True

                    if key == ord("1"):
                        rotation_lock = True
                        rotation = 0
                    elif key == ord("2"):
                        rotation_lock = True
                        rotation = 90
                    elif key == ord("3"):
                        rotation_lock = True
                        rotation = 180
                    elif key == ord("4"):
                        rotation_lock = True
                        rotation = 270
                    elif key == ord("r"):
                        rotation_lock = False
                    elif key == ord("q") or key == 27:
                        log.info("user quit")
                        break
                    elif key == ord("w") and prev_image_cv is not None:
                        if last_frame_partial:
                            ok, encoded = cv2.imencode(".jpg", prev_image_cv)
                            snap_bytes = encoded.tobytes() if ok else b""
                        else:
                            snap_bytes = last_complete_buf
                        if snap_bytes:
                            now = datetime.now()
                            filename = (
                                now.strftime("snapshot_%Y%m%d_%H%M%S_")
                                + f"{now.microsecond // 10000:02d}.jpg"
                            )
                            with open(filename, "wb") as fd:
                                ret = fd.write(snap_bytes)
                            log.info("Wrote %d bytes to %s", ret, filename)
                    elif key == ord("+"):
                        if brightness < 100:
                            brightness += 10
                            brightness_tx.send_nowait(brightness)
                    elif key == ord("-"):
                        if brightness > 0:
                            brightness -= 10
                            brightness_tx.send_nowait(brightness)
                    elif key == ord("f"):
                        fullframe = not fullframe
                    elif key == ord("p"):
                        if assembler.min_completeness < 1.0:
                            assembler.min_completeness = 1.0
                            log.info("partial frames off")
                        else:
                            assembler.min_completeness = partial_threshold
                            log.info(
                                "partial frames on (min completeness %.0f%%)",
                                assembler.min_completeness * 100,
                            )
                    elif key == ord("d"):
                        if log.isEnabledFor(logging.DEBUG):
                            log.info("debug logging off")
                            log.setLevel(logging.INFO)
                        else:
                            log.setLevel(logging.DEBUG)
                            log.info("debug logging on")
                    elif key == ord("h"):
                        mouse_clicked[0] = True  # reuse toggle logic

                # read video stream, but come back in time for the next UI poll
                reply = None
                with trio.move_on_at(min(next_ui_poll, deadline)):
                    reply = await conn.recv_video()

                if reply is None:
                    if trio.current_time() >= deadline:
                        break
                    if trio.current_time() - last_packet_time > VIDEO_TIMEOUT:
                        log.warning("Video timeout")
                        break
                    continue
                last_packet_time = trio.current_time()

                assembled = assembler.push(reply, last_packet_time)
                if assembled is None:
                    continue

                if not rotation_lock:
                    rotation = assembled.rotation

                try:
                    image_cv, valid_rows = decode_jpeg(
                        assembled.data,
                        partial=assembled.partial,
                        prev_image_cv=prev_image_cv,
                    )
                    prev_image_cv = image_cv
                    if not assembled.partial:
                        last_complete_buf = assembled.data
                    last_frame_partial = assembled.partial

                    if assembled.partial:
                        exp_str = (
                            f"{assembled.expected_parts}"
                            if assembled.expected_known
                            else f"~{assembled.expected_parts}"
                        )
                        log.debug(
                            "showing partial frame %d: prefix %d/%s parts, %d/%d rows",
                            assembled.frame,
                            assembled.prefix_parts,
                            exp_str,
                            valid_rows,
                            image_cv.shape[0],
                        )

                    image_to_show, square_size = render_image(
                        image_cv, fullframe, rotation
                    )

                    battery_level = state.battery_level
                    if battery_level is not None:
                        draw_battery(
                            image_to_show,
                            x=square_size // 100,
                            y=square_size // 100,
                            width=square_size // 10,
                            height=square_size // 20,
                            level=battery_level,
                            thickness=square_size // 200,
                        )
                    # Fit image_to_show into the current window size, preserving aspect ratio.
                    # On macOS (WINDOW_GUI_NORMAL), the backend already preserves aspect ratio
                    # on window resize, and getWindowImageRect always returns the native image
                    # size — so this block is a no-op there.
                    # On Windows, the backend stretches the image to fill the window, and
                    # getWindowImageRect reflects the actual stretched display dimensions —
                    # so we resize to fit the smaller dimension and pad the rest with black.
                    # On Linux, at least Wayland, the backend also preserves aspect ratio, but
                    # resizing is async so this block is better off skipped.
                    if not headless:
                        if sys.platform == "win32":
                            rect = cv2.getWindowImageRect(win_name)
                            win_w, win_h = rect[2], rect[3]
                            if win_w > 0 and win_h > 0:
                                fit = min(win_w, win_h)
                                if fit != square_size:
                                    image_to_show = cv2.resize(
                                        image_to_show,
                                        (fit, fit),
                                        interpolation=cv2.INTER_LINEAR,
                                    )
                                if win_w != win_h:
                                    # Pad the shorter axis with black to fill the window
                                    pad_w = win_w - fit
                                    pad_h = win_h - fit
                                    image_to_show = cv2.copyMakeBorder(
                                        image_to_show,
                                        pad_h // 2,
                                        pad_h - pad_h // 2,
                                        pad_w // 2,
                                        pad_w - pad_w // 2,
                                        cv2.BORDER_CONSTANT,
                                        value=(0, 0, 0),
                                    )
                        # Bail before imshow silently recreates a destroyed
                        # window (GTK/Wayland backends don't raise on this).
                        # Not race-safe.
                        if _is_window_closed(win_name):
                            log.info("window closed")
                            break

                        cv2.imshow(win_name, image_to_show)
                        if firstframe:
                            cv2.resizeWindow(win_name, square_size, square_size)
                            firstframe = False
                        frame_shown = True
                    frames_shown += 1

                    shown_at = trio.current_time()
                    if last_shown_time is not None:
                        interval = shown_at - last_shown_time
                        log.log(
                            TRACE,
                            "frame %d shown: %.0f ms since last, %.0f ms since first part",
                            assembled.frame,
                            interval * 1000,
                            (shown_at - assembled.started_at) * 1000,
                        )
                        if interval > STALL_THRESHOLD:
                            log.debug(
                                "stall: %.0f ms since last frame",
                                interval * 1000,
                            )
                    last_shown_time = shown_at

                except OSError:
                    log.debug(
                        "%sframe %d: failed to decode JPEG",
                        "partial " if assembled.partial else "",
                        assembled.frame,
                    )

            except cv2.error as e:
                # Any cv2 window call (imshow, getWindowProperty, resizeWindow,
                # destroyWindow) can raise once the user destroys the window.
                # If the helper confirms the main window is gone, exit cleanly;
                # otherwise re-raise so genuine bugs still surface.
                # Print the error either way so an unrelated cv2.error that
                # happens to coincide with a destroyed window is still visible.
                log.warning("cv2.error: %s", e)
                if not headless and _is_window_closed(win_name):
                    log.info("window closed")
                    break
                raise

        return frames_shown
    finally:
        if not headless:
            cv2.destroyAllWindows()


async def run_app(
    conn: EndscopeConnection,
    buffer_size: int,
    headless: bool = False,
    duration: float | None = None,
    min_completeness: float = 1.0,
) -> int:
    state = DeviceState()
    brightness_tx, brightness_rx = trio.open_memory_channel[int](math.inf)
    frames_shown = 0
    try:
        await conn.start_video()
        await initial_queries(conn, state)

        async with trio.open_nursery() as nursery:
            nursery.start_soon(meta_task, conn, state, brightness_rx)
            frames_shown = await video_loop(
                conn,
                state,
                brightness_tx,
                headless=headless,
                duration=duration,
                min_completeness=min_completeness,
            )
            # Cancel meta_task before closing the connection under it.
            nursery.cancel_scope.cancel()
    finally:
        # stop stream and close
        await conn.stop_video()
        await conn.aclose()
    return frames_shown


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="Use fake endscope device")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without creating OpenCV windows",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        metavar="SEC",
        help="Exit cleanly after SEC seconds",
    )
    parser.add_argument(
        "--min-completeness",
        type=float,
        nargs="?",
        const=DEFAULT_PARTIAL_COMPLETENESS,
        default=1.0,
        metavar="RATIO",
        help=(
            "Render incomplete frames with a contiguous prefix of at least RATIO "
            f"(0-1; {DEFAULT_PARTIAL_COMPLETENESS} if omitted, 1.0 to disable)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log timeouts, retries, dropped frames and stalls",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Log every packet and per-frame timing (very verbose)",
    )
    fake_group = parser.add_argument_group("fake device network simulation")
    fake_group.add_argument(
        "--fake-loss",
        type=float,
        default=0.0,
        metavar="P",
        help="Probability (0-1) of dropping each datagram, in each direction",
    )
    fake_group.add_argument(
        "--fake-latency",
        type=float,
        default=0.0,
        metavar="MS",
        help="Delay each datagram by this many milliseconds",
    )
    fake_group.add_argument(
        "--fake-jitter",
        type=float,
        default=0.0,
        metavar="MS",
        help="Add a random extra delay of up to this many milliseconds",
    )
    args = parser.parse_args()
    if not (0.0 < args.min_completeness <= 1.0):
        parser.error(
            "--min-completeness must be between 0 (exclusive) and 1 (inclusive)"
        )

    buffer_size = 1500
    target_ip = "192.168.1.1"
    target_port_meta = 61502
    source_port_meta = 50262
    target_port_vid = 61503
    source_port_vid = 51320

    setup_logging(
        TRACE if args.trace else logging.DEBUG if args.debug else logging.INFO
    )

    frames_shown = 0
    async with trio.open_nursery() as nursery:
        if args.fake:
            from fake_endscope import start_fake_device

            meta_chan, vid_chan = start_fake_device(
                nursery,
                loss=args.fake_loss,
                latency=args.fake_latency / 1000,
                jitter=args.fake_jitter / 1000,
            )

            conn = EndscopeConnection(meta_chan, vid_chan)

            frames_shown = await run_app(
                conn,
                buffer_size,
                headless=args.headless,
                duration=args.duration,
                min_completeness=args.min_completeness,
            )
            nursery.cancel_scope.cancel()
        else:
            async with (
                UdpDatagramTransport(
                    source_port_meta, target_ip, target_port_meta, buffer_size
                ) as meta_chan,
                UdpDatagramTransport(
                    source_port_vid, target_ip, target_port_vid, buffer_size
                ) as vid_chan,
            ):
                conn = EndscopeConnection(meta_chan, vid_chan)
                frames_shown = await run_app(
                    conn,
                    buffer_size,
                    headless=args.headless,
                    duration=args.duration,
                    min_completeness=args.min_completeness,
                )

    if args.headless:
        log.debug("rendered %d frames", frames_shown)
        if frames_shown == 0:
            log.error("no frames were rendered")
            sys.exit(1)


def cli_main() -> None:
    trio.run(main)


if __name__ == "__main__":
    cli_main()
