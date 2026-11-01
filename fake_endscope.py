import logging
import math
import random
import trio
import cv2
import numpy as np
from PIL import Image
from io import BytesIO
from urllib.parse import parse_qs
from transports import MemoryDatagramTransport

log = logging.getLogger("endscope.fake")
TRACE = 5  # matches endscopetool.TRACE


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
            # modelled on a real reply
            reply = (
                "type=2002&protocol=2&w=640&h=480&fps=20&ratio=4:3&angle=270"
                "&hardware=fake&company=fake&id=0&firmware=fake"
                "&ssid=softish-fake&dn=fake&bl=30\x0a"
            ).encode()
            if reply_channel:
                await reply_channel.send(reply)
        elif cmd_str.startswith("type=1003"):  # brightness
            try:
                params = parse_qs(cmd_str)
                val_str = params["value"][0]
                self.brightness = int(val_str)
                # real device pads 1-3 char value to 4 using \n, and appends load of aa bytes
                if reply_channel:
                    val_padded = val_str + "\n" * (4 - len(val_str))
                    reply = f"type=1003&value={val_padded}".encode() + (b"\xaa" * 32)
                    await reply_channel.send(reply)
            except (KeyError, IndexError, ValueError):
                pass
        elif data == b"\x20\x36\x00\x02":  # start video
            log.debug("fake: video started")
            self.is_video_running = True
        elif data == b"\x20\x37":  # stop video
            log.debug("fake: video stopped")
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


# Roughly how many ~1300 byte datagrams fit in a default Linux UDP receive buffer.
UDP_BUFFER_DATAGRAMS = 128


class UdpLikeLink:
    """One direction of a simulated UDP link.

    Unlike a plain memory channel, sending never blocks: datagrams may be
    dropped at random, delayed (and so reordered), or dropped because the
    receiver isn't keeping up and its buffer is full.
    """

    def __init__(
        self,
        nursery: trio.Nursery,
        name: str,
        loss: float,
        latency: float,
        jitter: float,
    ):
        self.nursery = nursery
        self.name = name
        self.loss = loss
        self.latency = latency
        self.jitter = jitter
        self.send_channel: trio.MemorySendChannel[bytes]
        self.receive_channel: trio.MemoryReceiveChannel[bytes]
        self.send_channel, self.receive_channel = trio.open_memory_channel(
            UDP_BUFFER_DATAGRAMS
        )

    async def send(self, data: bytes) -> None:
        if random.random() < self.loss:
            log.log(TRACE, "fake: %s: dropped %d byte datagram", self.name, len(data))
            return
        delay = self.latency + random.uniform(0, self.jitter)
        if delay > 0:
            self.nursery.start_soon(self._deliver_later, data, delay)
        else:
            self._deliver(data)

    async def _deliver_later(self, data: bytes, delay: float) -> None:
        await trio.sleep(delay)
        self._deliver(data)

    def _deliver(self, data: bytes) -> None:
        try:
            self.send_channel.send_nowait(data)
        except trio.WouldBlock:
            log.debug("fake: %s: receive buffer full, dropped datagram", self.name)
        except (trio.ClosedResourceError, trio.BrokenResourceError):
            pass

    async def aclose(self) -> None:
        await self.send_channel.aclose()


def start_fake_device(
    nursery: trio.Nursery,
    loss: float = 0.0,
    latency: float = 0.0,
    jitter: float = 0.0,
) -> tuple[
    MemoryDatagramTransport,
    MemoryDatagramTransport,
]:
    """
    Creates simulated links and starts the fake device task in the given nursery.
    Returns the transports needed by the application (meta_transport, vid_transport).

    loss is the probability of dropping each datagram, in each direction.
    latency and jitter (seconds) delay each datagram by latency + U(0, jitter).
    """

    def link(name: str) -> UdpLikeLink:
        return UdpLikeLink(nursery, name, loss, latency, jitter)

    meta_cmd = link("meta cmd")
    meta_reply = link("meta reply")
    vid_cmd = link("vid cmd")
    vid_data = link("vid data")

    nursery.start_soon(
        run_fake_device,
        meta_cmd.receive_channel,
        meta_reply,
        vid_cmd.receive_channel,
        vid_data,
    )

    meta_transport = MemoryDatagramTransport(meta_cmd, meta_reply.receive_channel)
    vid_transport = MemoryDatagramTransport(vid_cmd, vid_data.receive_channel)

    return meta_transport, vid_transport
