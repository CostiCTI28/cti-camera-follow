"""
CTI Camera Follow - aplicatie desktop.
Porneste serverul intern, fereastra proprie (WebView2) si iconita de langa ceas.

  CTI Camera Follow.exe            porneste si deschide fereastra
  CTI Camera Follow.exe --tray     porneste ascuns (pornire automata la login)
  CTI Camera Follow.exe --nogui    doar serverul (depanare)
"""
import os
import sys
import threading
import time
import urllib.request

ARGS = set(a.lower() for a in sys.argv[1:])
TITLE = "CTI Camera Follow"


def _port():
    try:
        import json
        from app import CFG_PATH
        return int(json.loads(CFG_PATH.read_text(encoding="utf-8")).get("web_port", 28080))
    except Exception:
        return 28080


def _post(url, timeout=2.0):
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_ok(url, timeout=1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ───────────────────────── instanta unica ─────────────────────────

_MUTEX = None


def single_instance():
    """True daca suntem prima instanta. Altfel aduce fereastra existenta in fata."""
    global _MUTEX
    if os.name != "nt":
        return True
    import ctypes
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\CTI_Camera_Follow_Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        try:
            _post(f"http://127.0.0.1:{_port()}/api/ui/show")
        except Exception:
            pass
        return False
    return True


# ───────────────────────── server ─────────────────────────

class Server:
    def __init__(self, port):
        import uvicorn
        import app as appmod
        self.appmod = appmod
        cfg = uvicorn.Config(appmod.app, host="0.0.0.0", port=port, log_level="warning",
                             log_config=None, access_log=False)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="server", daemon=True)

    def start(self, url):
        self.thread.start()
        for _ in range(150):
            if _get_ok(url + "/api/status"):
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.1)
        return False

    def stop(self):
        try:
            self.appmod.ENG.g2r.stop()
        except Exception:
            pass
        self.server.should_exit = True


def fatal(msg):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, TITLE, 0x10)
    else:
        print(msg, file=sys.stderr)
    sys.exit(1)


# ───────────────────────── tray ─────────────────────────

def make_tray(on_open, on_quit, on_mode, get_mode, on_repair):
    import pystray
    from PIL import Image
    import app as appmod
    img = Image.open(appmod.BASE / "icons" / "app.png")

    def label_mode(_):
        return "Mod: AUTO (click = MANUAL)" if get_mode() == "auto" else "Mod: MANUAL (click = AUTO)"

    menu = pystray.Menu(
        pystray.MenuItem("Deschide", lambda i, it: on_open(), default=True),
        pystray.MenuItem(label_mode, lambda i, it: on_mode()),
        pystray.MenuItem("Diagnostic / Reparare", lambda i, it: on_repair()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Iesire", lambda i, it: on_quit()),
    )
    return pystray.Icon("cti_camera_follow", img, TITLE, menu)


# ───────────────────────── main ─────────────────────────

def main():
    if not single_instance():
        return
    port = _port()
    url = f"http://127.0.0.1:{port}"
    if _get_ok(url + "/api/status"):
        fatal(f"Portul {port} e ocupat de alta aplicatie (posibil o instalare veche pornita).\n"
              f"Opreste-o si porneste din nou CTI Camera Follow.")
    srv = Server(port)
    if not srv.start(url):
        fatal(f"Serverul intern nu a pornit pe portul {port}.\nVezi jurnalul: {srv.appmod.LOG_PATH}")
    eng = srv.appmod.ENG

    if "--nogui" in ARGS:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
        return

    import webview
    state = {"quitting": False}
    win = webview.create_window(TITLE, url, width=1360, height=880, min_size=(900, 600),
                                hidden="--tray" in ARGS, background_color="#0f1115", text_select=True)

    def show():
        try:
            win.show()
            win.restore()
        except Exception:
            pass

    def on_closing():
        if state["quitting"]:
            return True
        win.hide()          # inchiderea ferestrei NU opreste urmarirea camerelor
        return False

    def quit_all():
        state["quitting"] = True
        try:
            tray.stop()
        except Exception:
            pass
        srv.stop()
        try:
            win.destroy()
        except Exception:
            pass
        threading.Timer(3.0, lambda: os._exit(0)).start()

    def toggle_mode():
        eng.mode = "manual" if eng.mode == "auto" else "auto"
        eng.target = None
        srv.appmod.log(f"Mod: {eng.mode.upper()} (din iconita)")

    def repair():
        show()
        try:
            win.evaluate_js("window.runRepair && window.runRepair()")
        except Exception:
            pass

    win.events.closing += on_closing
    srv.appmod.SHOW_HOOK = show
    tray = make_tray(show, quit_all, toggle_mode, lambda: eng.mode, repair)
    tray.run_detached()

    storage = os.path.join(os.environ.get("LOCALAPPDATA") or str(srv.appmod.DATA_DIR), "CTI Camera Follow", "webview")
    try:
        webview.start(private_mode=False, storage_path=storage)
    finally:
        if not state["quitting"]:
            quit_all()


if __name__ == "__main__":
    main()
