"""Жизненный цикл слушателя на Windows: один экземпляр и уборка осиротевших туннелей.

Остановка задачи Планировщика или падение процесса оставляют дочерний ssh жить
(Windows не убивает детей вместе с родителем). При старте гасим только свои туннели —
по ключу id_agents_tunnel и пробросу 8790.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess

log = logging.getLogger("listener")
LOCK_PORT = 8791


def acquire_instance_lock(port: int = LOCK_PORT) -> socket.socket | None:
    """Занять локальный порт как мьютекс; None — слушатель уже запущен."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError:
        sock.close()
        return None
    return sock


def parent_alive(pid: int) -> bool:
    """Жив ли процесс pid. Слушатель запущен под conhost --headless: «Остановить» в Планировщике
    гасит conhost, а Python остаётся сиротой — поэтому он сам выходит, когда родителя не стало."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259                                # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_our_tunnel(cmdline: str) -> bool:
    return "id_agents_tunnel" in (cmdline or "") and "8790:127.0.0.1:8790" in cmdline


def kill_stale_tunnels() -> int:
    if os.name != "nt":
        return 0
    script = ("Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    killed = 0
    for line in out.splitlines():
        pid, _, cmdline = line.partition("\t")
        if pid.strip().isdigit() and is_our_tunnel(cmdline):
            subprocess.run(["taskkill", "/F", "/PID", pid.strip()], capture_output=True)
            killed += 1
    if killed:
        log.info("убраны осиротевшие туннели: %s", killed)
    return killed
