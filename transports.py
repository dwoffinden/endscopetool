import trio
from typing import Protocol, runtime_checkable


@runtime_checkable
class AsyncDatagramTransport(Protocol):
    async def send(self, data: bytes) -> None: ...

    async def recv(self) -> bytes: ...

    async def aclose(self) -> None: ...


class UdpDatagramTransport:
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


class MemoryDatagramTransport:
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
