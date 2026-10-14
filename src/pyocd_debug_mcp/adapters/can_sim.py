"""Local, cross-process Classical CAN simulation over TCP (no physical bus).

Wire format: each peer first sends b'BYOCAN1' + one byte (receive-own: 0/1),
then receives b'BYOCAN1'. Frames are !IBB8s: identifier, extended flag, length,
and zero-padded payload. Broker-to-peer frames prepend a one-byte own-echo
flag (0=RX, 1=TX). TCP preserves order per sender; no CAN arbitration,
bit timing, error frames, or electrical behavior is simulated. A successful
send means local socket submission, not peer delivery or physical TX quiescence.
"""

from __future__ import annotations

import argparse
import select
import socket
import socketserver
import struct
import threading
import time
from dataclasses import dataclass, field, replace
from typing import cast

from pyocd_debug_mcp.adapters.can_interface import (
    CanBackendCapabilities,
    CanBusHandle,
    CanDirection,
    CanFilter,
    CanFrame,
    CapabilityStatus,
)
from pyocd_debug_mcp.can_config import LoadedCanFixtureConfig
from pyocd_debug_mcp.can_errors import (
    CanConnectionError,
    CanOperationError,
    CanPolicyError,
    CanTimeoutError,
)

MAGIC = b"BYOCAN1"
FRAME = struct.Struct("!IBB8s")


def endpoint(channel: str) -> tuple[str, int]:
    host, separator, port_text = channel.rpartition(":")
    if not separator or host != "127.0.0.1" or not port_text.isdecimal():
        raise ValueError("tcp_sim channel must be 127.0.0.1:<port>")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("tcp_sim port must be between 1 and 65535")
    return host, port


def encode(frame: CanFrame) -> bytes:
    if frame.is_fd or frame.is_error or frame.is_remote:
        raise ValueError("tcp_sim supports only Classical CAN data frames")
    return FRAME.pack(frame.arbitration_id, int(frame.extended), len(frame.data), frame.data)


def decode(data: bytes) -> CanFrame:
    identifier, extended, length, payload = FRAME.unpack(data)
    if extended not in (0, 1) or length > 8:
        raise ValueError("invalid Classical CAN simulation frame")
    return CanFrame(identifier, bool(extended), payload[:length])


def _read_exact(sock: socket.socket, length: int) -> bytes:
    result = bytearray()
    while len(result) < length:
        chunk = sock.recv(length - len(result))
        if not chunk:
            raise ConnectionError("CAN simulator disconnected")
        result.extend(chunk)
    return bytes(result)


class _Peer(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = cast(Simulator, self.server)
        sock = cast(socket.socket, self.request)
        sock.settimeout(1.0)
        try:
            greeting = _read_exact(sock, len(MAGIC) + 1)
            if greeting[:-1] != MAGIC or greeting[-1] not in (0, 1):
                return
            with server.peers_lock:
                # Register before acknowledging so clients can transmit as soon
                # as open returns. A single lock gives complete-frame broadcasts.
                server.peers[sock] = bool(greeting[-1])
                sock.sendall(MAGIC)
            sock.settimeout(0.1)
            incoming = bytearray()
            while True:
                try:
                    chunk = sock.recv(FRAME.size - len(incoming))
                except TimeoutError:
                    continue
                if not chunk:
                    break
                incoming.extend(chunk)
                if len(incoming) < FRAME.size:
                    continue
                packet = bytes(incoming)
                incoming.clear()
                decode(packet)
                with server.peers_lock:
                    for peer, receive_own in tuple(server.peers.items()):
                        if peer is sock and not receive_own:
                            continue
                        try:
                            # A stalled consumer must not hold up every peer.
                            # select bounds queueing; frames are only 14 bytes.
                            if not select.select([], [peer], [], 0.1)[1]:
                                raise TimeoutError("simulator consumer is not draining")
                            peer.sendall(bytes([peer is sock]) + packet)
                        except OSError:
                            server.peers.pop(peer, None)
                            try:
                                peer.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
        except (OSError, ValueError):
            pass
        finally:
            with server.peers_lock:
                server.peers.pop(sock, None)


class Simulator(socketserver.ThreadingTCPServer):
    """Explicit localhost simulator; never opens a CAN adapter or remote socket."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 0) -> None:
        self.peers: dict[socket.socket, bool] = {}
        self.peers_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), _Peer)

    @property
    def channel(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"

    def server_close(self) -> None:
        with self.peers_lock:
            for peer in tuple(self.peers):
                try:
                    peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                peer.close()
            self.peers.clear()
        super().server_close()


@dataclass
class _SocketHandle:
    sock: socket.socket
    loaded: LoadedCanFixtureConfig
    buffer: bytearray = field(default_factory=bytearray)
    closed: bool = False


class TcpSimAdapter:
    """Worker-owned software backend using native sockets on all three OSes."""

    def inspect(self, config: LoadedCanFixtureConfig) -> CanBackendCapabilities:
        endpoint(config.config.adapter.channel)
        return CanBackendCapabilities(
            bounded_open=CapabilityStatus.SUPPORTED,
            bounded_send=CapabilityStatus.SUPPORTED,
            bounded_receive=CapabilityStatus.SUPPORTED,
            bounded_close=CapabilityStatus.SUPPORTED,
            pending_tx_quiescence=CapabilityStatus.UNKNOWN,
            physical=False,
            driver="tcp_sim",
            device_identity=("tcp_sim", config.config.adapter.channel),
            observed_channel=config.config.adapter.channel,
            link_kind="simulation",
            bus_state="active",
        )

    def open(self, config: LoadedCanFixtureConfig, timeout_seconds: float) -> CanBusHandle:
        deadline = time.monotonic() + timeout_seconds
        sock: socket.socket | None = None
        try:
            sock = socket.create_connection(
                endpoint(config.config.adapter.channel), timeout_seconds
            )
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            sock.sendall(MAGIC + bytes([config.config.bus.receive_own_messages]))
            # Use a single overall handshake deadline, not a timeout per byte.
            greeting = bytearray()
            while len(greeting) < len(MAGIC):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("CAN simulator handshake timed out")
                sock.settimeout(remaining)
                greeting.extend(_read_exact(sock, 1))
            if greeting != MAGIC:
                raise ValueError("unexpected CAN simulator protocol")
            return CanBusHandle(
                native_handle=_SocketHandle(sock, config),
                profile_id=config.config.profile_id,
                profile_sha256=config.canonical_sha256,
                interface="tcp_sim",
                channel=config.config.adapter.channel,
                protocol="classic",
                filters=tuple(CanFilter.exact(x) for x in config.config.receive.exact_ids),
            )
        except (OSError, ValueError) as exc:
            if sock is not None:
                sock.close()
            raise CanConnectionError(f"Cannot connect to localhost CAN simulator: {exc}") from exc

    @staticmethod
    def _native(handle: CanBusHandle) -> _SocketHandle:
        native = cast(_SocketHandle, handle.native_handle)
        if native.closed:
            raise CanOperationError("CAN simulator handle is closed")
        return native

    def close(self, handle: CanBusHandle) -> None:
        native = cast(_SocketHandle, handle.native_handle)
        if not native.closed:
            native.closed = True
            try:
                native.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            native.sock.close()

    def send(
        self,
        handle: CanBusHandle,
        frame: CanFrame,
        timeout_seconds: float,
        *,
        not_after_monotonic: float | None = None,
    ) -> CanFrame:
        native = self._native(handle)
        if not native.loaded.config.transmit.enabled:
            raise CanPolicyError("can/tx-disabled", "CAN transmit is disabled by operator policy.")
        if frame.identifier not in native.loaded.config.transmit.exact_ids:
            raise CanPolicyError("can/id-forbidden", "CAN identifier is absent from the allowlist.")
        timeout = timeout_seconds
        if not_after_monotonic is not None:
            timeout = min(timeout, not_after_monotonic - time.monotonic())
        if timeout <= 0:
            raise CanTimeoutError("CAN simulator send deadline expired before submission")
        try:
            packet = encode(frame)
            native.sock.settimeout(timeout)
            native.sock.sendall(packet)
        except (OSError, ValueError) as exc:
            # A partial TCP write cannot safely be retried on this stream.
            self.close(handle)
            raise CanOperationError(f"CAN simulator send failed: {exc}") from exc
        return replace(frame, direction=CanDirection.TX, channel=handle.channel)

    def receive(self, handle: CanBusHandle, timeout_seconds: float) -> CanFrame | None:
        native = self._native(handle)
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                if len(native.buffer) == FRAME.size + 1:
                    own_echo = native.buffer[0]
                    if own_echo not in (0, 1):
                        raise ValueError("invalid CAN simulator direction flag")
                    frame = decode(bytes(native.buffer[1:]))
                    native.buffer.clear()
                    if frame.identifier in native.loaded.config.receive.exact_ids:
                        return replace(
                            frame,
                            direction=CanDirection.TX if own_echo else CanDirection.RX,
                            channel=handle.channel,
                            timestamp=time.time(),
                        )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                native.sock.settimeout(remaining)
                chunk = native.sock.recv(FRAME.size + 1 - len(native.buffer))
                if not chunk:
                    raise ConnectionError("CAN simulator disconnected")
                native.buffer.extend(chunk)
        except TimeoutError:
            # Keep partial frames across bounded polls.
            return None
        except (OSError, ValueError) as exc:
            self.close(handle)
            raise CanOperationError(f"CAN simulator receive failed: {exc}") from exc

    def state(self, handle: CanBusHandle) -> str:
        native = self._native(handle)
        if select.select([native.sock], [], [], 0)[0] and not native.sock.recv(1, socket.MSG_PEEK):
            raise CanOperationError("CAN simulator disconnected")
        return "active"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="localhost port; 0 selects a free port")
    args = parser.parse_args()
    with Simulator(args.port) as server:
        print(server.channel, flush=True)
        try:
            server.serve_forever(poll_interval=0.05)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
