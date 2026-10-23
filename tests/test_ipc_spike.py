# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false, reportUnknownArgumentType=false, reportUnknownParameterType=false
"""Spike A, IPC half: peer identity and pipe/socket access control (design sections 8.6, 15).

These tests are the evidence behind the platform matrix in design section 15. The Windows
helper code here is a prototype of what task 2.14 builds properly.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest

WINDOWS = sys.platform == "win32"
SRC = str(Path(__file__).resolve().parents[1] / "src")

CLIENT = """
    import os, sys
    path = sys.argv[1]
    if sys.platform == "win32":
        with open(path, "r+b", buffering=0) as pipe:
            pipe.write(b"hello")
            reply = pipe.read(10)
    else:
        import socket
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(path)
            s.sendall(b"hello")
            reply = s.recv(10)
    print(os.getpid(), reply.decode(), flush=True)
"""


def run_client(address: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(CLIENT), address],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# --- Windows: named pipes ---------------------------------------------------------------------

if sys.platform == "win32":
    import _winapi
    import ctypes
    from asyncio import windows_events, windows_utils
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)

    _k32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
    ]  # fmt: skip
    _k32.CreateNamedPipeW.restype = wintypes.HANDLE
    _k32.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _adv.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _adv.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]  # fmt: skip
    _adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
    ]  # fmt: skip
    _adv.GetSecurityInfo.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]  # fmt: skip
    _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p,
    ]  # fmt: skip

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [  # noqa: RUF012
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    def current_user_sid() -> str:
        token = wintypes.HANDLE()
        assert _adv.OpenProcessToken(_k32.GetCurrentProcess(), 0x8, ctypes.byref(token))
        size = wintypes.DWORD()
        _adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        assert _adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size))
        sid_ptr = ctypes.c_void_p.from_buffer(buffer).value  # TOKEN_USER.User.Sid
        text = wintypes.LPWSTR()
        assert _adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text))
        value = text.value
        assert value is not None
        _k32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        return value

    def pipe_sddl(handle: int) -> str:
        descriptor = ctypes.c_void_p()
        assert (
            _adv.GetSecurityInfo(handle, 6, 0x7, None, None, None, None, ctypes.byref(descriptor))
            == 0
        )
        text = wintypes.LPWSTR()
        assert _adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 0x7, ctypes.byref(text), None
        )
        value = text.value
        assert value is not None
        _k32.LocalFree(descriptor)
        return value

    def client_pid(handle: int) -> int:
        pid = wintypes.ULONG()
        assert _k32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid))
        return pid.value

    def create_pipe_instance(name: str, *, first: bool, owner_only: bool) -> int:
        """What asyncio does, plus an owner-only DACL and rejection of remote clients."""
        flags = _winapi.PIPE_ACCESS_DUPLEX | _winapi.FILE_FLAG_OVERLAPPED
        if first:
            flags |= _winapi.FILE_FLAG_FIRST_PIPE_INSTANCE  # a second server on the name fails
        mode = _winapi.PIPE_TYPE_MESSAGE | _winapi.PIPE_READMODE_MESSAGE | _winapi.PIPE_WAIT
        attributes = None
        keep: list[Any] = []
        if owner_only:
            mode |= 0x8  # PIPE_REJECT_REMOTE_CLIENTS
            descriptor = ctypes.c_void_p()
            sddl = f"D:P(A;;GA;;;{current_user_sid()})"  # protected: no inherited entries
            assert _adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None
            )
            sa = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, False)
            keep.append(sa)
            attributes = ctypes.byref(sa)
        handle = _k32.CreateNamedPipeW(name, flags, mode, 255, 65536, 65536, 0, attributes)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    class OwnerOnlyPipeServer(windows_events.PipeServer):  # type: ignore[misc]
        """asyncio's pipe server with an owner-only DACL (asyncio passes a NULL descriptor)."""

        def _server_pipe_handle(self, first: bool) -> Any:
            if self.closed():
                return None
            pipe = windows_utils.PipeHandle(
                create_pipe_instance(self._address, first=first, owner_only=True)
            )
            self._free_instances.add(pipe)
            return pipe

    def pipe_name() -> str:
        return "\\\\.\\pipe\\relay-spike-" + uuid.uuid4().hex

    async def serve_one(
        name: str, monkeypatch: pytest.MonkeyPatch, *, owner_only: bool
    ) -> dict[str, Any]:
        if owner_only:
            monkeypatch.setattr(windows_events, "PipeServer", OwnerOnlyPipeServer)
        loop = asyncio.get_running_loop()
        seen: dict[str, Any] = {}
        finished: asyncio.Future[None] = loop.create_future()

        class Echo(asyncio.Protocol):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                self.transport = transport
                pipe = transport.get_extra_info("pipe")
                seen["handle"] = pipe.handle
                seen["peer_pid"] = client_pid(pipe.handle)
                seen["sddl"] = pipe_sddl(pipe.handle)

            def data_received(self, data: bytes) -> None:
                assert isinstance(self.transport, asyncio.WriteTransport)
                self.transport.write(b"echo:" + data)
                if not finished.done():
                    finished.set_result(None)

        servers = await loop.start_serving_pipe(Echo, name)  # type: ignore[attr-defined]
        client = run_client(name)
        await asyncio.wait_for(finished, 20)
        out, err = await asyncio.to_thread(client.communicate, None, 20)
        for server in servers:
            server.close()
        assert client.returncode == 0, err
        pid_text, reply = out.split()
        seen.update(client_pid=int(pid_text), reply=reply)
        return seen

    @pytest.mark.skipif(not WINDOWS, reason="Windows named pipes")
    def test_windows_has_no_af_unix_in_python() -> None:
        """Why Windows IPC is a named pipe: this Python exposes no AF_UNIX on Windows."""
        assert not hasattr(socket, "AF_UNIX")

    @pytest.mark.skipif(not WINDOWS, reason="Windows named pipes")
    def test_default_asyncio_pipe_grants_read_to_everyone_and_anonymous(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Finding: asyncio's pipe server passes a NULL security descriptor, so the default
        DACL lets `Everyone` and `Anonymous` open the pipe for reading."""
        seen = asyncio.run(serve_one(pipe_name(), monkeypatch, owner_only=False))
        assert seen["reply"] == "echo:hello"
        assert "(A;;FR;;;WD)" in seen["sddl"]
        assert "(A;;FR;;;AN)" in seen["sddl"]

    @pytest.mark.skipif(not WINDOWS, reason="Windows named pipes")
    def test_owner_only_pipe_serves_asyncio_and_reports_the_peer_pid(
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Finding: subclassing `PipeServer` gives an asyncio-served pipe whose DACL contains
        exactly one entry (the current user), and the server learns the client's PID."""
        seen = asyncio.run(serve_one(pipe_name(), monkeypatch, owner_only=True))
        assert seen["reply"] == "echo:hello"
        assert seen["peer_pid"] == seen["client_pid"]
        dacl = seen["sddl"].split("D:", 1)[1]
        assert dacl == f"P(A;;FA;;;{current_user_sid()})"  # Windows reports GA as FA

    @pytest.mark.skipif(not WINDOWS, reason="Windows named pipes")
    def test_first_instance_flag_stops_a_second_server_on_the_same_name() -> None:
        name = pipe_name()
        first = create_pipe_instance(name, first=True, owner_only=True)
        try:
            with pytest.raises(OSError):
                create_pipe_instance(name, first=True, owner_only=True)
        finally:
            _winapi.CloseHandle(first)


# --- Unix: domain sockets ---------------------------------------------------------------------

if sys.platform != "win32":

    async def serve_unix(path: Path) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        finished: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            sock = writer.get_extra_info("socket")
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)  # pid, uid, gid
            seen["peer_pid"], seen["peer_uid"], _ = struct_unpack(creds)
            data = await reader.read(10)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()
            finished.set_result(None)

        server = await asyncio.start_unix_server(handle, path=str(path))
        os.chmod(path, 0o600)
        seen["socket_mode"] = path.stat().st_mode & 0o777
        client = run_client(str(path))
        await asyncio.wait_for(finished, 20)
        out, err = await asyncio.to_thread(client.communicate, None, 20)
        server.close()
        await server.wait_closed()
        assert client.returncode == 0, err
        pid_text, reply = out.split()
        seen.update(client_pid=int(pid_text), reply=reply)
        return seen

    def struct_unpack(raw: bytes) -> tuple[int, int, int]:
        import struct

        pid, uid, gid = struct.unpack("3i", raw)
        return pid, uid, gid

    def test_unix_socket_reports_peer_pid_and_uid(tmp_path: Path) -> None:
        if not hasattr(socket, "SO_PEERCRED"):
            pytest.skip("SO_PEERCRED is Linux only; macOS would use LOCAL_PEERCRED")
        seen = asyncio.run(serve_unix(tmp_path / "s.sock"))
        assert seen["reply"] == "echo:hello"
        assert seen["peer_pid"] == seen["client_pid"]
        assert seen["peer_uid"] == os.getuid()
        assert seen["socket_mode"] == 0o600

    def test_unix_socket_path_length_limit(tmp_path: Path) -> None:
        """Finding: `sun_path` is about 108 bytes (104 on macOS), so deep run directories need
        the hashed short-path fallback of design section 6."""
        long_dir = tmp_path
        while len(str(long_dir)) < 130:
            long_dir = long_dir / ("d" * 20)
        long_dir.mkdir(parents=True)
        with socket.socket(socket.AF_UNIX) as sock, pytest.raises(OSError, match="too long|path"):
            sock.bind(str(long_dir / "ipc.sock"))
