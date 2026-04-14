import math
import trio
import cv2
import numpy as np
from PIL import Image
from io import BytesIO
from transports import MemoryDatagramTransport


class FakeEndscope:
    def __init__(
        self,
        meta_receive_channel,
        meta_send_channel,
        vid_receive_channel,
        vid_send_channel,
    ):
        self.meta_receive = meta_receive_channel
        self.meta_send = meta_send_channel
        self.vid_receive = vid_receive_channel
        self.vid_send = vid_send_channel
        self.brightness = 100
        self.battery_level = 99
        self.rotation = 0
        self.is_video_running = False

    async def handle_command(self, data: bytes, reply_channel=None):
        cmd_str = ""
        try:
            cmd_str = data.decode()
        except UnicodeDecodeError:
            pass

        if cmd_str.startswith("type=1001"):  # battery
            reply = f"type=2001&data={self.battery_level}\x0a".encode()
            if reply_channel:
                await reply_channel.send(reply)
        elif cmd_str.startswith("type=1002"):  # system info
            reply = "type=2002&version=fake-v1.0\x0a".encode()
            if reply_channel:
                await reply_channel.send(reply)
        elif cmd_str.startswith("type=1003"):  # brightness
            try:
                val = int(cmd_str.split("value=")[1].strip())
                self.brightness = val
                if reply_channel:
                    await reply_channel.send(b"OK\x0a")
            except (IndexError, ValueError):
                pass
        elif data == b"\x20\x36\x00\x02":  # start video
            print("FAKE: Video Started")
            self.is_video_running = True
        elif data == b"\x20\x37":  # stop video
            print("FAKE: Video Stopped")
            self.is_video_running = False

    async def run_meta_listener(self):
        async for data in self.meta_receive:
            await self.handle_command(data, self.meta_send)

    async def run_vid_listener(self):
        async for data in self.vid_receive:
            await self.handle_command(data)

    async def run_video_generator(self):
        raw_frame_id = 0
        fps = 20
        frame_time = 1.0 / fps
        width, height = 640, 480

        while True:
            if not self.is_video_running:
                await trio.sleep(0.1)
                continue

            start_time = trio.current_time()

            # Generate frame: black background with a moving triangle
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            t = trio.current_time()

            # Triangle points rotating
            center_x, center_y = width // 2, height // 2
            size = 100
            angle = t * 2.0  # radians

            pts = []
            for i in range(3):
                a = angle + i * (2 * math.pi / 3)
                x = int(center_x + size * math.cos(a))
                y = int(center_y + size * math.sin(a))
                pts.append([x, y])

            cv2.fillPoly(frame, [np.array(pts)], (0, 255, 0))

            # Add some text
            cv2.putText(
                frame,
                f"FAKE DEVICE - BRIGHTNESS: {self.brightness}%",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (255, 255, 255),
                2,
            )

            # Convert to JPEG
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            buf = BytesIO()
            image.save(buf, format="JPEG", quality=75)
            pic_bytes = buf.getvalue()

            # Segment into datagrams (8 byte header)
            chunk_size = 1300
            parts = [
                pic_bytes[i : i + chunk_size]
                for i in range(0, len(pic_bytes), chunk_size)
            ]
            num_parts = len(parts)

            for i, part_data in enumerate(parts):
                # Header: raw_frame, frame_end, part, part_end, rotation(2), unknown(2)
                header = bytearray(8)
                header[0] = raw_frame_id % 256
                header[1] = 1 if i == num_parts - 1 else 0
                header[2] = i
                header[3] = num_parts if i == num_parts - 1 else 0

                # Rotation (4:6) - send current rotation
                header[4:6] = self.rotation.to_bytes(2, "big")

                payload = header + part_data
                await self.vid_send.send(payload)

            raw_frame_id += 1

            # Wait for next frame
            elapsed = trio.current_time() - start_time
            await trio.sleep(max(0, frame_time - elapsed))

    async def run(self):
        async with trio.open_nursery() as nursery:
            nursery.start_soon(self.run_meta_listener)
            nursery.start_soon(self.run_vid_listener)
            nursery.start_soon(self.run_video_generator)


async def run_fake_device(meta_receive, meta_send, vid_receive, vid_send):
    device = FakeEndscope(meta_receive, meta_send, vid_receive, vid_send)
    await device.run()


def start_fake_device(
    nursery: trio.Nursery,
) -> tuple[
    MemoryDatagramTransport,
    MemoryDatagramTransport,
]:
    """
    Creates memory channels and starts the fake device task in the given nursery.
    Returns the transports needed by the application (meta_transport, vid_transport).
    """
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

    nursery.start_soon(run_fake_device, meta_rx, meta_reply_tx, vid_back_rx, vid_tx)

    meta_transport = MemoryDatagramTransport(meta_tx, meta_reply_rx)
    vid_transport = MemoryDatagramTransport(vid_back_tx, vid_rx)

    return meta_transport, vid_transport
