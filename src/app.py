#!/usr/bin/env python3
"""
CTI Camera Follow
Bosch CCS 1000 D (micOn)  ->  2x Rocware RC821U (VISCA over IP)  ->  OBS (RTSP + Virtual Camera)

Ruleaza:  venv\\Scripts\\python.exe app.py            (interfata: http://localhost:28080)
          venv\\Scripts\\python.exe app.py --write-obs-ws   (configureaza OBS WebSocket)
"""
import asyncio
import json
import os
import socket
import struct
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
# resurse incluse (static, icons)
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
# folderul de instalare (binare go2rtc / ffmpeg)
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent


def _data_dir():
    d = os.environ.get("CTI_DATA_DIR")
    if d:
        return Path(d)
    if os.name == "nt" and (FROZEN or os.environ.get("CTI_USE_PROGRAMDATA")):
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "CTI Camera Follow"
    return APP_DIR / "data"


DATA_DIR = _data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CFG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "camera_follow.log"

# pythonw.exe nu are consola -> totul merge in fisierul de log
if sys.stdout is None or sys.stderr is None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5_000_000:
            LOG_PATH.unlink()
    except Exception:
        pass
    _lf = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _lf
    sys.stderr = sys.stderr or _lf

import httpx
import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse

import logging
logging.getLogger("obsws_python").setLevel(logging.CRITICAL + 1)
logging.getLogger("obsws_python").propagate = False

try:
    import obsws_python as obsws
    try:
        from obsws_python.error import OBSSDKRequestError as OBS_REQ_ERR
    except Exception:
        OBS_REQ_ERR = None
except ImportError:
    obsws = None
    OBS_REQ_ERR = None

# ─────────────────────────────── config ───────────────────────────────

DEFAULT_CFG = {
    "web_port": 28080,
    "ccs": {"ip": "192.168.11.40", "username": "admin", "password": "", "poll_ms": 500, "auto_discover": True},
    "cameras": {
        "1": {"name": "CAM 1", "ip": "192.168.11.10", "visca_mode": "sony_udp", "visca_port": 52381,
              "rtsp": "rtsp://192.168.11.10:554/live/av0", "rtsp_preview": "", "overview_preset": 200, "enabled": True},
        "2": {"name": "CAM 2", "ip": "192.168.11.12", "visca_mode": "sony_udp", "visca_port": 52381,
              "rtsp": "rtsp://192.168.11.12:554/live/av0", "rtsp_preview": "", "overview_preset": 200, "enabled": False},
    },
    "obs": {"autostart": True, "source_via_go2rtc": True, "freeze_restart_s": 8, "host": "127.0.0.1", "port": 4455, "password": "desa2026", "scene_prefix": "CAM",
            "provision": True, "start_virtualcam": True, "rtsp_transport": "tcp",
            "audio_device_match": "USB", "audio_sync_offset_ms": 400,
            "use_audio_monitor_plugin": True, "monitor_device_match": "CABLE Input"},
    "preview": {"enabled": True, "api_port": 1984, "webrtc_port": 8555, "source": "auto"},
    "logic": {"debounce_ms": 600, "settle_ms": 1500, "min_shot_ms": 2000,
              "overview_after_s": 8, "park_offair_on_overview": True},
    "update": {"enabled": True, "repo": "", "token": "", "check_hours": 6},
    "seats": {},
}


def deep_merge(base, over):
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "seats":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cfg():
    if CFG_PATH.exists():
        try:
            cfg = deep_merge(DEFAULT_CFG, json.loads(CFG_PATH.read_text(encoding="utf-8")))
            if int(cfg.get("web_port", 0)) == 8080:  # migrare: 8080 era ocupat
                cfg["web_port"] = 28080
                save_cfg(cfg)
            return cfg
        except Exception as e:
            print(f"config.json invalid ({e}) - folosesc valorile implicite", flush=True)
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    save_cfg(cfg)
    return cfg


def save_cfg(cfg):
    tmp = CFG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CFG_PATH)


LOG = deque(maxlen=300)


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


# ─────────────────────────────── VISCA ───────────────────────────────

class Visca:
    """VISCA over IP. sony_udp = header Sony 8 octeti (port 52381); raw_udp / raw_tcp = VISCA simplu."""

    def __init__(self, ip, mode="sony_udp", port=52381):
        self.ip, self.mode, self.port = ip, mode, int(port)
        self.seq = 0
        self.lock = threading.Lock()
        if self.mode == "sony_udp":
            try:
                self._udp(struct.pack(">HHI", 0x0200, 1, 0) + b"\x01")  # reset sequence
            except Exception:
                pass

    def _udp(self, pkt):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.3)
            s.sendto(pkt, (self.ip, self.port))
            try:
                s.recvfrom(64)
            except (socket.timeout, OSError):
                pass

    def send(self, payload: bytes):
        with self.lock:
            if self.mode == "sony_udp":
                self.seq = (self.seq + 1) & 0xFFFFFFFF
                self._udp(struct.pack(">HHI", 0x0100, len(payload), self.seq) + payload)
            elif self.mode == "raw_udp":
                self._udp(payload)
            else:  # raw_tcp
                with socket.create_connection((self.ip, self.port), timeout=1.5) as s:
                    s.sendall(payload)
                    s.settimeout(0.3)
                    try:
                        s.recv(64)
                    except (socket.timeout, OSError):
                        pass

    @staticmethod
    def _p(n):
        n = int(n)
        if not 0 <= n <= 254:
            raise ValueError("preset 0-254")
        return n

    def recall(self, n):
        self.send(bytes([0x81, 0x01, 0x04, 0x3F, 0x02, self._p(n), 0xFF]))

    def store(self, n):
        self.send(bytes([0x81, 0x01, 0x04, 0x3F, 0x01, self._p(n), 0xFF]))

    def pantilt(self, dx, dy, speed=8):
        x = 0x01 if dx < 0 else 0x02 if dx > 0 else 0x03
        y = 0x01 if dy > 0 else 0x02 if dy < 0 else 0x03
        vs = max(1, min(0x18, int(speed)))
        ws = max(1, min(0x14, int(speed)))
        self.send(bytes([0x81, 0x01, 0x06, 0x01, vs, ws, x, y, 0xFF]))

    def zoom(self, direction, speed=4):
        s = max(0, min(7, int(speed)))
        b = 0x20 | s if direction > 0 else 0x30 | s if direction < 0 else 0x00
        self.send(bytes([0x81, 0x01, 0x04, 0x07, b, 0xFF]))

    def home(self):
        self.send(bytes([0x81, 0x01, 0x06, 0x04, 0xFF]))


# ─────────────────────────────── Bosch CCS ───────────────────────────────

def _items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("speakers", "seats", "items", "data", "microphones"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def seat_key(s):
    for k in ("id", "seatId", "seat", "unitId"):
        if s.get(k) is not None:
            return str(s[k])
    return None


def parse_active(data):
    out = []
    for s in _items(data):
        if not isinstance(s, dict):
            continue
        if "micOn" in s and not s.get("micOn"):
            continue
        k = seat_key(s)
        if k and k not in out:
            out.append(k)
    return out


class CCS:
    """Sesiune unica refolosita. Doar login-ul e serializat (un login cu override anuleaza sesiunea veche);
    cererile folosesc acelasi token in paralel. La 401 / raspuns gol: re-login o singura data si reincercare.
    /api/speakers?isPolling=true este long-poll: CCS tine cererea deschisa pana la o schimbare."""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=3.0)
        self.lock = asyncio.Lock()
        self.sid = None
        self.sid_ip = None

    async def _login(self, c, stale=None):
        async with self.lock:
            # alta cerere a reinnoit deja tokenul cat am asteptat -> il refolosim
            if self.sid and self.sid_ip == c["ip"] and self.sid != stale:
                return self.sid
            r = await self.client.post(f"http://{c['ip']}/api/login",
                                       json={"username": c["username"], "password": c["password"], "override": True})
            r.raise_for_status()
            try:
                d = r.json()
            except Exception:
                d = r.text
            sid = (d.get("sid") or d.get("token")) if isinstance(d, dict) else str(d).strip().strip('"')
            if not sid:
                raise RuntimeError("login fara sid")
            self.sid, self.sid_ip = sid, c["ip"]
            return sid

    async def _get_once(self, c, path, sid, timeout):
        r = await self.client.get(f"http://{c['ip']}{path}", timeout=timeout,
                                  headers={"Bosch-Sid": sid, "Cookie": f"sid={sid}"})
        if r.status_code in (401, 403):
            raise PermissionError(f"HTTP {r.status_code}")
        r.raise_for_status()
        t = r.text.strip()
        if not t:
            raise PermissionError("raspuns gol (sesiune expirata)")
        return json.loads(t)

    async def ensure(self, c):
        """Login daca nu exista sesiune; dovedeste ca CCS-ul e accesibil."""
        if not (self.sid and self.sid_ip == c["ip"]):
            await self._login(c)

    async def get(self, c, path, timeout=None):
        to = timeout or httpx.Timeout(3.0)
        sid = self.sid if (self.sid and self.sid_ip == c["ip"]) else await self._login(c)
        try:
            return await self._get_once(c, path, sid, to)
        except (PermissionError, json.JSONDecodeError):
            sid = await self._login(c, stale=sid)
            return await self._get_once(c, path, sid, to)


def err_txt(e):
    m = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return f"{type(e).__name__}: {m}" if m else type(e).__name__


# ─────────────────────────────── OBS ───────────────────────────────

AUDIO_SRC = "AUDIO CCS"
AUDIO_MON_FILTER = "Monitor Zoom"


class OBS:
    def __init__(self, eng):
        self.eng = eng
        self.cl = None
        self.lock = threading.Lock()
        self.ok, self.err, self.vcam, self.scene = False, "", False, ""

    def scene_name(self, cam):
        return f"{self.eng.cfg['obs']['scene_prefix']}{cam}"

    def src_name(self, cam):
        return f"SRC {self.scene_name(cam)}"

    def drop(self):
        with self.lock:
            if self.cl is not None:
                try:
                    self.cl.disconnect()
                except Exception:
                    pass
            self.cl = None

    def call(self, fn, *a):
        with self.lock:
            if obsws is None:
                raise RuntimeError("obsws-python lipseste")
            if self.cl is None:
                o = self.eng.cfg["obs"]
                self.cl = obsws.ReqClient(host=o["host"], port=int(o["port"]), password=o["password"], timeout=3)
            try:
                return getattr(self.cl, fn)(*a)
            except Exception as e:
                if not (OBS_REQ_ERR and isinstance(e, OBS_REQ_ERR)):
                    try:
                        self.cl.disconnect()
                    except Exception:
                        pass
                    self.cl = None
                raise

    def poll(self):
        sc = self.call("get_current_program_scene")
        name = getattr(sc, "current_program_scene_name", None) or getattr(sc, "scene_name", "") or ""
        vc = bool(self.call("get_virtual_cam_status").output_active)
        return name, vc

    def cut(self, cam):
        self.call("set_current_program_scene", self.scene_name(cam))

    def screenshot(self, cam):
        return self.call("get_source_screenshot", self.src_name(cam), "jpg", 480, 270, 60).image_data

    def provision(self):
        cfg = self.eng.cfg
        o = cfg["obs"]
        scenes = [s["sceneName"] for s in self.call("get_scene_list").scenes]
        inputs = [i["inputName"] for i in self.call("get_input_list").inputs]
        try:
            vs = self.call("get_video_settings")
            cw, ch = vs.base_width, vs.base_height
        except Exception:
            cw, ch = 1920, 1080

        for cam in [c for c in ("1", "2") if cam_on(cfg, c)]:
            sc, src = self.scene_name(cam), self.src_name(cam)
            if sc not in scenes:
                self.call("create_scene", sc)
            settings = {
                "is_local_file": False,
                "input": self.eng.obs_url(cam),
                "input_format": "",
                "buffering_mb": 0,
                "hw_decode": True,
                "close_when_inactive": False,
                "restart_on_activate": False,
                "clear_on_media_end": False,
                "reconnect_delay_sec": 2,
                "ffmpeg_options": f"rtsp_transport={o.get('rtsp_transport', 'tcp')}",
            }
            if src not in inputs:
                self.call("create_input", sc, src, "ffmpeg_source", settings, True)
                inputs.append(src)
            else:
                self.call("set_input_settings", src, settings, True)
            try:
                iid = self.call("get_scene_item_id", sc, src).scene_item_id
                self.call("set_scene_item_transform", sc, iid, {
                    "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": cw, "boundsHeight": ch,
                    "boundsAlignment": 0, "alignment": 5, "positionX": 0, "positionY": 0})
            except Exception as e:
                log(f"OBS: nu am putut potrivi {src} pe canvas ({e})")

        # audio CCS (placa USB) in ambele scene
        if AUDIO_SRC not in inputs:
            self.call("create_input", self.scene_name(self._first_cam()), AUDIO_SRC, "wasapi_input_capture", {}, True)
        try:
            items = self.call("get_input_properties_list_property_items", AUDIO_SRC, "device_id").property_items
            want = (o.get("audio_device_match") or "").lower()
            pick = next((i for i in items if want and want in str(i.get("itemName", "")).lower()
                         and i.get("itemValue") != "default"), None)
            if not pick:
                bad = ("cable", "vb-audio", "realtek", "array", "what u hear", "stereo mix", "spdif")
                ext = [i for i in items if i.get("itemValue") != "default"
                       and not any(b in str(i.get("itemName", "")).lower() for b in bad)]
                pick = next((i for i in ext if str(i.get("itemName", "")).lower().startswith("line")), None) \
                    or (ext[0] if ext else None)
                if pick:
                    log(f"OBS audio: '{want}' negasit - aleg automat intrarea externa.")
            if pick:
                self.call("set_input_settings", AUDIO_SRC, {"device_id": pick["itemValue"]}, True)
                log(f"OBS audio: {pick['itemName']}")
            else:
                names = ", ".join(str(i.get("itemName")) for i in items)
                log(f"OBS audio: niciun dispozitiv cu '{want}'. Disponibile: {names}")
        except Exception as e:
            log(f"OBS audio: selectie dispozitiv esuata ({e})")
        for cam in [c for c in ("1", "2") if cam_on(cfg, c)]:
            sc = self.scene_name(cam)
            items = [i["sourceName"] for i in self.call("get_scene_item_list", sc).scene_items]
            if AUDIO_SRC not in items:
                self.call("create_scene_item", sc, AUDIO_SRC, True)
        delay = int(o.get("audio_sync_offset_ms", 400))
        self.call("set_input_audio_sync_offset", AUDIO_SRC, delay)

        # traseul spre Zoom/Teams: plugin Audio Monitor (delay propriu) -> CABLE Input
        mon_type = "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"
        if o.get("use_audio_monitor_plugin", True):
            try:
                fs = {"delay": delay, "volume": 100.0}
                dev = self._find_output_device(o.get("monitor_device_match", "CABLE Input"))
                if dev:
                    fs.update({"deviceName": dev[0], "device": dev[1]})
                else:
                    log(f"Audio Monitor: nu gasesc iesirea '{o.get('monitor_device_match')}' (VB-Cable instalat?).")
                filters = [f["filterName"] for f in self.call("get_source_filter_list", AUDIO_SRC).filters]
                if AUDIO_MON_FILTER not in filters:
                    self.call("create_source_filter", AUDIO_SRC, AUDIO_MON_FILTER, "audio_monitor", fs)
                else:
                    self.call("set_source_filter_settings", AUDIO_SRC, AUDIO_MON_FILTER, fs, True)
                mon_type = "OBS_MONITORING_TYPE_NONE"
                log(f"Audio Monitor: {dev[0] if dev else '?'}, delay {delay} ms.")
            except Exception as e:
                log(f"Audio Monitor indisponibil ({e}) - folosesc monitoring-ul OBS.")
        self.call("set_input_audio_monitor_type", AUDIO_SRC, mon_type)
        log("OBS configurat: scene CAM1/CAM2 + audio CCS.")

    def _first_cam(self):
        return next((c for c in ("1", "2") if cam_on(self.eng.cfg, c)), "1")

    def _find_output_device(self, match):
        """Lista iesirilor audio Windows (aceleasi ID-uri ca dispozitivele de monitoring)."""
        tmp = "TMP OUTPUT DEVICES"
        try:
            self.call("create_input", self.scene_name(self._first_cam()), tmp, "wasapi_output_capture", {}, False)
        except Exception:
            pass
        try:
            items = self.call("get_input_properties_list_property_items", tmp, "device_id").property_items
        finally:
            try:
                self.call("remove_input", tmp)
            except Exception:
                pass
        m = (match or "").lower()
        for i in items:
            if m and m in str(i.get("itemName", "")).lower() and i.get("itemValue") != "default":
                return str(i["itemName"]), str(i["itemValue"])
        return None


# ─────────────────────────────── go2rtc (preview video) ───────────────────────────────

GO2RTC_DIR = APP_DIR / "go2rtc"            # binare (folderul de instalare)
GO2RTC_EXE = GO2RTC_DIR / ("go2rtc.exe" if os.name == "nt" else "go2rtc")
GO2RTC_CFG = DATA_DIR / "go2rtc.yaml"       # config generat (folder de date, scriere permisa)
_NOWIN = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


class Go2RTC:
    """RTSP de la camere -> WebRTC in browser. Pornit si supravegheat de aplicatie."""

    def __init__(self):
        self.proc = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def write_cfg(self, cfg):
        p = cfg["preview"]
        ffmpeg = GO2RTC_DIR / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        lines = ["streams:"]
        for c in [x for x in ("1", "2") if cam_on(cfg, x)]:
            cc = cfg["cameras"][c]
            url = (cc.get("rtsp_preview") or cc["rtsp"]).replace('"', "")
            mode = p.get("source", "auto")  # auto = RTSP direct, apoi ffmpeg | rtsp | ffmpeg
            lines.append(f"  cam{c}:")
            native = f'    - "{url}#backchannel=0#timeout=5"'
            viaff = f'    - "ffmpeg:{url}#video=copy"'
            if mode == "rtsp" or (mode == "auto" and not ffmpeg.exists()):
                lines.append(native)
            elif mode == "ffmpeg":
                lines.append(viaff)
            else:
                # auto cu ffmpeg: ffmpeg primul (tolerant la RTSP nestandard, fara sesiuni esuate
                # care raman deschise in camera); conexiunea directa doar ca rezerva
                lines += [viaff, native]
        lines += [
            "api:", f'  listen: ":{int(p["api_port"])}"', '  origin: "*"',
            "rtsp:", '  listen: "127.0.0.1:18554"',  # intern, necesar pentru sursele ffmpeg
            "ffmpeg:", f'  bin: "{ffmpeg.as_posix() if ffmpeg.exists() else "ffmpeg"}"',
            "webrtc:", f'  listen: ":{int(p["webrtc_port"])}"',
            "log:", "  level: warn",
        ]
        GO2RTC_CFG.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def start(self, cfg):
        import subprocess
        if not cfg["preview"].get("enabled", True) or not any(cam_on(cfg, c) for c in ("1", "2")):
            self.stop()
            return
        if not GO2RTC_EXE.exists():
            log("Preview video: go2rtc lipseste (ruleaza SETUP.bat).")
            return
        self.stop()
        if os.name == "nt":  # instanta ramasa de la o rulare anterioara
            subprocess.run(["taskkill", "/f", "/im", "go2rtc.exe"], capture_output=True, creationflags=_NOWIN)
        self.write_cfg(cfg)
        self.proc = subprocess.Popen([str(GO2RTC_EXE), "-config", str(GO2RTC_CFG)], cwd=str(DATA_DIR),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=_NOWIN)
        log(f"Preview video pornit (go2rtc :{cfg['preview']['api_port']}).")

    def stop(self):
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except Exception:
                self.proc.kill()
        self.proc = None


# ─────────────────────────────── OBS (proces) ───────────────────────────────

OBS_EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "obs-studio" / "bin" / "64bit" / "obs64.exe"


def obs_running():
    if os.name != "nt":
        return False
    import subprocess
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"], capture_output=True, text=True,
                       creationflags=_NOWIN)
    return "obs64.exe" in r.stdout.lower()


def start_obs(cfg):
    """Porneste OBS (minimizat, cu Virtual Camera). Scrie config-ul WebSocket doar cu OBS oprit."""
    if os.name != "nt" or not OBS_EXE.exists():
        if os.name == "nt":
            log("OBS nu este instalat.")
        return False
    if obs_running():
        return True
    try:
        write_obs_ws(cfg)
    except Exception as e:
        log(f"OBS WebSocket: nu pot scrie config-ul ({err_txt(e)})")
    import subprocess
    subprocess.Popen([str(OBS_EXE), "--startvirtualcam", "--minimize-to-tray", "--disable-shutdown-check"],
                     cwd=str(OBS_EXE.parent), creationflags=_NOWIN)
    log("OBS pornit.")
    return True


def restart_obs(cfg):
    if os.name != "nt":
        return False
    import subprocess
    subprocess.run(["taskkill", "/F", "/IM", "obs64.exe"], capture_output=True, creationflags=_NOWIN)
    time.sleep(2.5)
    return start_obs(cfg)


# ─────────────────────────────── retea / descoperire ───────────────────────────────

def _tcp_ok(ip, port, timeout=1.0):
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


async def ccs_probe(ip, c, timeout=2.0):
    """True daca la IP raspunde un CCS (login cu sid valid)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as cl:
            r = await cl.post(f"http://{ip}/api/login",
                              json={"username": c.get("username", "admin"), "password": c.get("password", ""),
                                    "override": True})
        if r.status_code != 200:
            return False
        try:
            d = r.json()
        except Exception:
            d = r.text
        sid = (d.get("sid") or d.get("token")) if isinstance(d, dict) else str(d).strip().strip('"')
        return bool(sid) and len(str(sid)) >= 16
    except Exception:
        return False


async def scan_port(p3, port=80, timeout=0.8, conc=128):
    sem = asyncio.Semaphore(conc)
    hits = []

    async def one(h):
        ip = f"{p3}.{h}"
        async with sem:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
                w.close()
                hits.append(ip)
            except Exception:
                pass
    await asyncio.gather(*(one(h) for h in range(1, 255)))
    return sorted(hits, key=lambda x: int(x.rsplit(".", 1)[1]))


def local_subnets():
    out = []
    for ip in _local_ips():
        if ip.startswith(("127.", "169.254.")):
            continue
        p3 = ip.rsplit(".", 1)[0]
        if p3 not in out:
            out.append(p3)
    return out


async def find_ccs(c, skip=()):
    mine = set(_local_ips())
    for nm in ("CCS1000D.local", "CCS1000D"):
        try:
            ip = await asyncio.wait_for(asyncio.to_thread(socket.gethostbyname, nm), 3)
            if await ccs_probe(ip, c):
                return ip
        except Exception:
            pass
    # reteaua CCS-ului stiut intai, apoi celelalte retele ale laptopului
    nets = local_subnets()
    known = str(c.get("ip", "")).rsplit(".", 1)[0]
    nets.sort(key=lambda n: 0 if n == known else 1)
    for p3 in nets:
        for ip in await scan_port(p3, 80):
            if ip in skip or ip in mine:
                continue
            if await ccs_probe(ip, c):
                return ip
    return None


RTSP_PATHS = ["/live/av0", "/live", "/live/1", "/1", "/stream1", "/h264", "/0", "/live/main", "/media/video1", "/"]


def rtsp_describe(url, timeout=2.5):
    """(cod RTSP, are video). Cod '' = nu raspunde."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        host, port = u.hostname, u.port or 554
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
                      f"User-Agent: cti-camera-follow\r\n\r\n".encode())
            data, t0 = b"", time.time()
            while time.time() - t0 < timeout:
                try:
                    ch = s.recv(8192)
                except socket.timeout:
                    break
                if not ch:
                    break
                data += ch
                if b"m=video" in data:
                    break
        txt = data.decode("latin-1", "ignore")
        import re
        m = re.search(r"RTSP/1\.\d\s+(\d{3})", txt)
        return (m.group(1) if m else "?"), ("m=video" in txt)
    except Exception:
        return "", False


def find_rtsp_path(ip, port=554, current=""):
    cands = [current] if current else []
    cands += [f"rtsp://{ip}:{port}{p}" for p in RTSP_PATHS]
    for url in cands:
        code, vid = rtsp_describe(url)
        if code in ("200",) and vid or code == "401":
            return url
    return None


# ─────────────────────────────── versiune / actualizare ───────────────────────────────

try:
    from cti_version import VERSION, REPO as BUILD_REPO
except Exception:
    VERSION, BUILD_REPO = "0.0.0-dev", ""


def _vtuple(v):
    import re
    n = [int(x) for x in re.findall(r"\d+", str(v).split("-")[0])[:3]]
    return tuple(n + [0] * (3 - len(n)))


class Updater:
    """Verifica GitHub Releases si instaleaza versiunea noua (installer-ul Setup.exe)."""

    def __init__(self, eng):
        self.eng = eng
        self.latest, self.asset, self.err, self.busy, self.checked = None, None, "", False, 0.0

    def repo(self):
        return (self.eng.cfg.get("update", {}).get("repo") or BUILD_REPO or "").strip()

    def _headers(self, raw=False):
        h = {"User-Agent": "cti-camera-follow",
             "Accept": "application/octet-stream" if raw else "application/vnd.github+json"}
        t = (self.eng.cfg.get("update", {}).get("token") or "").strip()
        if t:
            h["Authorization"] = f"Bearer {t}"
        return h

    async def check(self):
        repo = self.repo()
        if not repo:
            self.err = "repo GitHub nesetat"
            return None
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cl:
            api = (self.eng.cfg.get("update", {}).get("api") or "https://api.github.com").rstrip("/")
            r = await cl.get(f"{api}/repos/{repo}/releases/latest", headers=self._headers())
        self.checked = time.time()
        if r.status_code == 404:
            self.err = "nicio versiune publicata (sau repo privat fara token)"
            self.latest = self.asset = None
            return None
        r.raise_for_status()
        d = r.json()
        tag = str(d.get("tag_name", "")).lstrip("vV")
        asset = next((a for a in d.get("assets", [])
                      if a.get("name", "").lower().endswith(".exe") and "setup" in a["name"].lower()), None)
        self.err = ""
        if tag and asset and _vtuple(tag) > _vtuple(VERSION):
            if self.latest != tag:
                log(f"Versiune noua disponibila: {tag} (instalata: {VERSION}).")
            self.latest, self.asset = tag, asset
        else:
            self.latest = self.asset = None
        return self.latest

    async def loop(self):
        await asyncio.sleep(15)
        while True:
            u = self.eng.cfg.get("update", {})
            if u.get("enabled", True):
                try:
                    await self.check()
                except Exception as e:
                    self.err = err_txt(e)
            await asyncio.sleep(max(1.0, float(u.get("check_hours", 6))) * 3600)

    async def install(self):
        if not self.asset:
            raise RuntimeError("nu exista o versiune noua")
        import tempfile
        self.busy = True
        try:
            dst = Path(tempfile.gettempdir()) / self.asset["name"]
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=600), follow_redirects=True) as cl:
                async with cl.stream("GET", self.asset["url"], headers=self._headers(raw=True)) as r:
                    r.raise_for_status()
                    with open(dst, "wb") as f:
                        async for ch in r.aiter_bytes(1 << 20):
                            f.write(ch)
            log(f"Actualizare descarcata ({dst.name}). Pornesc instalarea...")
            if os.name == "nt":
                # ShellExecute: installer-ul cere drepturi de Administrator (UAC)
                os.startfile(str(dst), "open", "/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS")
            return str(dst)
        finally:
            self.busy = False


# ─────────────────────────────── Engine ───────────────────────────────

def other(cam):
    return "2" if cam == "1" else "1"


def cam_on(cfg, cam):
    return bool(cfg["cameras"].get(cam, {}).get("enabled", True))


class Engine:
    def __init__(self):
        self.cfg = load_cfg()
        self.ccs = CCS()
        self.obs = OBS(self)
        self.visca = {}
        self.build_visca()
        self.mode = "auto"
        self.first_seen, self.order, self.raw_active = {}, [], []
        self.raw_last = None
        self.ccs_ok, self.ccs_err, self.ccs_fails = False, "", 0
        self.active_cache = []
        self.snap_supported, self.snap_fail, self.lp_ok = None, 0, 0.0
        self.snap_path = None
        self.cam_online = {"1": None, "2": None}
        self.ccs_down_since, self.last_discover, self.discover_task = 0.0, 0.0, None
        self.updater = Updater(self)
        self.frz = {}
        self.live_cam = "1"
        self.cam_pos = {"1": None, "2": None}
        self.target = None
        self.task = None
        self.last_cut = 0.0
        self.last_active = time.time()
        self.seat_names = {}
        self.provisioned = False
        self.g2r = Go2RTC()

    async def g2r_keeper(self):
        await asyncio.to_thread(self.g2r.start, self.cfg)
        while True:
            await asyncio.sleep(10)
            if self.cfg["preview"].get("enabled", True) and GO2RTC_EXE.exists() and not self.g2r.running \
                    and any(cam_on(self.cfg, c) for c in ("1", "2")):
                log("Preview video oprit neasteptat - repornesc.")
                await asyncio.to_thread(self.g2r.start, self.cfg)

    def build_visca(self):
        for c, cc in self.cfg["cameras"].items():
            self.visca[c] = Visca(cc["ip"], cc.get("visca_mode", "sony_udp"), cc.get("visca_port", 52381))

    def name(self, s):
        if s == "overview":
            return "Overview"
        return self.seat_names.get(str(s)) or f"Scaun {s}"

    def label(self, cam):
        return self.cfg["cameras"][cam].get("name") or f"CAM {cam}"

    # ---- bucle ----
    async def run(self):
        log("CTI Camera Follow pornit.")
        asyncio.create_task(self.obs_keeper())
        asyncio.create_task(self.names_keeper())
        asyncio.create_task(self.g2r_keeper())
        asyncio.create_task(self.ccs_poller())
        asyncio.create_task(self.obs_freeze_watch())
        asyncio.create_task(self.cam_watch())
        asyncio.create_task(self.updater.loop())
        if os.name == "nt" and self.cfg["obs"].get("autostart", True):
            await asyncio.to_thread(start_obs, self.cfg)
        # logica ruleaza continuu (debounce, overview), independent de cand raspunde long-poll-ul
        while True:
            if self.ccs_ok:
                self.update(self.active_cache)
            await asyncio.sleep(0.2)

    async def ccs_poller(self):
        """Long-poll pentru reactie rapida + re-login la fiecare long-poll expirat
        (o sesiune moarta nu da eroare, doar nu mai raspunde)."""
        c = self.cfg["ccs"]
        snapshot, relogin = True, False
        asyncio.create_task(self.ccs_watchdog())
        while True:
            lp = httpx.Timeout(3.0, read=float(c.get("longpoll_s", 15)))
            t0 = time.time()
            try:
                if relogin:
                    await self.ccs._login(c, stale=self.ccs.sid)
                    relogin = False
                await self.ccs.ensure(c)
                self._ccs_good()
                if snapshot:
                    await self._snapshot()
                    snapshot = False
                    t0 = time.time()
                data = await self.ccs.get(c, "/api/speakers?isPolling=true", timeout=lp)
                self.raw_last = data
                self._set_active(parse_active(data), "eveniment")
                self._ccs_good()
                self.lp_ok = time.time()
            except httpx.ReadTimeout:
                # niciun eveniment in fereastra long-poll: verific daca sesiunea mai e vie
                # (un login inutil ar putea rata un eveniment exact la reconectare)
                try:
                    sid = self.ccs.sid
                    await self.ccs.get(c, "/api/seats", timeout=httpx.Timeout(3.0))
                    if self.ccs.sid != sid:      # CCS a cerut re-login -> sesiune noua
                        snapshot = True
                    self._ccs_good()
                except Exception:
                    relogin, snapshot = True, True
                continue
            except Exception as e:
                self.ccs_fails += 1
                snapshot, relogin = True, True
                if self.ccs_fails == 1:
                    log(f"CCS: {err_txt(e)} - reincerc")
                if self.ccs_fails == 3:
                    log(f"CCS offline: {err_txt(e)}")
                    self.ccs_ok, self.ccs_err = False, err_txt(e)
                    self.ccs_down_since = time.time()
                elif self.ccs_fails > 3:
                    self.ccs_err = err_txt(e)
                    self._maybe_discover()
                await asyncio.sleep(1.0)
                continue
            took = time.time() - t0
            await asyncio.sleep(0.05 if took > 1.0 else max(0.05, c["poll_ms"] / 1000 - took))

    async def _snapshot(self):
        """Starea curenta fara asteptare. Dezactivat automat daca CCS-ul nu o suporta."""
        if self.snap_supported is False:
            return False
        try:
            paths = [self.snap_path] if self.snap_path else ["/api/speakers", "/api/speakers?isPolling=false"]
            data, last = None, None
            for pth in paths:
                try:
                    data = await self.ccs.get(self.cfg["ccs"], pth, timeout=httpx.Timeout(3.0))
                    if isinstance(data, (list, dict)):
                        self.snap_path = pth
                        break
                    data = None
                except Exception as e:
                    last = e
            if data is None:
                raise last or ValueError("format necunoscut")
            self.snap_supported, self.snap_fail = True, 0
            self.raw_last = data
            self._set_active(parse_active(data), "verificare")
            return True
        except Exception:
            self.snap_fail += 1
            if self.snap_fail >= 3 and self.snap_supported is None:
                self.snap_supported = False
                log("CCS: citirea directa a starii nu e suportata - folosesc doar evenimente.")
            return False

    async def ccs_watchdog(self):
        """Plasa de siguranta: verifica starea la cateva secunde, independent de long-poll."""
        while True:
            await asyncio.sleep(float(self.cfg["ccs"].get("watchdog_s", 2)))
            if self.ccs_ok and self.snap_supported is not False:
                await self._snapshot()

    def _set_active(self, active, src):
        if active != self.active_cache:
            if src == "verificare":
                log(f"CCS (verificare): microfoane active {active or '-'}")
            self.active_cache = active

    async def cam_watch(self):
        """Verifica la 5 s daca fiecare camera activa raspunde pe portul RTSP; logheaza caderile."""
        from urllib.parse import urlparse
        while True:
            for cam in ("1", "2"):
                if not cam_on(self.cfg, cam):
                    self.cam_online[cam] = None
                    continue
                cc = self.cfg["cameras"][cam]
                try:
                    u = urlparse(cc["rtsp"]); host, port = u.hostname or cc["ip"], u.port or 554
                except Exception:
                    host, port = cc["ip"], 554
                ok = (await asyncio.to_thread(_tcp, host, port, 2.0)).startswith("OK")
                prev = self.cam_online.get(cam)
                if ok != prev:
                    if prev is not None or not ok:
                        log(f"{self.label(cam)} {'ONLINE' if ok else 'OFFLINE (nu raspunde pe ' + host + ':' + str(port) + ')'}")
                    if ok and prev is False:
                        self.cam_pos[cam] = None  # pozitia dupa repornire e necunoscuta
                    self.cam_online[cam] = ok
            await asyncio.sleep(5)

    def obs_url(self, cam):
        """URL-ul folosit de OBS: restream local go2rtc daca e disponibil, altfel direct camera."""
        o, p = self.cfg["obs"], self.cfg["preview"]
        if o.get("source_via_go2rtc", True) and p.get("enabled", True) and GO2RTC_EXE.exists():
            return f"rtsp://127.0.0.1:18554/cam{cam}"
        return self.cfg["cameras"][cam]["rtsp"]

    async def obs_freeze_watch(self):
        """Daca imaginea unei camere din OBS nu se schimba deloc N secunde -> repornesc sursa."""
        import hashlib
        restarts = []
        while True:
            await asyncio.sleep(2)
            lim = float(self.cfg["obs"].get("freeze_restart_s", 8))
            if not self.obs.ok or lim <= 0:
                continue
            for cam in [c for c in ("1", "2") if cam_on(self.cfg, c)]:
                src = self.obs.src_name(cam)
                try:
                    img = await asyncio.to_thread(self.obs.call, "get_source_screenshot", src, "jpg", 96, 54, 75)
                    h = hashlib.md5(img.image_data.encode()).hexdigest()
                except Exception:
                    continue
                st = self.frz.setdefault(cam, {"h": None, "since": time.time(), "last_fix": 0.0})
                now = time.time()
                if h != st["h"]:
                    st["h"], st["since"] = h, now
                    continue
                if now - st["since"] < lim or now - st["last_fix"] < 20:
                    continue
                st["last_fix"], st["since"] = now, now
                restarts = [t for t in restarts if now - t < 90] + [now]
                log(f"OBS: imagine inghetata pe {self.label(cam)} ({int(lim)} s) - repornesc sursa.")
                try:
                    await asyncio.to_thread(self.obs.call, "trigger_media_input_action", src,
                                            "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                except Exception as e:
                    log(f"OBS: repornire sursa esuata ({err_txt(e)})")
                if len(restarts) >= 3 and self.cfg["preview"].get("enabled", True):
                    log("OBS: inghetari repetate - repornesc si go2rtc.")
                    restarts = []
                    await asyncio.to_thread(self.g2r.start, self.cfg)

    def _maybe_discover(self):
        c = self.cfg["ccs"]
        now = time.time()
        if not c.get("auto_discover", True) or self.discover_task and not self.discover_task.done():
            return
        if now - self.ccs_down_since < 30 or now - self.last_discover < 120:
            return
        self.last_discover = now
        self.discover_task = asyncio.create_task(self.discover_ccs())

    async def discover_ccs(self):
        """CCS nu raspunde la IP-ul stiut: il caut (CCS1000D.local + retelele laptopului)."""
        c = self.cfg["ccs"]
        log(f"CCS: caut unitatea in retea (nu raspunde la {c['ip']})...")
        skip = {cc.get("ip") for cc in self.cfg["cameras"].values()}
        ip = await find_ccs(c, skip)
        if ip and ip != c["ip"]:
            log(f"CCS gasit la {ip} - actualizez configuratia.")
            c["ip"] = ip
            save_cfg(self.cfg)
            self.ccs.sid = None
        elif not ip:
            log("CCS negasit. Verifica alimentarea si cablul unitatii.")
        return ip

    def _ccs_good(self):
        if not self.ccs_ok:
            log("CCS conectat.")
        self.ccs_ok, self.ccs_err, self.ccs_fails = True, "", 0

    async def names_keeper(self):
        while True:
            try:
                await self.fetch_seats()
            except Exception:
                pass
            await asyncio.sleep(60)

    async def fetch_seats(self):
        data = await self.ccs.get(self.cfg["ccs"], "/api/seats")
        rows = []
        for s in _items(data):
            if isinstance(s, dict) and seat_key(s):
                k = seat_key(s)
                self.seat_names[k] = s.get("name") or f"Scaun {k}"
                rows.append({"id": k, "name": self.seat_names[k], "connected": s.get("connected", True)})
        return rows

    async def obs_keeper(self):
        while True:
            try:
                name, vc = await asyncio.to_thread(self.obs.poll)
                if not self.obs.ok:
                    log("OBS conectat.")
                self.obs.ok, self.obs.err = True, ""
                if self.cfg["obs"].get("provision") and not self.provisioned:
                    await asyncio.to_thread(self.obs.provision)
                    self.provisioned = True
                if not vc and self.cfg["obs"].get("start_virtualcam"):
                    await asyncio.to_thread(self.obs.call, "start_virtual_cam")
                    log("OBS Virtual Camera pornita.")
                    vc = True
                self.obs.scene, self.obs.vcam = name, vc
                if self.task is None or self.task.done():
                    for c in ("1", "2"):
                        if name == self.obs.scene_name(c):
                            self.live_cam = c
            except Exception as e:
                if self.obs.ok or not self.obs.err:
                    log(f"OBS indisponibil: {e}")
                self.obs.ok, self.obs.err = False, str(e) or type(e).__name__
                self.provisioned = False
            await asyncio.sleep(2)

    # ---- logica ----
    def update(self, active):
        now = time.time()
        L = self.cfg["logic"]
        act = set(active)
        self.raw_active = active
        for s in act:
            self.first_seen.setdefault(s, now)
        for s in list(self.first_seen):
            if s not in act:
                del self.first_seen[s]
        self.order = [s for s in self.order if s in act]
        for s in active:
            if s not in self.order and now - self.first_seen[s] >= L["debounce_ms"] / 1000:
                self.order.append(s)
                log(f"Microfon activ: {self.name(s)}")
        desired = self.order[-1] if self.order else None
        if desired:
            self.last_active = now
        if self.mode != "auto":
            return
        if desired:
            if desired != self.target:
                self.go(desired)
        elif self.target != "overview" and now - self.last_active >= L["overview_after_s"]:
            self.go("overview")

    def go(self, target):
        self.target = target
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = asyncio.create_task(self._go(target))

    def _preset(self, cam, target):
        if target == "overview":
            p = self.cfg["cameras"][cam].get("overview_preset")
        else:
            p = self.cfg["seats"].get(str(target), {}).get(cam)
        return None if p in (None, "") else int(p)

    def _plan(self, target):
        live, off = self.live_cam, other(self.live_cam)
        if cam_on(self.cfg, live) and self.cam_pos.get(live) == target and self._preset(live, target) is not None:
            return live, self._preset(live, target)
        for c in (off, live):
            if not cam_on(self.cfg, c):
                continue
            p = self._preset(c, target)
            if p is not None:
                return c, p
        return None

    async def _go(self, target):
        try:
            nm = self.name(target)
            plan = self._plan(target)
            if not plan:
                log(f"Fara preset pentru {nm}.")
                return
            cam, preset = plan
            L = self.cfg["logic"]
            if self.cam_pos.get(cam) != target:
                await asyncio.to_thread(self.visca[cam].recall, preset)
                self.cam_pos[cam] = target
                if cam != self.live_cam:
                    await asyncio.sleep(L["settle_ms"] / 1000)
            if cam != self.live_cam:
                wait = L["min_shot_ms"] / 1000 - (time.time() - self.last_cut)
                if wait > 0:
                    await asyncio.sleep(wait)
                await asyncio.to_thread(self.obs.cut, cam)
                prev = self.live_cam
                self.live_cam, self.last_cut = cam, time.time()
                log(f"LIVE -> {self.label(cam)}  ({nm}, preset {preset})")
                if L.get("park_offair_on_overview") and target != "overview":
                    ov = self._preset(prev, "overview")
                    if ov is not None and self.cam_pos.get(prev) != "overview":
                        await asyncio.to_thread(self.visca[prev].recall, ov)
                        self.cam_pos[prev] = "overview"
            else:
                log(f"{self.label(cam)} (live) -> {nm}, preset {preset}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"Eroare comutare: {e}")

    def status(self):
        return {
            "mode": self.mode,
            "ccs_ok": self.ccs_ok, "ccs_err": self.ccs_err,
            "raw_active": self.raw_active, "confirmed": self.order,
            "target": self.target, "live_cam": self.live_cam, "cam_pos": self.cam_pos,
            "obs_ok": self.obs.ok, "obs_err": self.obs.err, "vcam": self.obs.vcam, "scene": self.obs.scene,
            "cams": {c: self.label(c) for c in ("1", "2")},
            "cams_on": {c: cam_on(self.cfg, c) for c in ("1", "2")},
            "cams_online": self.cam_online,
            "preview": {"running": self.g2r.running, "port": self.cfg["preview"]["api_port"]},
            "version": VERSION,
            "update": {"available": self.updater.latest, "error": self.updater.err, "busy": self.updater.busy,
                       "repo": self.updater.repo()},
            "log": list(LOG)[-120:],
        }


ENG = Engine()


# ─────────────────────────────── Web ───────────────────────────────

@asynccontextmanager
async def lifespan(_app):
    asyncio.create_task(ENG.run())
    yield
    ENG.g2r.stop()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(BASE / "icons" / "app.ico", media_type="image/x-icon")


@app.get("/icon.png")
async def icon_png():
    return FileResponse(BASE / "icons" / "app.png", media_type="image/png")


@app.get("/api/status")
async def api_status():
    return ENG.status()


@app.get("/api/seats")
async def api_seats():
    err = ""
    try:
        rows = await ENG.fetch_seats()
    except Exception as e:
        err = str(e) or type(e).__name__
        rows = [{"id": k, "name": ENG.name(k), "connected": None} for k in ENG.cfg["seats"]]
    try:
        rows.sort(key=lambda r: int(r["id"]))
    except Exception:
        pass
    for r in rows:
        m = ENG.cfg["seats"].get(r["id"], {})
        r["p1"], r["p2"] = m.get("1"), m.get("2")
    return {"rows": rows, "error": err}


@app.post("/api/mapping")
async def api_mapping(b: dict = Body(...)):
    seats = {}
    for sid, m in (b.get("seats") or {}).items():
        e = {}
        for c in ("1", "2"):
            v = m.get(c)
            if v not in (None, ""):
                e[c] = int(v)
        if e:
            seats[str(sid)] = e
    ENG.cfg["seats"] = seats
    save_cfg(ENG.cfg)
    log(f"Mapare salvata ({len(seats)} scaune).")
    return {"ok": True}


@app.post("/api/mode")
async def api_mode(b: dict = Body(...)):
    ENG.mode = "manual" if b.get("mode") == "manual" else "auto"
    ENG.target = None
    log(f"Mod: {ENG.mode.upper()}")
    return {"ok": True}


@app.post("/api/cut")
async def api_cut(b: dict = Body(...)):
    cam = str(b.get("cam", "1"))
    ENG.mode = "manual"
    if ENG.task and not ENG.task.done():
        ENG.task.cancel()
    await asyncio.to_thread(ENG.obs.cut, cam)
    ENG.live_cam, ENG.last_cut = cam, time.time()
    log(f"CUT manual -> {ENG.label(cam)} (mod MANUAL)")
    return {"ok": True}


@app.post("/api/goto")
async def api_goto(b: dict = Body(...)):
    ENG.go(str(b.get("target")))
    return {"ok": True}


@app.post("/api/ptz")
async def api_ptz(b: dict = Body(...)):
    cam = str(b.get("cam", "1"))
    v = ENG.visca[cam]
    a = b.get("action")
    sp = int(b.get("speed", 8))
    if a == "move":
        await asyncio.to_thread(v.pantilt, int(b.get("dx", 0)), int(b.get("dy", 0)), sp)
    elif a == "stop":
        await asyncio.to_thread(v.pantilt, 0, 0, sp)
    elif a == "zoom":
        await asyncio.to_thread(v.zoom, int(b.get("dir", 0)), max(0, min(7, sp // 3)))
    elif a == "recall":
        await asyncio.to_thread(v.recall, int(b["preset"]))
        ENG.cam_pos[cam] = None
    elif a == "store":
        await asyncio.to_thread(v.store, int(b["preset"]))
        log(f"{ENG.label(cam)}: preset {b['preset']} salvat.")
    elif a == "home":
        await asyncio.to_thread(v.home)
        ENG.cam_pos[cam] = None
    return {"ok": True}


@app.post("/api/seat_store")
async def api_seat_store(b: dict = Body(...)):
    sid, cam = str(b["seat"]), str(b["cam"])
    m = ENG.cfg["seats"].setdefault(sid, {})
    p = b.get("preset")
    if p in (None, ""):
        p = m.get(cam)
    if p in (None, ""):
        try:
            p = max(1, min(199, int(sid)))
        except ValueError:
            p = 1
    p = int(p)
    await asyncio.to_thread(ENG.visca[cam].store, p)
    m[cam] = p
    ENG.cam_pos[cam] = sid
    save_cfg(ENG.cfg)
    log(f"{ENG.label(cam)}: {ENG.name(sid)} -> preset {p}")
    return {"ok": True, "preset": p}


@app.post("/api/overview_store")
async def api_overview_store(b: dict = Body(...)):
    cam = str(b["cam"])
    p = ENG.cfg["cameras"][cam].get("overview_preset")
    p = 200 if p in (None, "") else int(p)
    await asyncio.to_thread(ENG.visca[cam].store, p)
    ENG.cfg["cameras"][cam]["overview_preset"] = p
    ENG.cam_pos[cam] = "overview"
    save_cfg(ENG.cfg)
    log(f"{ENG.label(cam)}: overview -> preset {p}")
    return {"ok": True, "preset": p}


@app.get("/api/preview/{cam}")
async def api_preview(cam: str):
    try:
        return {"img": await asyncio.to_thread(ENG.obs.screenshot, cam)}
    except Exception:
        return {"img": None}


@app.get("/api/config")
async def api_config_get():
    return ENG.cfg


@app.post("/api/config")
async def api_config_set(b: dict = Body(...)):
    ENG.cfg = deep_merge(DEFAULT_CFG, b)
    save_cfg(ENG.cfg)
    ENG.build_visca()
    ENG.obs.drop()
    ENG.provisioned = False
    await asyncio.to_thread(ENG.g2r.start, ENG.cfg)
    log("Configuratie salvata si reincarcata.")
    return {"ok": True}


@app.post("/api/provision")
async def api_provision():
    try:
        await asyncio.to_thread(ENG.obs.provision)
        ENG.provisioned = True
        return {"ok": True}
    except Exception as e:
        log(f"OBS provision esuat: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _tcp(ip, port, timeout=1.5):
    t0 = time.time()
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return f"OK ({int((time.time() - t0) * 1000)} ms)"
    except Exception as e:
        return f"NU raspunde ({type(e).__name__})"


def _local_ips():
    ips = set()
    try:
        for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(i[4][0])
    except Exception:
        pass
    for probe in ("192.168.10.1", "192.168.11.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                ips.add(s.getsockname()[0])
        except Exception:
            pass
    return sorted(i for i in ips if not i.startswith("127."))


@app.get("/api/diag")
async def api_diag():
    def run():
        cfg = ENG.cfg
        out = {"ip_laptop": _local_ips(),
               "ccs": {"ip": cfg["ccs"]["ip"], "http_80": _tcp(cfg["ccs"]["ip"], 80)}}
        for c in ("1", "2"):
            cc = cfg["cameras"][c]
            try:
                from urllib.parse import urlparse
                u = urlparse(cc["rtsp"])
                rh, rp = u.hostname or cc["ip"], u.port or 554
            except Exception:
                rh, rp = cc["ip"], 554
            out[f"cam{c}"] = {"ip": cc["ip"], "web_80": _tcp(cc["ip"], 80), f"rtsp_{rp}": _tcp(rh, rp)}
        out["ffmpeg_preview"] = (GO2RTC_DIR / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).exists()
        out["nota"] = ("Fiecare IP de echipament trebuie sa fie in aceeasi retea cu un IP al laptopului "
                       "(ex. 192.168.10.x si 192.168.11.x). Daca lipseste o retea, adaugi un IP secundar pe placa de retea.")
        return out
    return await asyncio.to_thread(run)


SHOW_HOOK = None   # setat de launcher: aduce fereastra in fata


@app.post("/api/ui/show")
async def api_ui_show():
    if SHOW_HOOK:
        SHOW_HOOK()
        return {"ok": True}
    return {"ok": False}


@app.post("/api/update/check")
async def api_update_check():
    try:
        v = await ENG.updater.check()
        return {"ok": True, "latest": v, "current": VERSION, "error": ENG.updater.err}
    except Exception as e:
        return {"ok": False, "error": err_txt(e)}


@app.post("/api/update/install")
async def api_update_install():
    try:
        await ENG.updater.install()
        return {"ok": True}
    except Exception as e:
        log(f"Actualizare esuata: {err_txt(e)}")
        return {"ok": False, "error": err_txt(e)}


@app.post("/api/repair")
async def api_repair():
    """Diagnostic + reparare automata (fara drepturi de Administrator)."""
    items = []

    def add(level, msg):
        items.append({"level": level, "msg": msg})
        log(f"Reparare: {msg}")

    cfg = ENG.cfg
    c = cfg["ccs"]
    changed = False

    # 1. CCS
    if await ccs_probe(c["ip"], c):
        add("ok", f"CCS raspunde la {c['ip']}.")
    else:
        add("warn", f"CCS nu raspunde la {c['ip']} - il caut in retea...")
        skip = {cc.get("ip") for cc in cfg["cameras"].values()}
        ip = await find_ccs(c, skip)
        if ip:
            c["ip"], changed = ip, True
            ENG.ccs.sid = None
            add("fix", f"CCS gasit la {ip}, configuratie actualizata.")
        else:
            add("bad", "CCS negasit. Verifica alimentarea si cablul unitatii CCS.")

    # 2. camere
    from urllib.parse import urlparse
    for cam in ("1", "2"):
        cc = cfg["cameras"][cam]
        used = any(cam in m for m in cfg["seats"].values())
        if not cc.get("enabled", True) and not used:
            add("info", f"{ENG.label(cam)}: dezactivata.")
            continue
        try:
            port = urlparse(cc["rtsp"]).port or 554
        except Exception:
            port = 554
        if not await asyncio.to_thread(_tcp_ok, cc["ip"], port, 1.5):
            web = await asyncio.to_thread(_tcp_ok, cc["ip"], 80, 1.5)
            add("bad", f"{ENG.label(cam)} {cc['ip']}: " + ("interfata web raspunde, dar RTSP e oprit - activeaza RTSP in camera."
                                                          if web else "nu raspunde - verifica alimentarea/PoE si cablul."))
            continue
        code, vid = await asyncio.to_thread(rtsp_describe, cc["rtsp"])
        if code == "200" and vid or code == "401":
            add("ok", f"{ENG.label(cam)}: stream valid ({cc['rtsp']}).")
        else:
            url = await asyncio.to_thread(find_rtsp_path, cc["ip"], port)
            if url:
                cc["rtsp"], changed = url, True
                add("fix", f"{ENG.label(cam)}: cale RTSP corectata -> {url}")
            else:
                add("bad", f"{ENG.label(cam)}: RTSP raspunde ({code or '?'}), dar nu gasesc calea stream-ului.")
        if not cc.get("enabled", True) and used:
            cc["enabled"], changed = True, True
            add("fix", f"{ENG.label(cam)}: reactivata (are presetari).")

    if changed:
        save_cfg(cfg)
        ENG.build_visca()

    # 3. OBS
    if not ENG.obs.ok and os.name == "nt":
        add("warn", "OBS nu raspunde - il repornesc.")
        ENG.obs.drop()
        await asyncio.to_thread(restart_obs, cfg)
        for _ in range(15):
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(ENG.obs.poll)
                ENG.obs.ok = True
                break
            except Exception:
                pass
    if ENG.obs.ok or not ENG.obs.err:
        try:
            await asyncio.to_thread(ENG.obs.provision)
            ENG.provisioned = True
            add("ok", "OBS configurat (scene, surse, audio).")
        except Exception as e:
            add("bad", f"OBS: {err_txt(e)}")
    else:
        add("bad", f"OBS indisponibil: {ENG.obs.err}")

    # 4. preview / restream
    await asyncio.to_thread(ENG.g2r.start, cfg)
    if any(cam_on(cfg, x) for x in ("1", "2")):
        add("ok" if ENG.g2r.running else "bad", "go2rtc " + ("repornit." if ENG.g2r.running else "nu porneste."))
    return {"items": items}


@app.get("/api/debug")
async def api_debug():
    out = {"speakers_last": ENG.raw_last}
    try:
        out["seats_raw"] = await ENG.ccs.get(ENG.cfg["ccs"], "/api/seats")
    except Exception as e:
        out["seats_raw"] = f"eroare: {e}"
    return out


# ─────────────────────────────── CLI ───────────────────────────────

def write_obs_ws(cfg=None):
    cfg = cfg or load_cfg()
    p = Path(os.environ.get("APPDATA", str(Path.home()))) / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update({"first_load": False, "server_enabled": True, "server_port": int(cfg["obs"]["port"]),
                 "auth_required": True, "server_password": cfg["obs"]["password"], "alerts_enabled": False})
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log(f"OBS WebSocket configurat ({p}).")


if __name__ == "__main__":
    if "--write-obs-ws" in sys.argv:
        write_obs_ws()
        sys.exit(0)
    uvicorn.run(app, host="0.0.0.0", port=int(ENG.cfg.get("web_port", 28080)), log_level="warning")
