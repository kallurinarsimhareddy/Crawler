"""SANA GTM control panel (the "SANA GTM" Start menu / desktop shortcut).

Shows API / Worker / Tunnel / Overall from state\\status.json (written by the
supervisor every 10 s) and offers Start, Stop, Restart, Open SANA GTM, View Logs,
Update and Uninstall. Runs under pythonw.exe: no console window. All decisions live
in panel_logic.py (tested headless); this file is only the Tk view.
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel_logic as L  # noqa: E402
import sanagtm_common as C  # noqa: E402

P = C.installed_paths()
ACTIONS = L.PanelActions(P)
BUSY_LABEL = {"start": "STARTING", "stop": "STOPPING", "restart": "RESTARTING", "update": "UPDATING"}


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
        tk.Label(self, text="Background services on this PC", font=("Segoe UI", 9), fg=L.GREY,
                 bg="white").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        self.values = {}
        for i, name in enumerate(("API", "Worker", "Tunnel", "Overall")):
            font = ("Segoe UI Semibold", 12) if name == "Overall" else ("Segoe UI", 11)
            tk.Label(self, text=name + ":", font=font, bg="white", width=9, anchor="w").grid(
                row=2 + i, column=0, sticky="w", pady=2)
            v = tk.Label(self, text="...", font=font, bg="white", anchor="w", width=16)
            v.grid(row=2 + i, column=1, sticky="w")
            self.values[name] = v
        self.detail = tk.Label(self, text="", font=("Segoe UI", 8), fg=L.GREY, bg="white", justify="left",
                               wraplength=330, anchor="w")
        self.detail.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 10))

        buttons = tk.Frame(self, bg="white")
        buttons.grid(row=7, column=0, columnspan=2, sticky="we")
        self.buttons = []
        for i, (label, action) in enumerate(L.BUTTONS):
            b = tk.Button(buttons, text=label, command=lambda a=action: self.act(a), width=14, font=("Segoe UI", 9))
            b.grid(row=i // 3, column=i % 3, padx=3, pady=3)
            self.buttons.append(b)
        self.refresh()

    def refresh(self):
        view = L.compute_view(C.read_status(P), ACTIONS.supervisor_alive(), C.stop_requested(P))
        for name, (text, colour) in view["rows"].items():
            self.values[name].configure(text=text, fg=colour)
        if not self.busy:
            self.detail.configure(text=view["detail"])
        self.after(3000, self.refresh)

    def act(self, action):
        if action == "uninstall":
            if messagebox.askyesno("Uninstall SANA GTM",
                                   "Remove SANA GTM from this PC?\n\nThis stops the services and deletes the "
                                   "program, its configuration and its logs.", icon="warning"):
                ACTIONS.run("uninstall")
                self.destroy()
            return
        if action in ("open_site", "view_logs"):
            ACTIONS.run(action)
            return
        if self.busy:
            return
        self.busy = True
        for b in self.buttons:
            b.configure(state="disabled")
        self.values["Overall"].configure(text=BUSY_LABEL.get(action, "WORKING"), fg=L.AMBER)

        def run():
            message = ""
            try:
                message = ACTIONS.run(action)
            except Exception as e:  # noqa: BLE001 - shown to the user, never raised inside Tk
                message = f"{action} failed: {type(e).__name__}"
            finally:
                self.after(0, lambda: self.done(message))

        threading.Thread(target=run, daemon=True).start()

    def done(self, message=""):
        self.busy = False
        for b in self.buttons:
            b.configure(state="normal")
        if message:
            self.detail.configure(text=message)


if __name__ == "__main__":
    if single_instance():
        Panel().mainloop()
