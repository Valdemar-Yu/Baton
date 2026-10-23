"""Portable test executables, built from pip's already-installed launcher (no install)."""
import io
import os
from pathlib import Path
import struct
import sys
import subprocess
import time
import zipfile


def executable_fixture(path):
    path = Path(path)
    if os.name != "nt":
        path.chmod(path.stat().st_mode | 0o111)
        return path
    from pip._vendor import distlib
    launcher = Path(distlib.__file__).parent / ("t64.exe" if struct.calcsize("P") == 8 else "t32.exe")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("__main__.py", path.read_text(encoding="utf-8"))
    target = path.with_suffix(".exe")
    target.write_bytes(launcher.read_bytes() + ('#!"' + sys.executable + '"\n').encode("utf-8") + archive.getvalue())
    return target


def directory_link(link, target):
    """A Windows junction exercises realpath without symlink privileges."""
    if os.name == "nt":
        env = dict(os.environ, BATON_TEST_LINK=str(link), BATON_TEST_TARGET=str(target))
        subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                        "New-Item -ItemType Junction -Path $env:BATON_TEST_LINK -Target $env:BATON_TEST_TARGET | Out-Null"],
                       env=env, check=True, capture_output=True)
    else:
        Path(link).symlink_to(target, target_is_directory=True)


def wait_windows_pid(pid, timeout=30):
    import baton
    import ctypes
    from ctypes import wintypes
    # GetExitCodeProcess can report termination before all inherited log handles
    # are released. Wait on the process object before deleting test directories.
    kernel = baton.platform_support._kernel()
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x00100000, False, int(pid)) if pid else None
    if handle:
        try:
            result = kernel.WaitForSingleObject(handle, int(timeout * 1000))
            if result != 0:
                baton.platform_support.stop_tree(pid, (0, 3))
                raise AssertionError(f"Test process {pid} did not exit in {timeout}s")
        finally:
            kernel.CloseHandle(handle)
    elif pid and ctypes.get_last_error() != 87:
        raise ctypes.WinError(ctypes.get_last_error())
    baton.reap_detached_runners()
    if pid and baton.pid_alive(pid):
        baton.platform_support.stop_tree(pid, (0, 3))
        raise AssertionError(f"Test process {pid} did not exit in {timeout}s")
