"""Native Windows support; callers retain their original POSIX paths."""
import contextlib
import ctypes
from ctypes import wintypes
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time


def utf8_stdio():
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")


def codex_readonly_config():
    """Read-only monitors ignore user config, including the Windows sandbox choice."""
    return ["-c", 'windows.sandbox="unelevated"'] if os.name == "nt" else []


class _ConsoleTTY:
    """The Windows console as one tty-like object (CONIN$ in, CONOUT$ out)."""

    def __init__(self):
        self._in = open("CONIN$", "r", encoding="utf-8")
        try:
            self._out = open("CONOUT$", "w", encoding="utf-8")
        except OSError:
            self._in.close()
            raise

    def write(self, text):
        self._out.write(text)

    def flush(self):
        self._out.flush()

    def readline(self):
        return self._in.readline()

    def close(self):
        try:
            self._out.close()
        finally:
            self._in.close()


def tty_name():
    return "CONIN$" if os.name == "nt" else "/dev/tty"


def open_tty():
    """The user's terminal, bypassing redirected stdin/stdout."""
    if os.name == "nt":
        return _ConsoleTTY()
    return open("/dev/tty", "r+", encoding="utf-8")


def executable(name):
    """Windows must never execute npm .cmd/.ps1 shims (TOML quoting is lost)."""
    if os.name != "nt":
        return name
    candidate = name
    if os.path.splitext(candidate)[1].lower() != ".exe":
        if os.path.splitext(candidate)[1]:
            raise ValueError(f"Native .exe required, not a shell shim: {name}")
        candidate += ".exe"
    resolved = shutil.which(candidate)
    if not resolved:
        raise FileNotFoundError(f"Native executable not found: {candidate}")
    return os.path.abspath(resolved)


def directory_junction(source, target):
    """Create a Windows directory junction without requiring Developer Mode."""
    if os.name != "nt":
        raise OSError("directory junctions are Windows-only")
    source = os.path.abspath(source)
    target = os.path.abspath(target)
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    result = subprocess.run([comspec, "/d", "/c", "mklink", "/J", target, source],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode == 0 and os.path.isdir(target):
        return target
    detail = (result.stderr or result.stdout or "").strip()
    raise OSError(f"mklink /J failed ({result.returncode}): {detail}")


def _kernel():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                  ctypes.POINTER(wintypes.FILETIME)]
    return k


def pid_alive(pid):
    """Never os.kill(pid, 0): on Windows it is not a liveness probe."""
    if not pid or int(pid) <= 0:
        return False
    k = _kernel()
    handle = k.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID does not exist
            return False
        raise ctypes.WinError(error)  # access denied is not proof of death
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value == 259  # STILL_ACTIVE
    finally:
        k.CloseHandle(handle)


def powershell_json(script, timeout=8):
    prefix = ("$ErrorActionPreference='Stop'; "
              "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); ")
    p = subprocess.run([executable("powershell"), "-NoLogo", "-NoProfile",
                        "-NonInteractive", "-Command", prefix + script],
                       capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode:
        raise RuntimeError(f"Windows process query failed: {p.stderr.strip()}")
    return json.loads(p.stdout) if p.stdout.strip() else None


_WMI_ACCESS_DENIED_MARKERS = ("0x80041003", "access denied", "access is denied",
                              "permissiondenied", "拒绝访问")
_FILETIME_UNIX_EPOCH = 116444736000000000


def _wmi_access_denied(error):
    text = str(error).lower()
    return any(marker in text for marker in _WMI_ACCESS_DENIED_MARKERS)


def _native_process_creation(pid):
    """Creation time as a CIM-style JSON date, from the Windows process handle."""
    k = _kernel()
    handle = k.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                 ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        value = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return f"/Date({(value - _FILETIME_UNIX_EPOCH) // 10000})/"
    finally:
        k.CloseHandle(handle)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def _native_process_rows():
    """Toolhelp snapshot with creation times; used when the CIM namespace is denied."""
    k = _kernel()
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k.Process32NextW.restype = wintypes.BOOL
    snapshot = k.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    rows = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            pid = int(entry.th32ProcessID)
            created = _native_process_creation(pid)
            if created is not None:
                rows.append({"ProcessId": pid,
                             "ParentProcessId": int(entry.th32ParentProcessID),
                             "CreationDate": created})
            found = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    return rows


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2_0", ctypes.c_void_p), ("Reserved2_1", ctypes.c_void_p),
                ("UniqueProcessId", ctypes.c_void_p), ("Reserved3", ctypes.c_void_p)]


def _native_proc_cmdline(pid):
    """Read another process's command line without WMI, using its PEB."""
    k = _kernel()
    handle = k.OpenProcess(0x1000 | 0x10, False, int(pid))  # QUERY_LIMITED_INFORMATION | VM_READ
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID does not exist
            return ""
        raise RuntimeError(f"Cannot open PID {pid} for its command line: {ctypes.WinError(error)}")
    try:
        k.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        wow64 = wintypes.BOOL()
        if not k.IsWow64Process(handle, ctypes.byref(wow64)):
            raise ctypes.WinError(ctypes.get_last_error())
        ptr_size = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 and not wow64.value else 4
        nt = ctypes.WinDLL("ntdll", use_last_error=True)
        nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                                wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        nt.NtQueryInformationProcess.restype = ctypes.c_long
        basic = _PROCESS_BASIC_INFORMATION()
        returned = wintypes.ULONG()
        status = nt.NtQueryInformationProcess(handle, 0, ctypes.byref(basic),
                                              ctypes.sizeof(basic), ctypes.byref(returned))
        if status != 0:
            raise RuntimeError(f"Cannot read process information for PID {pid}: NTSTATUS {status:#x}")
        peb = int(basic.PebBaseAddress or 0)
        if ptr_size == 4 and ctypes.sizeof(ctypes.c_void_p) == 8:
            peb32 = ctypes.c_void_p()
            status = nt.NtQueryInformationProcess(handle, 26, ctypes.byref(peb32),
                                                  ctypes.sizeof(peb32), ctypes.byref(returned))
            if status != 0:
                raise RuntimeError(f"Cannot read 32-bit PEB for PID {pid}: NTSTATUS {status:#x}")
            peb = int(peb32.value or 0)

        def read(address, size):
            buffer = (ctypes.c_ubyte * size)()
            count = ctypes.c_size_t()
            if not k.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(count)) \
                    or count.value != size:
                raise ctypes.WinError(ctypes.get_last_error())
            return bytes(buffer)

        params = int.from_bytes(read(peb + (0x20 if ptr_size == 8 else 0x10), ptr_size), "little")
        head = read(params + (0x70 if ptr_size == 8 else 0x40), 16 if ptr_size == 8 else 8)
        length = int.from_bytes(head[:2], "little")
        if length > 32768:
            raise RuntimeError(f"Invalid command-line length for PID {pid}: {length}")
        if not length:
            return ""
        buffer = int.from_bytes(head[8:16] if ptr_size == 8 else head[4:8], "little")
        return read(buffer, length).decode("utf-16-le")
    finally:
        k.CloseHandle(handle)


def _process_rows():
    try:
        rows = powershell_json("@(Get-CimInstance Win32_Process | Select-Object "
                               "ProcessId,ParentProcessId,CreationDate) | ConvertTo-Json -Compress")
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        if isinstance(error, RuntimeError) and not _wmi_access_denied(error):
            raise
        rows = _native_process_rows()
    return rows or []


def _process_creation_time(pid):
    try:
        current = powershell_json(
            f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
            "Select-Object CreationDate | ConvertTo-Json -Compress")
    except (RuntimeError, subprocess.TimeoutExpired) as error:
        if isinstance(error, RuntimeError) and not _wmi_access_denied(error):
            raise
        return _native_process_creation(pid)
    return current.get("CreationDate") if current else None


def proc_cmdline(pid):
    """CIM provides Unicode full command lines; timeout/query denial raises, never 'dead'."""
    try:
        result = powershell_json(
            f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
            "Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress")
    except RuntimeError as error:
        if not _wmi_access_denied(error):
            raise
        try:
            result = _native_proc_cmdline(pid)
        except (OSError, RuntimeError):
            if not pid_alive(pid):
                return ""  # the process exited while CIM was being denied
            raise
    if not result and pid_alive(pid):
        raise RuntimeError(f"Cannot read command line of live PID {pid}; refusing PID-reuse guess")
    return result or ""


def lock_file(lock, blocking=True):
    import msvcrt
    # Lock a byte beyond EOF; do not truncate/write the shared file while it is locked.
    while True:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EDEADLK):
                raise
            if not blocking:
                return False  # explicitly requested nonblocking contention
            time.sleep(0.05)


@contextlib.contextmanager
def exclusive_lock(path):
    with open(path, "a+b") as lock:
        lock_file(lock)
        try:
            yield lock
        finally:
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def terminate_group(proc, force=False):
    """Terminate a timed-out monitor/verification and its descendants."""
    if os.name == "nt":
        stop_tree(proc.pid, (0 if force else 2, 3))
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def atomic_replace(source, destination, timeout=2.0):
    """Keep atomic publication while Windows readers briefly deny delete sharing."""
    deadline = None
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33):
                raise
            if deadline is None:
                deadline = time.monotonic() + timeout
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def read_text(path, timeout=2.0):
    """Read UTF-8, retrying Windows's brief delete-pending publication window."""
    deadline = None
    while True:
        try:
            with open(path, encoding="utf-8") as stream:
                return stream.read()
        except PermissionError:
            if os.name != "nt":
                raise
            if deadline is None:
                deadline = time.monotonic() + timeout
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def replace_readonly(source, destination):
    """Windows cannot replace a read-only generated artifact; keep final mode."""
    mode = None
    if os.name == "nt" and os.path.isfile(destination):
        import stat
        mode = os.stat(destination).st_mode
        os.chmod(destination, mode | stat.S_IWRITE)
    try:
        atomic_replace(source, destination)
    except BaseException:
        if mode is not None:
            os.chmod(destination, mode)
        raise


def venv_python(home):
    return os.path.join(home, "venv", "Scripts", "python.exe") if os.name == "nt" else os.path.join(home, "venv", "bin", "python")


def detached_popen(args, **kwargs):
    if os.name != "nt":
        return subprocess.Popen(args, start_new_session=True, **kwargs)
    # A hidden private console permits targeted CTRL_BREAK; DETACHED_PROCESS and
    # CREATE_NO_WINDOW have no console to signal. NEW_CONSOLE supplies a new group.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE
    try:
        return subprocess.Popen(args, startupinfo=startup,
                                creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                                **kwargs)
    except OSError as e:
        if e.winerror != 5:
            raise
        print("[fail] Job denied breakaway; retrying with a hidden console inside the job. "
              "Survival of parent-job teardown is not guaranteed.", file=sys.stderr)
        return subprocess.Popen(args, startupinfo=startup, creationflags=flags, **kwargs)


def _send_break(pid):
    # Run in a disposable helper: attaching must not change the caller's console.
    k = _kernel()
    k.FreeConsole()
    if not k.AttachConsole(int(pid)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not k.GenerateConsoleCtrlEvent(1, int(pid)):  # CTRL_BREAK_EVENT
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.1)  # delivery is asynchronous
    finally:
        k.FreeConsole()


def send_break(pid):
    try:
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "--break", str(pid)],
                           capture_output=True, text=True, encoding="utf-8", timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as e:
        print(f"[fail] CTRL_BREAK helper timed out for PID {pid}: {e}; "
              "continuing to timed tree termination", file=sys.stderr)
        return False
    if p.returncode:
        print(f"[fail] CTRL_BREAK for PID {pid}: {p.stderr.strip()}; "
              "continuing to timed tree termination", file=sys.stderr)
    return p.returncode == 0


def process_tree(pid):
    """Capture descendant identities before graceful exit can orphan them."""
    rows = _process_rows()
    by_pid = {r["ProcessId"]: r for r in rows}
    if int(pid) not in by_pid:
        return {}
    # ParentProcessId can refer to an old, reused PID. An older process is not
    # this incarnation's child, even if its recorded parent number matches.
    def created(row):
        return int(re.fullmatch(r"/Date\((-?\d+)\)/", row["CreationDate"])[1])
    members = {int(pid)}
    while True:
        children = {r["ProcessId"] for r in rows if r["ParentProcessId"] in members
                    and created(r) >= created(by_pid[r["ParentProcessId"]])}
        if children <= members:
            break
        members |= children
    return {r["ProcessId"]: r["CreationDate"] for r in rows if r["ProcessId"] in members}


def _terminate_process(pid):
    """Last-resort termination when taskkill is denied by the current job/sandbox."""
    k = _kernel()
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateProcess.restype = wintypes.BOOL
    handle = k.OpenProcess(0x0001, False, int(pid))  # PROCESS_TERMINATE
    if not handle:
        return ctypes.get_last_error() == 87  # already gone
    try:
        return bool(k.TerminateProcess(handle, 1))
    finally:
        k.CloseHandle(handle)


def _kill_process(pid):
    p = subprocess.run([executable("taskkill"), "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode and pid_alive(pid) and not _terminate_process(pid):
        raise RuntimeError(f"taskkill failed for live PID {pid}: {p.stderr!r}")


def stop_tree(pid, graces):
    identities = process_tree(pid)
    send_break(pid)
    # Windows has no SIGTERM stage. Retain its time budget for graceful shutdown.
    deadline = time.monotonic() + sum(graces[:-1])
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    # Kill root first (/T includes newly spawned descendants), then any orphans
    # captured before CTRL_BREAK. Recheck creation dates to guard PID reuse.
    for child in sorted(identities, key=lambda p: p != pid):
        created = identities[child]
        if not pid_alive(child):
            continue
        if _process_creation_time(child) != created:
            continue  # PID now belongs to a different process
        _kill_process(child)
    deadline = time.monotonic() + graces[-1]
    while any(pid_alive(p) for p in identities) and time.monotonic() < deadline:
        time.sleep(0.2)
    alive = [p for p in identities if pid_alive(p)]
    if alive:
        raise RuntimeError(f"Process tree survived stop: {alive}")


def pipe_ready(fd, timeout):
    """select() cannot wait on Windows anonymous pipes. Peek without blocking."""
    import msvcrt
    k = _kernel()
    k.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                               ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    handle = msvcrt.get_osfhandle(fd)
    deadline = time.monotonic() + timeout
    while True:
        available = wintypes.DWORD()
        if not k.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error == 109:  # broken pipe: let the caller read EOF
                return True
            raise ctypes.WinError(error)
        if available.value:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


if __name__ == "__main__":
    utf8_stdio()
    if len(sys.argv) == 3 and sys.argv[1] == "--break":
        _send_break(int(sys.argv[2]))
    else:
        raise SystemExit("Internal helper: expected --break PID")
