"""SANA GTM control panel (the "SANA GTM" Start menu / desktop shortcut).

Shows API / Worker / Tunnel / Overall from state\\status.json (written by the
supervisor every 10 s) and offers Start, Stop, Restart, Open SANA GTM, View Logs
and Uninstall. Runs under pythonw.exe: no console window.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sanagtm_common as C  # noqa: E402

P = C.installed_paths()

GREEN, RED, AMBER, GREY = "#1a7f37", "#cf222e", "#9a6700", "#57606a"


def single_instance() -> bool:
    import ctypes

    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    single_instance.handle = k32.CreateMutexW(None, True, "Local\\SanaGtmPanel-" + C.instance_id(P.root))
    return k32.GetLastError() != 183


class Panel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SANA GTM")
        self.resizable(False, False)
        try:
            self.iconbitmap(str(P.icon))
        except tk.TclError:
            pass
        self.busy = False
        self.configure(bg="white", padx=22, pady=18)
        tk.Label(self, text="SANA GTM", font=("Segoe UI Semibold", 16), bg="white").grid(row=0, column=0,
                                                                                         columnspan=2, sticky="w")
        tk.Label(self, text="Background services on this PC", font=("Segoe UI", 9), fg=GREY,
                 bg="white").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.values = {}
        for i, name in enumerate(("API", "Worker", "Tunnel", "Overall")):
            font = ("Segoe UI Semibold", 12) if name == "Overall" else ("Segoe UI", 11)
            tk.Label(self, text=name + ":", font=font, bg="white", width=9, anchor="w").grid(
                row=2 + i, column=0, sticky="w", pady=2)
            v = tk.Label(self, text="...", font=font, bg="white", anchor="w", width=16)
            v.grid(row=2 + i, column=1, sticky="w")
            self.values[name] = v
        self.detail = tk.Label(self, text="", font=("Segoe UI", 8), fg=GREY, bg="white", justify="left",
                               wraplength=330, anchor="w")
        self.detail.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 10))

        buttons = tk.Frame(self, bg="white")
        buttons.grid(row=7, column=0, columnspan=2, sticky="we")
        spec = [("Start", self.start), ("Stop", self.stop), ("Restart", self.restart),
                ("Open SANA GTM", self.open_site), ("View Logs", self.view_logs), ("Uninstall", self.uninstall)]
        self.buttons = []
        for i, (label, cmd) in enumerate(spec):
            b = tk.Button(buttons, text=label, command=cmd, width=14, font=("Segoe UI", 9))
            b.grid(row=i // 3, column=i % 3, padx=3, pady=3)
            self.buttons.append(b)
        self.refresh()

    # --- status ------------------------------------------------------------------------------
    def supervisor_alive(self) -> bool:
        return any(C.classify(p) == "supervisor" for p in C.owned_processes(P))

    def refresh(self):
        st = C.read_status(P)
        alive = self.supervisor_alive()
        stale = time.time() - float(st.get("updated", 0) or 0) > 60

        def show(name, text, color):
            self.values[name].configure(text=text, fg=color)

        if not alive:
            stopped = C.stop_requested(P) or st.get("overall") == "STOPPED"
            for n in ("API", "Worker", "Tunnel"):
                show(n, "OFFLINE", RED)
            if st.get("overall") == "NOT CONFIGURED":
                show("Overall", "NOT CONFIGURED", RED)
            else:
                show("Overall", "STOPPED" if stopped else "NOT RUNNING", RED)
            self.detail.configure(text="Press Start to start SANA GTM." if not st.get("error") else st["error"])
        elif stale or not st.get("api"):
            for n in ("API", "Worker", "Tunnel"):
                show(n, "STARTING", AMBER)
            show("Overall", "STARTING", AMBER)
            self.detail.configure(text="The supervisor is starting the services...")
        else:
            for n, key in (("API", "api"), ("Worker", "worker"), ("Tunnel", "tunnel")):
                comp = st.get(key, {})
                if comp.get("online"):
                    show(n, "ONLINE", GREEN)
                elif comp.get("running"):
                    show(n, "STARTING", AMBER)
                else:
                    show(n, "OFFLINE", RED)
            ready = st.get("overall") == "READY"
            show("Overall", "READY" if ready else "NOT READY", GREEN if ready else AMBER)
            notes = []
            if st.get("database") is False:
                notes.append("Database (Supabase) not reachable.")
            if st.get("queue") is False:
                notes.append("Queue (Upstash) not reachable.")
            if st["tunnel"].get("url"):
                notes.append("Tunnel: " + st["tunnel"]["url"])
            self.detail.configure(text="\n".join(notes))
        self.after(3000, self.refresh)

    # --- actions -------------------------------------------------------------------------------
    def in_background(self, label, fn):
        if self.busy:
            return
        self.busy = True
        for b in self.buttons:
            b.configure(state="disabled")
        self.values["Overall"].configure(text=label, fg=AMBER)

        def run():
            try:
                fn()
            finally:
                self.after(0, self.done)

        threading.Thread(target=run, daemon=True).start()

    def done(self):
        self.busy = False
        for b in self.buttons:
            b.configure(state="normal")

    def start(self):
        self.in_background("STARTING", lambda: C.start_supervisor(P))

    def stop(self):
        self.in_background("STOPPING", lambda: C.stop_everything(P))

    def restart(self):
        def both():
            C.stop_everything(P)
            time.sleep(1)
            C.start_supervisor(P)
        self.in_background("RESTARTING", both)

    def open_site(self):
        webbrowser.open(C.load_settings(P).get("frontend_url") or C.FRONTEND_URL)

    def view_logs(self):
        os.startfile(str(P.logs))

    def uninstall(self):
        if not messagebox.askyesno("Uninstall SANA GTM",
                                   "Remove SANA GTM from this PC?\n\nThis stops the services and deletes the "
                                   "program, its configuration and its logs.", icon="warning"):
            return
        C.spawn_hidden([str(P.pythonw), str(P.manager / "uninstall.py"), "--confirmed"], cwd=os.environ.get("TEMP"))
        self.destroy()


if __name__ == "__main__":
    if single_instance():
        Panel().mainloop()
