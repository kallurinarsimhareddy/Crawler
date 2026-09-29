"""Report whether processes' consoles have a window on screen (read-only).

    python console_window.py PID [PID...]

Prints one JSON object per PID:
  {"pid": 123, "console": true, "window": false, "host": "", "visible": false}

A process started with CREATE_NO_WINDOW (launch_hidden.py) has a console but no
console window. A console handed to Windows Terminal has a window owned by
OpenConsole.exe (shown as a Terminal tab even though the window itself reports
hidden); a classic console has a conhost.exe window. "visible" is true for both.
"""
import ctypes
import json
import sys
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.GetConsoleWindow.restype = wintypes.HWND
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)


def check(pid):
    result = {"pid": pid, "console": False, "window": False, "host": "", "visible": False}
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return result
    try:
        result["console"] = True
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            host = process_name(owner.value)
            result.update(window=True, host=host,
                          visible=bool(user32.IsWindowVisible(hwnd)) or host.lower() == "openconsole.exe")
    finally:
        kernel32.FreeConsole()
    return result


def main(argv):
    lines = [json.dumps(check(int(a))) for a in argv]
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
