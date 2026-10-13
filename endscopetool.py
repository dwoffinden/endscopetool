#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
#
# Python implementation of the endscopetool (sic!) Android application used for the Vitcoco ear wax remover camera thingy.
#
# Original version released at https://gist.github.com/RaphaelWimmer/5bcb286414e6cd38ed38724f9a6a6129
# under CC0 / Public Domain (0) 2023 Raphael Wimmer.
# Contributions by https://github.com/Aghei2 and https://gist.github.com/jamaggs.
#
# v0.1.0
# reverse-engineered using a packet capture log - this means that I have no idea what all those magic numbers mean
# and whether there are further features that might be supported by the hardware
# usage: first connect to the 'softish-XXXX' wifi, then run this script. Check code for keyboard shortcuts.

import cv2
import logging
import sys
import numpy as np
import trio
import argparse
from datetime import datetime
from PIL import Image
from io import BytesIO
from urllib.parse import parse_qs
from transports import AsyncDatagramTransport, UdpDatagramTransport

cv2.setNumThreads(1)

TRACE = 5
logging.addLevelName(TRACE, "TRACE")

log = logging.getLogger("endscope")

# Gaps between displayed frames longer than this are logged at DEBUG.
STALL_THRESHOLD = 0.25


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

    async def query_battery(self) -> float | None:
        data: bytes = "type=1001\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        received_data: str = reply.decode()
        return get_battery_level(received_data)

    async def set_brightness(self, level: int) -> int | None:
        data = f"type=1003&value={level}\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        log.log(TRACE, "brightness response: %s", reply.hex(" "))
        try:
            params = parse_qs(reply.rstrip(b"\xaa").decode())
            return int(params["value"][0])
        except (KeyError, IndexError, ValueError):
            log.warning("failed to parse brightness response: %r", reply)
            return None

    async def get_system_info(self) -> dict[str, str]:
        data: bytes = "type=1002\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        return parse_reply(reply)

    async def start_video(self) -> None:
        # three times according to captured traffic, but works fine sent once?
        data = "\x20\x36\x00\x02".encode()
        await self.vid.send(data)

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

    The device pads some replies with trailing newlines and 0xaa bytes.
    """
    text = reply.rstrip(b"\xaa").decode(errors="replace")
    return {k: v[0].strip() for k, v in parse_qs(text).items()}


def describe_system_info(info: dict[str, str]) -> str:
    """Summarise the interesting bits of a type=2002 system info reply."""
    keys = ["company", "dn", "hardware", "firmware", "ssid", "w", "h", "fps"]
    return ", ".join(f"{k}={info[k]}" for k in keys if k in info) or repr(info)


def get_battery_level(query_string: str) -> float | None:
    """
    Extracts the battery level from a string like 'type=2001&data=23'.
    Returns an float in the range 0-1, or None if not found or invalid.
    """
    try:
        params = parse_qs(query_string)
        return int(params["data"][0]) / 100
    except (KeyError, IndexError, ValueError):
        log.warning("failed to extract battery from data: %r", query_string)
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


def render_image(
    pic_buf: bytes, fullframe: bool, rotation: int
) -> tuple[cv2.typing.MatLike, int]:
    """Render the image buffer, applying mask, rotation, and cropping or padding."""
    image = Image.open(BytesIO(pic_buf))
    image_np = np.array(image)
    image_cv = cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR)
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


async def run_app(conn: EndscopeConnection, buffer_size: int) -> None:
    brightness = 100
    win_name = "Video Stream"
    firstframe = True

    try:
        # get system info
        log.info("Device: %s", describe_system_info(await conn.get_system_info()))

        battery_level: float | None = await conn.query_battery()
        log.info("Battery: %s", format_battery(battery_level))

        # three times according to captured traffic
        await conn.start_video()

        # set led brightness to 100%
        log.info("Brightness: %s", await conn.set_brightness(100))

        cv2.namedWindow(win_name, flags=cv2.WINDOW_GUI_NORMAL)

        # Build help image once
        help_lines = [
            "Keyboard shortcuts:",
            "",
            "  1/2/3/4  Lock rotation 0/90/180/270",
            "  r        Unlock rotation (use sensor)",
            "  +/-      Brightness up/down 10%",
            "  f        Toggle full frame / circle",
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

        cv2.setMouseCallback(win_name, on_mouse)

        rotation_lock = False
        rotation = 0
        fullframe = False

        raw_frame = 0
        frame = 0
        part = 0
        pic_buf = b""
        keep_awake_time = trio.current_time()

        # Store received parts per frame
        # frame_number -> {part_number: pic_data}
        frames_dict: dict[int, dict[int, bytes]] = {}
        # number of parts required per frame
        parts_dict: dict[int, int] = {}
        # when the first part of each frame arrived, for timing logs
        frame_start: dict[int, float] = {}
        last_shown_time: float | None = None

        while True:
            try:
                # read video stream
                with trio.move_on_after(5.0) as cancel_scope:
                    reply = await conn.recv_video()

                if cancel_scope.cancelled_caught:
                    log.warning("Video timeout")
                    break

                raw_frame = reply[0]
                frame_end: int = reply[1]
                part = reply[2]
                part_end: int = reply[3]
                # misc_data = reply[4:8]
                if not rotation_lock:
                    rotation = int.from_bytes(reply[4:6], "big")
                pic_data = reply[8:]

                frame = absolute_frame_from_raw(raw_frame, frame)

                # store the part
                if frame not in frames_dict:
                    frames_dict[frame] = {}
                    frame_start[frame] = trio.current_time()
                frames_dict[frame][part] = pic_data

                log.log(
                    TRACE,
                    "raw_frame=%d, frame=%d, frame_end=%d, part=%d, part_end=%d",
                    raw_frame,
                    frame,
                    frame_end,
                    part,
                    part_end,
                )

                # find number of parts required
                if frame_end == 1:
                    parts_dict[frame] = part_end

                if frame in parts_dict:
                    num_parts = parts_dict[frame]
                    parts = frames_dict[frame]
                    if all(p in parts for p in range(num_parts)):
                        pic_buf = b"".join(parts[i] for i in range(num_parts))

                        try:
                            image_to_show, square_size = render_image(
                                pic_buf, fullframe, rotation
                            )

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

                            shown_at = trio.current_time()
                            if last_shown_time is not None:
                                interval = shown_at - last_shown_time
                                log.log(
                                    TRACE,
                                    "frame %d shown: %.0f ms since last, %.0f ms since first part",
                                    frame,
                                    interval * 1000,
                                    (shown_at - frame_start[frame]) * 1000,
                                )
                                if interval > STALL_THRESHOLD:
                                    log.debug(
                                        "stall: %.0f ms since last frame",
                                        interval * 1000,
                                    )
                            last_shown_time = shown_at

                            for f in frames_dict:
                                if f < frame:
                                    expected = parts_dict.get(f)
                                    log.debug(
                                        "dropped incomplete frame %d: got parts %s of %s",
                                        f,
                                        sorted(frames_dict[f]),
                                        expected if expected is not None else "?",
                                    )

                            # delete earlier frame AND current frame data since we processed it
                            frames_dict = {
                                f: frames_dict[f] for f in frames_dict if f > frame
                            }
                            parts_dict = {
                                f: parts_dict[f] for f in parts_dict if f > frame
                            }
                            frame_start = {
                                f: frame_start[f] for f in frame_start if f > frame
                            }

                            if trio.current_time() > keep_awake_time:
                                keep_awake_time = trio.current_time() + 10
                                prev_battery_level = battery_level
                                battery_level = await conn.query_battery()
                                if prev_battery_level != battery_level:
                                    log.info(
                                        "Battery: %s", format_battery(battery_level)
                                    )

                        except OSError:
                            log.debug("frame %d: failed to decode JPEG", frame)

                        # process UI events (e.g. window closing) and poll for a keypress
                        # we do this only when a frame is completely evaluated to save CPU!
                        key = cv2.pollKey() & 0xFF

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
                        elif key == ord("w"):
                            now = datetime.now()
                            filename = (
                                now.strftime("snapshot_%Y%m%d_%H%M%S_")
                                + f"{now.microsecond // 10000:02d}.jpg"
                            )
                            with open(filename, "wb") as fd:
                                ret = fd.write(pic_buf)
                            log.info("Wrote %d bytes to %s", ret, filename)
                        elif key == ord("+"):
                            if brightness < 100:
                                brightness += 10
                                log.info(
                                    "Brightness: %s",
                                    await conn.set_brightness(brightness),
                                )
                        elif key == ord("-"):
                            if brightness > 0:
                                brightness -= 10
                                log.info(
                                    "Brightness: %s",
                                    await conn.set_brightness(brightness),
                                )
                        elif key == ord("f"):
                            fullframe = not fullframe
                        elif key == ord("d"):
                            if log.isEnabledFor(logging.DEBUG):
                                log.info("debug logging off")
                                log.setLevel(logging.INFO)
                            else:
                                log.setLevel(logging.DEBUG)
                                log.info("debug logging on")
                        elif key == ord("h"):
                            mouse_clicked[0] = True  # reuse toggle logic

            except cv2.error as e:
                # Any cv2 window call (imshow, getWindowProperty, resizeWindow,
                # destroyWindow) can raise once the user destroys the window.
                # If the helper confirms the main window is gone, exit cleanly;
                # otherwise re-raise so genuine bugs still surface.
                # Print the error either way so an unrelated cv2.error that
                # happens to coincide with a destroyed window is still visible.
                log.warning("cv2.error: %s", e)
                if _is_window_closed(win_name):
                    log.info("window closed")
                    break
                raise

    finally:
        # stop stream and close
        await conn.stop_video()
        await conn.aclose()
        cv2.destroyAllWindows()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="Use fake endscope device")
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

    buffer_size = 1500
    target_ip = "192.168.1.1"
    target_port_meta = 61502
    source_port_meta = 50262
    target_port_vid = 61503
    source_port_vid = 51320

    setup_logging(
        TRACE if args.trace else logging.DEBUG if args.debug else logging.INFO
    )

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

            await run_app(conn, buffer_size)
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
                await run_app(conn, buffer_size)


def cli_main() -> None:
    trio.run(main)


if __name__ == "__main__":
    cli_main()
