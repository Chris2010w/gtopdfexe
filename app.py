import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import gallery_core as core

APP_DIR = os.path.join(os.path.expanduser("~"), ".galleryToPDF")
CFG = os.path.join(APP_DIR, "settings.json")
DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Downloads", "GalleryToPDF")


def load_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def save_cfg(d):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gallery → PDF")
        self.geometry("820x720")
        self.minsize(700, 600)
        cfg = load_cfg()
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.last = []

        self.url = tk.StringVar()
        self.name = tk.StringVar()
        self.out = tk.StringVar(value=cfg.get("out", DEFAULT_OUT))
        self.mode = tk.StringVar(value=cfg.get("mode", "PDF"))
        self.delay = tk.StringVar(value=cfg.get("delay", "0.5"))
        self.quality = tk.StringVar(value=cfg.get("quality", "1080"))
        self.original = tk.BooleanVar(value=cfg.get("original", False))
        self.cookie = tk.StringVar(value=cfg.get("cookie", ""))
        self.site_lbl = tk.StringVar(value="")
        self.status = tk.StringVar(value="Έτοιμο.")

        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=8, pady=8)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Link:").grid(row=0, column=0, sticky="w", **pad)
        e = ttk.Entry(frm, textvariable=self.url)
        e.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Επικόλληση", command=self.paste).grid(row=0, column=2, **pad)
        ttk.Label(frm, textvariable=self.site_lbl, foreground="#2a6").grid(row=1, column=1, sticky="w", padx=10)
        self.url.trace_add("write", lambda *_: self.refresh())
        self.original.trace_add("write", lambda *_: self.refresh())

        ttk.Label(frm, text="Όνομα (προαιρετικό):").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.name).grid(row=2, column=1, columnspan=2, sticky="ew", **pad)

        ttk.Label(frm, text="Φάκελος:").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(frm, textvariable=self.out).grid(row=3, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Επιλογή...", command=self.browse).grid(row=3, column=2, **pad)

        self.opts = ttk.Frame(frm)
        self.opts.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.gal = ttk.Frame(self.opts)
        ttk.Label(self.gal, text="Αρχείο:").pack(side="left")
        ttk.Combobox(self.gal, textvariable=self.mode, values=["PDF", "CBZ", "PDF + CBZ"], width=10, state="readonly").pack(side="left", padx=6)
        ttk.Label(self.gal, text="Καθυστέρηση (sec):").pack(side="left", padx=(12, 0))
        ttk.Combobox(self.gal, textvariable=self.delay, values=["0", "0.3", "0.5", "1", "2"], width=5).pack(side="left", padx=6)
        self.vid = ttk.Frame(self.opts)
        ttk.Label(self.vid, text="Ποιότητα βίντεο (μέγιστο ύψος):").pack(side="left")
        ttk.Combobox(self.vid, textvariable=self.quality, values=["2160", "1440", "1080", "720", "480"], width=6, state="readonly").pack(side="left", padx=6)

        self.ex = ttk.Frame(frm)
        self.ex.grid(row=5, column=0, columnspan=3, sticky="ew", padx=10)
        self.ex.columnconfigure(1, weight=1)
        self.chk = ttk.Checkbutton(self.ex, text="Πρωτότυπη ποιότητα (original)", variable=self.original)
        self.ckl = ttk.Label(self.ex, text="Cookies:")
        self.cke = ttk.Entry(self.ex, textvariable=self.cookie)
        self.hint = ttk.Label(self.ex, foreground="#888",
                              text="ipb_member_id=...; ipb_pass_hash=...; igneous=...  (το igneous μόνο για ExHentai)")

        bt = ttk.Frame(frm)
        bt.grid(row=6, column=0, columnspan=3, sticky="ew", padx=10, pady=8)
        self.go = ttk.Button(bt, text="Κατέβασε", command=self.start)
        self.go.pack(side="left")
        self.stop = ttk.Button(bt, text="Ακύρωση", command=self.cancel_job, state="disabled")
        self.stop.pack(side="left", padx=8)
        self.openb = ttk.Button(bt, text="Άνοιγμα φακέλου", command=self.open_folder)
        self.openb.pack(side="left")
        self.openf = ttk.Button(bt, text="Άνοιγμα αρχείου", command=self.open_file, state="disabled")
        self.openf.pack(side="left", padx=8)

        self.bar = ttk.Progressbar(frm, maximum=100)
        self.bar.grid(row=7, column=0, columnspan=3, sticky="ew", padx=10)
        ttk.Label(frm, textvariable=self.status).grid(row=8, column=0, columnspan=3, sticky="w", padx=10, pady=2)

        self.logbox = tk.Text(frm, height=16, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(frm, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        self.logbox.grid(row=9, column=0, columnspan=2, sticky="nsew", padx=(10, 0), pady=4)
        sb.grid(row=9, column=2, sticky="nsw", pady=4)
        frm.rowconfigure(9, weight=1)

        self.refresh()
        self.after(100, self.pump)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- UI helpers
    def paste(self):
        try:
            self.url.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out.get() or DEFAULT_OUT)
        if d:
            self.out.set(d)

    def refresh(self):
        u = self.url.get().strip()
        s = core.detect_site(u)
        self.site_lbl.set(("Site: " + s[0]) if s else "")
        video = bool(s and s[1] in ("ytdlp", "deviantart"))
        for w in (self.gal, self.vid):
            w.pack_forget()
        (self.vid if video else self.gal).pack(side="left")
        low = u.lower()
        eh = "e-hentai.org" in low
        exh = "exhentai.org" in low
        for w in (self.chk, self.ckl, self.cke, self.hint):
            w.grid_forget()
        if eh or exh:
            self.chk.grid(row=0, column=0, columnspan=2, sticky="w", pady=2)
        if exh or (eh and self.original.get()):
            self.ckl.grid(row=1, column=0, sticky="w")
            self.cke.grid(row=1, column=1, sticky="ew", padx=6)
            self.hint.grid(row=2, column=1, sticky="w", padx=6)

    def log(self, s):
        self.q.put(("log", s))

    def prog(self, cur, total, text):
        self.q.put(("prog", (cur, total, text)))

    def pump(self):
        try:
            while True:
                kind, v = self.q.get_nowait()
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", v + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "prog":
                    cur, total, text = v
                    if total <= 0:
                        self.bar.configure(mode="indeterminate")
                        self.bar.start(15)
                    else:
                        self.bar.stop()
                        self.bar.configure(mode="determinate", value=min(100, cur * 100 / total))
                    if text:
                        self.status.set(text)
                elif kind == "done":
                    self.finish(v)
        except queue.Empty:
            pass
        self.after(100, self.pump)

    # ---- job control
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        url = self.url.get().strip()
        if not core.detect_site(url):
            messagebox.showwarning("Link", "Βάλε ένα έγκυρο link (http/https).")
            return
        out = self.out.get().strip() or DEFAULT_OUT
        try:
            delay = float(self.delay.get().replace(",", "."))
        except ValueError:
            delay = 0.5
        mode = {"PDF": "pdf", "CBZ": "cbz", "PDF + CBZ": "both"}.get(self.mode.get(), "pdf")
        try:
            quality = int(self.quality.get())
        except ValueError:
            quality = 1080
        save_cfg({"out": out, "mode": self.mode.get(), "delay": self.delay.get(), "quality": self.quality.get(),
                  "original": self.original.get(), "cookie": self.cookie.get()})
        self.cancel.clear()
        self.last = []
        self.go.configure(state="disabled")
        self.stop.configure(state="normal")
        self.openf.configure(state="disabled")
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")
        cb = core.Reporter(self.log, self.prog, self.cancel)
        work = os.path.join(APP_DIR, "work")

        def job():
            try:
                res = core.run_job(url, self.name.get().strip(), out, work, mode, delay, self.cookie.get(), quality,
                                   self.original.get(), cb)
                self.q.put(("done", ("ok", res)))
            except core.Cancelled:
                self.q.put(("done", ("cancel", None)))
            except core.JobError as e:
                self.q.put(("done", ("err", str(e))))
            except Exception as e:  # noqa: BLE001
                import traceback
                self.log(traceback.format_exc())
                self.q.put(("done", ("err", "Απρόσμενο σφάλμα: %s" % e)))

        self.worker = threading.Thread(target=job, daemon=True)
        self.worker.start()

    def cancel_job(self):
        self.cancel.set()
        self.status.set("Ακύρωση...")

    def finish(self, v):
        kind, data = v
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.go.configure(state="normal")
        self.stop.configure(state="disabled")
        if kind == "ok":
            self.last = data
            self.bar.configure(value=100)
            self.status.set("Έτοιμο ✔")
            for p in data:
                self.log("→ " + p)
            self.openf.configure(state="normal")
        elif kind == "cancel":
            self.status.set("Ακυρώθηκε.")
            self.log("Ακυρώθηκε.")
        else:
            self.status.set("Σφάλμα.")
            self.log("ΣΦΑΛΜΑ: " + data)

    def open_folder(self):
        d = self.out.get().strip() or DEFAULT_OUT
        os.makedirs(d, exist_ok=True)
        self._open(d)

    def open_file(self):
        if self.last:
            self._open(self.last[0])

    @staticmethod
    def _open(path):
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Άνοιγμα", str(e))

    def on_close(self):
        self.cancel.set()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
