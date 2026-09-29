"""Start a process on Windows with NO console window.

    python launch_hidden.py [--cwd DIR] [--stdout FILE] [--stderr FILE] [--wait] -- EXE [ARGS...]

The process is created with CREATE_NO_WINDOW: it gets a console that has no
window, so nothing appears on screen, and every console child it starts
(python's venv launcher -> interpreter, cmd.exe -> npx, ...) inherits that
windowless console instead of opening its own. This is different from
-WindowStyle Hidden / SW_HIDE, which create a normal console and only hide it
afterwards -- on Windows 11 that console is handed to Windows Terminal, which
shows it anyway.

stdin is NUL; stdout/stderr go to the given files (appended; NUL if omitted).
Without --wait the launcher prints the new PID and exits (the process keeps
running). With --wait it waits and exits with the process's exit code -- run
under pythonw.exe (no console of its own) this is how the "SANA GTM Auto Start"
scheduled task starts the supervisor PowerShell without any window.
"""
import argparse
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cwd")
    parser.add_argument("--stdout")
    parser.add_argument("--stderr")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given")

    out = open(args.stdout, "ab") if args.stdout else subprocess.DEVNULL
    err = open(args.stderr, "ab") if args.stderr else subprocess.DEVNULL
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE, for any GUI window as well
    try:
        proc = subprocess.Popen(
            command,
            cwd=args.cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            startupinfo=startupinfo,
            close_fds=True,
        )
    finally:
        for f in (out, err):
            if f is not subprocess.DEVNULL:
                f.close()
    if not args.wait:
        if sys.stdout:
            print(proc.pid, flush=True)
        return 0
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
