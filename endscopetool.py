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
import numpy as np
import trio
import argparse
from PIL import Image
from io import BytesIO
from urllib.parse import parse_qs
from typing import Protocol, runtime_checkable

cv2.setNumThreads(1)


@runtime_checkable
class AsyncDatagramChannel(Protocol):
    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes: ...

    async def aclose(self) -> None: ...


class TrioUdpChannel:
    def __init__(
        self, local_port: int, target_ip: str, target_port: int, buffer_size: int = 1500
    ):
        self.local_port = local_port
        self.target_address = (target_ip, target_port)
        self.buffer_size = buffer_size
        self.sock: trio.socket.SocketType | None = None

    async def __aenter__(self):
        import trio.socket

        self.sock = trio.socket.socket(trio.socket.AF_INET, trio.socket.SOCK_DGRAM)
        await self.sock.bind(("0.0.0.0", self.local_port))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def send(self, data: bytes) -> None:
        assert self.sock is not None
        await self.sock.sendto(data, self.target_address)

    async def recv(self) -> bytes:
        assert self.sock is not None
        data, _ = await self.sock.recvfrom(self.buffer_size)
        return data

    async def aclose(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None


class MemoryDatagramChannel:
    def __init__(
        self,
        send_channel: trio.MemorySendChannel,
        receive_channel: trio.MemoryReceiveChannel,
    ):
        self.send_channel = send_channel
        self.receive_channel = receive_channel

    async def send(self, data: bytes) -> None:
        await self.send_channel.send(data)

    async def recv(self) -> bytes:
        return await self.receive_channel.receive()

    async def aclose(self) -> None:
        await self.send_channel.aclose()
        await self.receive_channel.aclose()


class EndscopeConnection:
    def __init__(
        self, meta_channel: AsyncDatagramChannel, vid_channel: AsyncDatagramChannel
    ):
        self.meta = meta_channel
        self.vid = vid_channel

    async def query_battery(self) -> float | None:
        data: bytes = "type=1001\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        received_data: str = reply.decode()
        return get_battery_level(received_data)

    async def set_brightness(self, level: int) -> str:
        data = f"type=1003&value={level}\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        try:
            return reply.decode()
        except UnicodeDecodeError:
            return "UnicodeDecodeError"

    async def get_system_info(self) -> str:
        data: bytes = "type=1002\x0a".encode()
        await self.meta.send(data)
        reply = await self.meta.recv()
        return reply.decode()

    async def start_video(self) -> None:
        # three times according to captured traffic
        data = "\x20\x36\x00\x02".encode()
        for _ in range(3):
            await self.vid.send(data)

    async def stop_video(self) -> None:
        data = "\x20\x37".encode()
        await self.vid.send(data)

    async def recv_video(self) -> bytes:
        return await self.vid.recv()

    async def aclose(self) -> None:
        await self.meta.aclose()
        await self.vid.aclose()


def get_battery_level(query_string: str) -> float | None:
    """
    Extracts the battery level from a string like 'type=2001&data=23'.
    Returns an integer or None if not found or invalid.
    """
    try:
        params = parse_qs(query_string)
        return int(params["data"][0]) / 100
    except (KeyError, IndexError, ValueError):
        print(f"failed to extract battery from data: ${query_string}")
        return None


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


async def run_app(conn: EndscopeConnection, buffer_size: int, debug: bool) -> None:
    brightness = 100
    win_name = "Video Stream"
    firstframe = True

    try:
        # get system info
        received_data = await conn.get_system_info()
        print("Received data:", received_data)

        battery_level: float | None = await conn.query_battery()
        print(f"Battery level: {battery_level}")

        # three times according to captured traffic
        await conn.start_video()

        # set led brightness to 100%
        received_data = await conn.set_brightness(100)
        print("Received data:", received_data)

        cv2.namedWindow(win_name, flags=cv2.WINDOW_GUI_NORMAL)

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

        while True:
            # read video stream
            with trio.move_on_after(5.0) as cancel_scope:
                reply = await conn.recv_video()

            if cancel_scope.cancelled_caught:
                print("Video timeout")
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
            frames_dict[frame][part] = pic_data

            if debug:
                print(
                    f"raw_frame={raw_frame}, frame={frame}, frame_end={frame_end}, part={part}, part_end={part_end}"
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
                            image_masked = cv2.bitwise_and(
                                image_cv, image_cv, mask=mask
                            )

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
                                image_cv, rotation_matrix, (square_size, square_size)
                            )
                        if debug:
                            print(
                                f"image {num_rows}x{num_cols}, using window {square_size}x{square_size}"
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
                        cv2.imshow(win_name, image_to_show)
                        if firstframe:
                            cv2.resizeWindow(win_name, square_size, square_size)
                            firstframe = False

                        # delete earlier frame AND current frame data since we processed it
                        frames_dict = {
                            f: frames_dict[f] for f in frames_dict if f > frame
                        }
                        parts_dict = {f: parts_dict[f] for f in parts_dict if f > frame}

                        if trio.current_time() > keep_awake_time:
                            keep_awake_time = trio.current_time() + 10
                            prev_battery_level = battery_level
                            battery_level = await conn.query_battery()
                            if prev_battery_level != battery_level:
                                print(f"Battery level: {battery_level}")

                    except OSError:
                        print("image corrupted")

                    # process UI events (e.g. window closing) and poll for a keypress
                    # we do this only when a frame is completely evaluated to save CPU!
                    key = cv2.pollKey() & 0xFF
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
                    elif (
                        key == ord("q")
                        or key == 27
                        or cv2.getWindowProperty(win_name, cv2.WND_PROP_AUTOSIZE) == -1
                    ):
                        print("window closed")
                        break
                    elif key == ord("w"):
                        with open("out.jpg", "wb") as fd:
                            ret = fd.write(pic_buf)
                        print("Wrote " + str(ret) + " bytes to out.jpg")
                    elif key == ord("+"):
                        if brightness < 100:
                            brightness += 10
                            received_data = await conn.set_brightness(brightness)
                            print("Received data:", received_data)
                    elif key == ord("-"):
                        if brightness > 0:
                            brightness -= 10
                            received_data = await conn.set_brightness(brightness)
                            print("Received data:", received_data)
                    elif key == ord("f"):
                        fullframe = not fullframe
                    elif key == ord("d"):
                        debug = not debug

    finally:
        # stop stream and close
        await conn.stop_video()
        await conn.aclose()
        cv2.destroyAllWindows()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fake", action="store_true", help="Use fake endscope device")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    buffer_size = 1500
    target_ip = "192.168.1.1"
    target_port_meta = 61502
    source_port_meta = 50262
    target_port_vid = 61503
    source_port_vid = 51320

    async with trio.open_nursery() as nursery:
        if args.fake:
            from fake_endscope import run_fake_device

            # Create memory channels for fake communication
            meta_tx: trio.MemorySendChannel
            meta_rx: trio.MemoryReceiveChannel
            meta_reply_tx: trio.MemorySendChannel
            meta_reply_rx: trio.MemoryReceiveChannel
            vid_tx: trio.MemorySendChannel
            vid_rx: trio.MemoryReceiveChannel
            vid_back_tx: trio.MemorySendChannel
            vid_back_rx: trio.MemoryReceiveChannel

            meta_tx, meta_rx = trio.open_memory_channel(10)
            meta_reply_tx, meta_reply_rx = trio.open_memory_channel(10)
            vid_tx, vid_rx = trio.open_memory_channel(10)
            vid_back_tx, vid_back_rx = trio.open_memory_channel(10)

            # Start fake device task
            nursery.start_soon(
                run_fake_device, meta_rx, meta_reply_tx, vid_back_rx, vid_tx
            )

            # Map to app channels
            meta_chan = MemoryDatagramChannel(meta_tx, meta_reply_rx)
            vid_chan = MemoryDatagramChannel(vid_back_tx, vid_rx)

            conn = EndscopeConnection(meta_chan, vid_chan)
        else:
            async with TrioUdpChannel(
                source_port_meta, target_ip, target_port_meta, buffer_size
            ) as meta_chan:
                async with TrioUdpChannel(
                    source_port_vid, target_ip, target_port_vid, buffer_size
                ) as vid_chan:
                    conn = EndscopeConnection(meta_chan, vid_chan)
                    await run_app(conn, buffer_size, args.debug)
            return

        await run_app(conn, buffer_size, args.debug)
        nursery.cancel_scope.cancel()


def cli_main() -> None:
    trio.run(main)


if __name__ == "__main__":
    cli_main()
