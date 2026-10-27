#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A1 打印看门狗 (a1_watchdog.py)

盯着拓竹 A1 的打印画面，用 AI 判断有没有出现炒面、翘边、堆料这类失败征兆，
发现后弹窗报警、存图、写日志，同时记录打印机状态和这一炉的耗时耗料。

三个通道：
  1. 摄像头：打印机 6000 端口 TLS 加拓竹二进制协议直接取 JPEG 帧
     （不需要 LAN 模式，不占用 Handy App）
  2. 打印机状态：本地 MQTT 8883（用户 bblp 加打印机访问码），只订阅
     device/<序列号>/report，只发 pushall、get_version 这类只读请求
  3. AI 检测：OctoEverywhere Gadget API（免费额度每月 9 万次），
     或自备本地模型（见 detection.engine）

本软件默认不发送任何控制命令。
默认配置下只读不写：不暂停、不恢复、不改温度、不动风扇和 AMS。
原因是拓竹从固件 01.08 起对第三方软件的控制做了授权校验，未授权的控制
命令会被打印机拒绝（err_code 0x05024007）。本项目定位是监控加报警，
控制权留给用户和官方工具。

如确认自己的机器固件早于该机制，或已开启开发者模式，并愿意承担全部后果，
可以在配置里设 alert.allow_pause = true 打开自动暂停。详见 README。

用法：
  python a1_watchdog.py --check    自检：摄像头 / MQTT / API key
  python a1_watchdog.py --once     单次取帧加检测（测试用）
  python a1_watchdog.py            常驻监控
"""

import argparse
import json
import logging
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paho.mqtt.client as mqtt
import requests

import job_metrics

def _resolve_base_dir() -> Path:
    """数据目录：环境变量 A1_WATCHDOG_HOME，其次打包目录，最后脚本目录。"""
    env = os.environ.get("A1_WATCHDOG_HOME", "").strip()
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "watchdog"
    return Path(__file__).resolve().parent


BASE_DIR = _resolve_base_dir()
EXAMPLE_CONFIG_NAME = "a1-watchdog.config.example.json"
EXAMPLE_SECRETS_NAME = "a1-watchdog.secrets.example.json"
CONFIG_PATH = BASE_DIR / "a1-watchdog.config.json"
SECRETS_PATH = BASE_DIR / "a1-watchdog.secrets.json"
LOG_PATH = BASE_DIR / "a1-watchdog.log"
STATUS_PATH = BASE_DIR / "a1-watchdog-status.json"
FRAMES_DIR = BASE_DIR / "frames"
# 本机 AI 巡检开关：工作台监控墙每块砖上的「AI」勾选就写这个文件。
# 存在 = 这台只看画面不判定（那卷料本来就会拉丝、或者在打样，别误判）。
# 用标记文件而不是 config：改 config 要重启进程才生效，重启会打断正在
# 进行的监控；标记文件每轮读一次，勾掉下一轮就停。
AI_OFF_MARKER = BASE_DIR / "a1-watchdog.ai-off"

# 「当前程度可以接受」标记：人眼看过后点一下，之后只有更严重才再提示。
# 里面存的是当时那一帧的严重程度（0~100）。新打印开始时会自动清掉，
# 免得上一炉的宽松标准带进下一炉。
ACK_MARKER = BASE_DIR / "a1-watchdog.ack.json"
# 比已接受的程度再高出这么多分才算「更严重」。给分数抖动留余量，
# 否则 78 分接受完，下一帧 79 分又弹一次，等于没接受。
ACK_MARGIN = 10

GADGET_CREATE_CONTEXT_URL = "https://gadget-pv1-oeapi.octoeverywhere.com/api/gadget/v1/createcontext"

log = logging.getLogger("a1-watchdog")


def setup_logging(verbose=False):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(sh)


def _as_float(value):
    """宽松转 float：云端和固件都爱把数字给成字符串，坏值一律当没有。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_json(path, required=True):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if not required:
            return {}
        log.error("找不到配置文件: %s", os.path.basename(path))
        log.error("先复制一份模板再把打印机地址填进去:")
        log.error("  copy %s %s", EXAMPLE_CONFIG_NAME, os.path.basename(path))
        log.error("或者直接双击 run.bat，它会自动复制并提示你改哪里。")
        sys.exit(2)
    except ValueError as e:
        log.error("配置文件不是合法 JSON: %s（%s）", os.path.basename(path), e)
        log.error("常见原因是改了之后少个逗号或者多了个逗号，找个 JSON 校验网站贴进去看。")
        sys.exit(2)


PLACEHOLDER_PREFIX = "PUT_"


def is_placeholder(value):
    """模板里的占位值要用能认出来的写法，否则用户忘了改就会被当成真值。

    历史上模板写的是中文说明（"打印机访问码"这类），中文进 HTTP 头会抛
    latin-1 编码错误，报错信息完全看不出是没填。统一用 PUT_ 开头。
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v:
        return True
    if v.upper().startswith(PLACEHOLDER_PREFIX):
        return True
    try:
        v.encode("ascii")
    except UnicodeEncodeError:
        return True
    return False


def access_code_for(cfg, secrets):
    """按序列号取访问码，每台机器的访问码不同，不能全局共用一个。

    优先级：secrets["access_codes"][serial] → secrets["access_code"]。
    后者是老格式（只有一台机器时的写法），留着兼容，别让老配置直接跑不起来。
    """
    serial = str(cfg.get("printer", {}).get("serial", ""))
    codes = secrets.get("access_codes") or {}
    if serial and codes.get(serial):
        return codes[serial]
    return secrets.get("access_code", "")


LOCK_PATH = BASE_DIR / "a1-watchdog.lock"
_lock_handle = None


def acquire_single_instance():
    """拿独占文件锁，防止同时跑两个看门狗。

    必须防：拓竹固件同一时刻只允许一个摄像头连接，第二个实例会取不到帧、不停
    重试；更糟的是两个进程抢着写同一个状态文件，界面看到的心跳和 PID 会来回
    跳（这个坑真实踩到过，上一轮遗留的进程让新进程的状态看起来全是错的）。
    用文件锁而不是比 PID：PID 会被系统复用，文件锁不会，进程一死系统自动释放。
    """
    global _lock_handle
    try:
        import msvcrt
    except ImportError:
        return True  # 非 Windows，不挡
    try:
        fh = open(LOCK_PATH, "a+")
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
    except OSError:
        pass
    _lock_handle = fh  # 必须保持引用，句柄一关锁就没了
    return True


# ---------------- 摄像头（6000 端口拓竹二进制协议） ----------------

class BambuCamera:
    """TLS 连 6000，发 80 字节认证包，之后流式返回 [16字节头(LE uint32 长度) + JPEG]。"""

    def __init__(self, ip, access_code, timeout=15):
        self.ip = ip
        self.access_code = access_code
        self.timeout = timeout
        self.sock = None

    def connect(self):
        self.close()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((self.ip, 6000), timeout=self.timeout)
        self.sock = ctx.wrap_socket(raw)
        auth = struct.pack(
            "<II8s32s32s", 0x40, 0x3000, b"\x00" * 8,
            b"bblp".ljust(32, b"\x00"),
            self.access_code.encode().ljust(32, b"\x00"))
        self.sock.sendall(auth)
        self.sock.settimeout(self.timeout)
        return self

    def _read_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(min(65536, n - len(buf)))
            if not chunk:
                raise ConnectionError("摄像头连接被打印机关闭")
            buf += chunk
        return buf

    def get_frame(self):
        if self.sock is None:
            self.connect()
        header = self._read_exact(16)
        size = struct.unpack("<I", header[0:4])[0]
        if size == 0 or size > 6 * 1024 * 1024:
            raise ConnectionError("异常帧大小: %s" % size)
        data = self._read_exact(size)
        if not data.startswith(b"\xff\xd8"):
            raise ConnectionError("收到的不是 JPEG 帧")
        return data

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# ---------------- 本地 MJPEG 中转（给 OBS / 浏览器看） ----------------

class FramePump:
    """常驻线程持续读摄像头，缓存最新一帧。

    为什么要常驻：6000 端口实测只有约 0.4 帧/秒（2.4 秒一张），如果只在检测那
    一刻连一次，除检测瞬间之外根本没有画面可以给 OBS。常驻还能省掉每次重连的
    TLS 握手（约 0.5 秒）。

    只在打印中运行：不打印时关掉连接，把摄像头让给 Bambu Studio / 手机 App。
    拓竹固件同一时刻只允许一个摄像头客户端，这是硬约束。

    为什么不让 OBS 自己连：6000 是拓竹私有二进制协议（16 字节长度头 + JPEG），
    OBS 的 ffmpeg 源不认，它只认 322 端口的 RTSP，而 RTSP 要开发者模式。
    所以方向只能反过来，我们独占，再对外吐标准 MJPEG。
    """

    def __init__(self, camera):
        self.camera = camera
        self._frame = b""
        self._ts = 0.0
        self._seq = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="frame-pump", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        try:
            self.camera.close()
        except Exception:
            pass

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                jpeg = self.camera.get_frame()
            except Exception as e:
                if self._stop.is_set():
                    break
                log.warning("取帧失败: %s（%.0f 秒后重连）", e, backoff)
                try:
                    self.camera.close()
                except Exception:
                    pass
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 15.0)
                continue
            with self._lock:
                self._frame = jpeg
                self._ts = time.time()
                self._seq += 1
            backoff = 1.0
            self._wake.set()

    def snapshot(self):
        with self._lock:
            return self._frame, self._ts, self._seq

    def wait_frame(self, after_seq=-1, timeout=25.0):
        """等一帧比 after_seq 更新的画面，语义等同原来阻塞式 get_frame()。

        超时就返回手上现有的（可能是旧的，调用方自己判断），别让检测线程卡死。
        """
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            frame, ts, seq = self.snapshot()
            if seq > after_seq and frame:
                return frame, ts, seq
            self._wake.wait(0.2)
            self._wake.clear()
        return self.snapshot()


_PUMP = None  # HTTP 处理器要用，模块级引用最省事

_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Bambu 画面中转</title>
<style>
  html,body{margin:0;height:100%;background:#111;color:#ddd;
    font:13px/1.6 "Microsoft YaHei",sans-serif;overflow:hidden}
  img{display:block;width:100%;height:100%;object-fit:contain}
  #badge{position:absolute;left:10px;top:10px;padding:4px 10px;border-radius:4px;
    background:#000a;font-size:12px}
  #badge.live{color:#7ed957}
  #badge.stale{color:#e2a03f}
</style>
<img id="v" src="/stream.mjpg" alt="等待画面">
<div id="badge">连接中…</div>
<script>
async function tick(){
  try{
    const d = await (await fetch('/status',{cache:'no-store'})).json();
    const b = document.getElementById('badge');
    if(d.seq === 0){ b.className='stale'; b.textContent='暂无画面'; }
    else if(d.live){ b.className='live'; b.textContent='实时 · 第 '+d.seq+' 帧'; }
    else { b.className='stale'; b.textContent='已停止更新 · '+(d.age_sec|0)+' 秒前'; }
  }catch(e){}
}
setInterval(tick, 2000); tick();
</script>
"""


class _StreamHandler(BaseHTTPRequestHandler):
    """只服务本机，三个端点：
    /            带状态角标的预览页（OBS 用「浏览器源」指这里最稳）
    /stream.mjpg multipart/x-mixed-replace 的 MJPEG 流
    /latest.jpg  最新单帧
    /status      JSON，给页面判断画面是不是停了
    """

    def log_message(self, fmt, *args):
        pass  # 别把 OBS 的轮询刷进日志

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/stream.mjpg":
            self._stream()
        elif path == "/latest.jpg":
            self._latest()
        elif path == "/status":
            self._status()
        else:
            self._page()

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _page(self):
        self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"))

    def _latest(self):
        frame, ts, seq = _PUMP.snapshot() if _PUMP else (b"", 0.0, 0)
        if not frame:
            self._send(503, "text/plain; charset=utf-8", "暂无画面".encode("utf-8"))
            return
        self._send(200, "image/jpeg", frame)

    def _status(self):
        frame, ts, seq = _PUMP.snapshot() if _PUMP else (b"", 0.0, 0)
        age = round(time.time() - ts, 1) if ts else None
        body = json.dumps({
            "seq": seq,
            "age_sec": age,
            "live": bool(ts) and age is not None and age < 10,
            "bytes": len(frame),
        }).encode("utf-8")
        self._send(200, "application/json", body)

    def _stream(self):
        if _PUMP is None:
            self._send(503, "text/plain; charset=utf-8", "中转未启动".encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = -1
        try:
            while True:
                frame, ts, seq = _PUMP.snapshot()
                if frame and seq != last:
                    last = seq
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(
                        ("Content-Length: %d\r\n\r\n" % len(frame)).encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                else:
                    time.sleep(0.15)  # 没有新帧就等着，别空转烧 CPU
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # OBS 关掉预览是常态，不算错


def serve_stream(pump, port):
    """在 127.0.0.1 起 MJPEG 服务。只绑本机，画面不该被局域网里别人看到。"""
    global _PUMP
    _PUMP = pump
    srv = ThreadingHTTPServer(("127.0.0.1", port), _StreamHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="mjpeg-http", daemon=True).start()
    return srv


# ---------------- MQTT（本地优先，云兜底） ----------------

def format_hms(attr, code):
    """把 HMS 的 attr/code 两个整数拼成拓竹界面/维基上的那种码。

    拓竹显示成 4 组 4 位十六进制，例如 0300_0A00_0002_0001，共 16 位。
    attr 占高 32 位、code 占低 32 位，所以直接 %08X%08X 再按 4 位分组。
    查码时把整串丢进搜索引擎或拓竹 Wiki 的 HMS 页面即可。
    """
    try:
        raw = "%08X%08X" % (int(attr), int(code))
    except (TypeError, ValueError):
        return "%s/%s" % (attr, code)
    return "_".join(raw[i:i + 4] for i in range(0, len(raw), 4))


# HMS 严重级。拓竹的 hms 是当前尚未清除的硬件消息列表，其中混着一类常驻
# 但不影响打印的码，最常见的是 AMS 某个槽的固件和打印机版本不匹配
# （例如 0503_0400_0001_0044，AMS D，装上去之后一直不清除）。
# 不分级的话这类码会让状态常年显示异常，告警徽标常年亮着，用户被训练成
# 直接忽略它，真正出事的那一次反而被淹没。分级如下：
#   noise  只提示，不当报错：不标红、不弹窗、不进告警徽标，只在提示和详情里带一句说明
#   normal 照常当报错（表里没有的码一律按 normal，宁可多响一次，不吞）
HMS_SEVERITY = {
    "0503_0400_0001_0044": ("noise", "AMS D 固件与打印机不匹配，不影响当前打印"),
}

# 常见 HMS 码的中文说明。只收录已经查证过的码，没查证的码不猜含义，
# 编一个假解释比不译更糟，用户照着假解释去拆机器才是真事故。
# 表外的码统一给编号和去哪里查。
HMS_TEXT = {
    "0500_0500_0001_0007": "控制命令未授权（固件 01.08 起第三方暂停/控制需授权）",
    "0503_0400_0001_0044": "AMS 槽位固件与打印机版本不匹配，不影响当前打印",
}

# 打印机报错会自己过期：hms 是"当前未清除的硬件消息列表"，打印机只在有变化
# 时上报，打印结束、断连之后本地这份会一直挂着，界面就永远显示一条早就不存在
# 的错误。超过这个秒数没再被上报，就当已清除。
ERR_TTL_SEC = 180.0


def human_hms(code):
    """一条 HMS 码转中文。未收录的码只说编号，不猜含义。"""
    c = str(code).strip().upper().replace("-", "_")
    text = HMS_TEXT.get(c)
    if text:
        return "%s（编号 %s）" % (text, c)
    return "机器提示，编号 %s（含义未收录，可在拓竹 Wiki 的 HMS 页按编号查）" % c


def human_error(text):
    """把 error_text() 的原始串转成中文。

    原始串形如 "打印错误码 50348032 / HMS 0300_1800_0001_0005"，直接给人看
    等于没提示，看不懂的码只会被当成噪音关掉。
    """
    parts = []
    for seg in str(text).split(" / "):
        seg = seg.strip()
        if not seg:
            continue
        if seg.startswith("HMS "):
            parts.append(human_hms(seg[4:]))
        elif seg.startswith("打印错误码"):
            parts.append("打印任务错误码 %s（可在打印机屏幕或 Bambu Studio 里查详情）"
                         % seg.replace("打印错误码", "").strip())
        else:
            parts.append(seg)
    return "；".join(parts)


class BambuMqtt:
    def __init__(self, cfg, secrets):
        self.serial = cfg["printer"]["serial"]
        self.ip = cfg["printer"]["ip"]
        self.access_code = access_code_for(cfg, secrets)
        mqtt_cfg = cfg.get("mqtt", {})
        self.mode = mqtt_cfg.get("mode", "local_then_cloud")
        self.cloud_broker = mqtt_cfg.get("cloud_broker", "cn.mqtt.bambulab.com")
        self.cloud_user = mqtt_cfg.get("cloud_user", "")
        self.cloud_secrets_path = mqtt_cfg.get("cloud_secrets_path", "")
        self.state = {}
        self.gcode_state = ""
        self.firmware = ""
        self.client = None
        self.seq = 0
        self._cloud_token = ""
        # 实际用上的通道（local / cloud）。写进状态文件，界面才能说清
        # "这条暂停命令是从哪条路发出去的"。
        self.mode_used = ""
        # 打印机自己的错误信号。光看 gcode_state 不够，堵头、缺料、AMS 故障
        # 这些会先体现在 print_error / hms 上，gcode_state 可能还停在 RUNNING。
        self.print_error = 0
        self.hms = []

    def _load_cloud_token(self):
        if self._cloud_token or not self.cloud_secrets_path:
            return self._cloud_token
        try:
            with open(self.cloud_secrets_path, "r", encoding="utf-8") as f:
                self._cloud_token = json.load(f).get("cloud_access_token", "")
        except Exception as e:
            log.warning("读取云令牌失败: %s", e)
        return self._cloud_token

    def _make_client(self, cloud=False):
        c = mqtt.Client(client_id="a1-watchdog-%d" % int(time.time()),
                        protocol=mqtt.MQTTv311)
        if cloud:
            c.username_pw_set(self.cloud_user, self._load_cloud_token())
            host = self.cloud_broker
        else:
            c.username_pw_set("bblp", self.access_code)
            host = self.ip
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        c.tls_set_context(ctx)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        # 本地连不上要快点失败：云兜底才来得及接手，用户也不用干等半分钟。
        # 云通道走公网，留长一点。
        c.connect(host, 8883, 20 if cloud else 8)
        return c

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            log.error("MQTT 连接被拒 rc=%s", rc)
            return
        client.subscribe("device/%s/report" % self.serial)
        self._publish(client, {"pushing": {"sequence_id": "0", "command": "pushall"}})

    def _on_message(self, client, userdata, msg):
        try:
            obj = json.loads(msg.payload.decode("utf-8", "replace"))
        except ValueError:
            return
        p = obj.get("print")
        if isinstance(p, dict):
            self.state.update(p)
            if "gcode_state" in p:
                self.gcode_state = p["gcode_state"]
            self._note_errors(p)
        info = obj.get("info")
        if isinstance(info, dict) and info.get("command") == "get_version":
            for m in info.get("module", []):
                if m.get("name") == "ota":
                    self.firmware = m.get("sw_ver", "")
                    log.info("固件版本: %s（可升级 %s）", self.firmware, m.get("new_ver"))

    def _note_errors(self, p):
        """跟住打印机的错误信号，有变化才记日志。

        注意 hms 是"当前未清除的硬件消息列表"，恢复后会变成空列表，所以
        光看"出现过"不行，得看它是不是还在。print_error 是整数码，0 表示没有。
        """
        changed = False
        if "print_error" in p:
            try:
                code = int(p.get("print_error") or 0)
            except (TypeError, ValueError):
                code = 0
            if code != self.print_error:
                self.print_error = code
                changed = True
            # 还在报同一个码就刷新时间戳，别让 TTL 把一条活着的错误当过期清掉
            if code:
                self._err_seen_ts = time.time()
        if isinstance(p.get("hms"), list):
            now = []
            for item in p["hms"]:
                if isinstance(item, dict):
                    now.append((item.get("attr"), item.get("code")))
            # 每次上报都是全量列表：列表里还有码就说明它此刻仍然有效
            if now:
                self._err_seen_ts = time.time()
            if now != self.hms:
                self.hms = now
                changed = True
        if not changed:
            return
        text = self.error_text()
        level, _note = self.error_severity()
        if text and level == "noise":
            # 常驻但不影响打印的码只记一条，不按 ERROR 输出
            log.info("打印机有常驻提示（不影响打印）：%s", human_error(text))
        elif text:
            log.warning("打印机报错（只记录，不弹窗）：%s", human_error(text))
        else:
            log.info("打印机报错已清除")

    def error_text(self):
        """一行人类可读的错误摘要；没有错误返回空串。

        带 TTL：超过 ERR_TTL_SEC 秒没再被打印机上报，就当它已经清除。
        否则打印结束后仍会一直挂着一条早就不存在的错误。
        """
        if self.print_error or self.hms:
            if time.time() - getattr(self, "_err_seen_ts", 0.0) > ERR_TTL_SEC:
                self.print_error = 0
                self.hms = []
                self._err_seen_ts = 0.0
                log.info("打印机报错已过期，自动清除（超过 %d 秒未再上报）",
                         int(ERR_TTL_SEC))
        parts = []
        if self.print_error:
            parts.append("打印错误码 %s" % self.print_error)
        for attr, code in self.hms:
            parts.append("HMS %s" % format_hms(attr, code))
        return " / ".join(parts)

    def error_severity(self):
        """(级别, 说明)。级别：normal > noise > none。

        print_error（整数错误码）永远算 normal。HMS 查 HMS_SEVERITY 表，
        表里没有的一律按 normal，没见过的码不该被悄悄吞掉，宁可多响一次。
        """
        if self.print_error:
            return "normal", ""
        worst, note = "none", ""
        for attr, code in self.hms:
            sev, text = HMS_SEVERITY.get(format_hms(attr, code), ("normal", ""))
            if sev == "normal":
                return "normal", ""
            if sev == "noise":
                worst, note = "noise", text
        return worst, note

    def _publish(self, client, payload):
        self.seq += 1
        for v in payload.values():
            if isinstance(v, dict) and "sequence_id" in v:
                v["sequence_id"] = str(self.seq)
        client.publish("device/%s/request" % self.serial, json.dumps(payload))

    def connect(self):
        order = []
        if self.mode in ("local", "local_then_cloud"):
            order.append(False)
        if self.mode in ("cloud", "local_then_cloud"):
            order.append(True)
        last_err = None
        for cloud in order:
            try:
                self.client = self._make_client(cloud=cloud)
                self.client.loop_start()
                self.mode_used = "cloud" if cloud else "local"
                log.info("MQTT 已连接（%s通道）", "云" if cloud else "本地")
                return True
            except Exception as e:
                last_err = e
                log.warning("MQTT 连接失败（%s）: %s", "云" if cloud else "本地", e)
        log.error("MQTT 全部连接失败: %s", last_err)
        return False

    def pause(self, timeout=20):
        """暂停打印；本地未生效且配置了云兜底时自动切云通道。

        注意：固件 01.08 及以后对第三方软件的控制命令有授权校验，这里发出的
        裸 pause 会被打印机拒绝并回 HMS 0500_0500_0001_0007 / err_code
        0x05024007。调用点必须受 alert.allow_pause 门控。
        """
        if self.client is None:
            return False
        log.warning("发送暂停命令...")
        self._publish(self.client, {"print": {"sequence_id": "0", "command": "pause"}})
        if self._wait_paused(timeout):
            return True
        if self.mode == "local_then_cloud" and self._load_cloud_token():
            log.warning("本地暂停未生效，改走云 MQTT")
            try:
                cloud = self._make_client(cloud=True)
                cloud.loop_start()
                self._publish(cloud, {"print": {"sequence_id": "0", "command": "pause"}})
                ok = self._wait_paused(timeout)
                cloud.loop_stop()
                cloud.disconnect()
                return ok
            except Exception as e:
                log.error("云暂停失败: %s", e)
        return False

    def _wait_paused(self, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.gcode_state == "PAUSE":
                return True
            time.sleep(0.5)
        return False

    def disconnect(self):
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None


# ---------------- 拓竹云：设备名 + 任务时间 / 耗材 ----------------

# 打印机昵称、已进行时间、已用克数这三样只能从云接口取。
# 本地 8883 的 report 里 64 个字段都没有这三项，固件本身也没有昵称概念，
# get_version 只给 product_name（机型名，如 "Bambu Lab A1"）。用户起的
# 名字和这一炉的用料耗时只存在拓竹账号里，两个接口能拿到：
#     GET {api}/v1/iot-service/api/user/bind
#         name（用户设的名字）/ dev_access_code / print_job（当前任务 id）
#     GET {api}/v1/user-service/my/tasks?deviceId=X&limit=5
#         title / startTime / weight（总克数）/ costTime（预计秒数）
# 域名：中国大陆账号只认 api.bambulab.cn，同一个令牌打 api.bambulab.com
# 一律返回 401 {"code":4,"error":"Please login."}，所以按顺序自动尝试。
CLOUD_API_HOSTS = ("api.bambulab.cn", "api.bambulab.com")
CLOUD_POLL_SEC = 60.0      # 后台轮询间隔
CLOUD_NAME_TTL = 1800.0    # 名字很少改，半小时刷一次
CLOUD_JOB_TTL = 90.0       # 任务详情（startTime/weight）刷得勤一点

# 打印机上报的速度档位 spd_lvl：1 静音 / 2 标准 / 3 运动 / 4 狂暴
# （0 表示还没推过遥测）。spd_mag 是速度百分比，标准档为 100。
# 切片时写进 gcode 或云端的预计总时长只对标准档成立，换档后机器会按当前
# 速度重算剩余时间。所以这一炉总共要多久必须用已进行时间加 mc_remaining_time，
# 不能拿切片那份估计值（运动档 124% 时机器算出的总时长比切片短 18%）。
SPEED_LEVELS = {1: "静音", 2: "标准", 3: "运动", 4: "狂暴"}


_TITLE_TRANS = str.maketrans({
    "，": ",", "、": ",", "；": ";", "：": ":", "（": "(", "）": ")",
    "％": "%", "　": " ", "－": "-", "．": ".",
})


def _norm_title(text):
    """标题归一化：全角标点、空格、大小写、扩展名都不该影响「是不是同一炉」。

    实测坑：本地 subtask_name 是「PETG 0.2mm层高，2层墙，15%填充」（全角逗号），
    云端 title 可能是半角逗号，直接 == 对不上。所以标点归半角 + 删掉所有空白，
    比到字符级别才稳。
    """
    raw = str(text or "").strip()
    for suffix in (".3mf", ".gcode"):
        if raw.lower().endswith(suffix):
            raw = raw[: -len(suffix)]
    return "".join(raw.translate(_TITLE_TRANS).split()).casefold()


class CloudJob:
    """后台线程轮询拓竹云，把「这台机器叫什么、这一炉打了多久、用了多少料」拿回来。

    为什么单开线程：requests 卡住能到 15 秒，放在主循环里会把 AI 检测一起拖住
    （主循环 1 秒一轮，靠它控制暂停时机）。这里只在后台跑，主循环只读快照。
    """

    def __init__(self, cfg, secrets):
        printer = cfg.get("printer", {}) or {}
        mqtt_cfg = cfg.get("mqtt", {}) or {}
        self.serial = str(printer.get("serial") or "").strip()
        self.secrets_path = str(mqtt_cfg.get("cloud_secrets_path") or "").strip()
        self._token = ""
        self._host = ""
        self._lock = threading.Lock()
        self._thread = None
        # 本地 MQTT 报的任务名，用来跟云端 title 对上号（主循环每秒更新）
        self.hint = ""
        self._name = ""
        self._print_job = None
        self._job = {}
        self._name_at = 0.0
        self._job_at = 0.0
        self.error = ""

    # -- 对外 ------------------------------------------------------------
    def start(self):
        if self._thread is not None or not self.serial:
            return
        self._thread = threading.Thread(
            target=self._loop, name="cloud-job", daemon=True)
        self._thread.start()

    def snapshot(self):
        """给 _write_status 用的只读快照，不碰网络。"""
        with self._lock:
            job = dict(self._job)
            return {"name": self._name, "print_job": self._print_job,
                    "job": job, "error": self.error}

    # -- 内部 ------------------------------------------------------------
    def refresh_now(self):
        """同步刷一次。``--once`` 这类没有后台线程的路径用。"""
        try:
            self._refresh_once()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self.error = "%s: %s" % (type(exc).__name__, exc)

    def _loop(self):
        while True:
            try:
                self._refresh_once()
            except Exception as exc:  # noqa: BLE001 - 后台线程不能死
                with self._lock:
                    self.error = "%s: %s" % (type(exc).__name__, exc)
                log.warning("云端任务信息获取失败：%s", exc)
            time.sleep(CLOUD_POLL_SEC)

    def _refresh_once(self):
        now = time.time()
        want_name = not self._name or now - self._name_at > CLOUD_NAME_TTL
        want_job = not self._job or now - self._job_at > CLOUD_JOB_TTL
        if not (want_name or want_job):
            return
        if want_name:
            self._fetch_name()
        if want_job:
            self._fetch_job()
        with self._lock:
            self.error = ""

    def _load_token(self):
        if self._token or not self.secrets_path:
            return self._token
        try:
            with open(self.secrets_path, "r", encoding="utf-8") as f:
                self._token = str(json.load(f).get("cloud_access_token") or "")
        except (OSError, ValueError):
            self._token = ""
        return self._token

    def _get(self, path):
        token = self._load_token()
        if not token:
            raise RuntimeError("没配云令牌（mqtt.cloud_secrets_path）")
        headers = {
            "Authorization": "Bearer " + token,
            "User-Agent": "bambu_network_agent/01.09.05.01",
            "Accept": "application/json",
        }
        hosts = [self._host] if self._host else list(CLOUD_API_HOSTS)
        last = ""
        for host in hosts:
            url = "https://%s%s" % (host, path)
            try:
                resp = requests.get(url, headers=headers, timeout=15)
            except requests.RequestException as exc:
                last = "%s: %s" % (host, exc)
                continue
            if resp.status_code == 200:
                self._host = host
                return resp.json()
            last = "%s HTTP %s" % (host, resp.status_code)
            # 401 = 这个域不认这个账号（大陆账号打 .com），换下一个继续试
        raise RuntimeError(last or "云端无响应")

    def _fetch_name(self):
        data = self._get("/v1/iot-service/api/user/bind")
        found = None
        for item in data.get("devices") or []:
            if str(item.get("dev_id") or "") == self.serial:
                found = item
                break
        with self._lock:
            self._name_at = time.time()
            if found is None:
                # 账号里没绑这台：保留上次的名字，别把界面刷成空白
                return
            self._name = str(found.get("name") or "").strip()
            self._print_job = found.get("print_job")

    def _fetch_job(self):
        data = self._get("/v1/user-service/my/tasks?deviceId=%s&limit=5"
                         % self.serial)
        hits = [h for h in (data.get("hits") or []) if isinstance(h, dict)]
        chosen = None
        # 优先用 bind 里的 print_job 精确命中（这是「打印机当前在跑哪一炉」）
        if self._print_job:
            for item in hits:
                if str(item.get("id")) == str(self._print_job):
                    chosen = item
                    break
        # 退一步按标题对：本地 MQTT 的 subtask_name 和云端 title 是同一串
        if chosen is None and self.hint:
            want = _norm_title(self.hint)
            for item in hits:
                if _norm_title(item.get("title")) == want:
                    chosen = item
                    break
        # 对不上就返回空，不要拿最近一炉的数据充当当前这一炉
        job = {}
        if chosen is not None:
            start_ts = None
            raw_start = str(chosen.get("startTime") or "").strip()
            if raw_start:
                try:
                    start_ts = datetime.fromisoformat(
                        raw_start.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    start_ts = None
            job = {
                "task_id": chosen.get("id"),
                "title": str(chosen.get("title") or ""),
                "start_ts": start_ts,
                "weight_g": chosen.get("weight"),
                "cost_s": chosen.get("costTime"),
                "status": chosen.get("status"),
                "device_name": str(chosen.get("deviceName") or ""),
                # 盘号：打印机 /cache 里同一份 3mf 会按盘存成 _plate_1/2.gcode，
                # 靠它才能选对当前在打的那一盘的 gcode（算实测耗材要用）
                "plate_index": chosen.get("plateIndex"),
                "filaments": [
                    {"type": m.get("filamentType"), "weight_g": m.get("weight")}
                    for m in (chosen.get("amsDetailMapping") or [])
                    if isinstance(m, dict)
                ],
            }
            if job["device_name"] and not self._name:
                self._name = job["device_name"]
        with self._lock:
            self._job = job
            self._job_at = time.time()


# ---------------- Gadget AI 检测 ----------------

# Gadget 的错误码里有两个不能当普通故障重试：
#   OE_FREE_USAGE_LIMIT_REACHED  本月 9 万次免费用完了，重试多少次都没用，得等下个月
#   OE_API_KEY_IP_RESTRICTED     这个公网 IP 已被另一个 key 占用，换 key 也救不回来
# 其余（限流、内部错误）等一会儿再试就行。
GADGET_FATAL = {
    "OE_FREE_USAGE_LIMIT_REACHED": "本月免费额度已用完，等下个计费周期或去 gadgetapi 开付费",
    "OE_API_KEY_IP_RESTRICTED": "这个公网 IP 已被另一个 API key 占用，换回原来那个 key",
    "OE_INVALID_API_KEY": "API key 无效，检查 a1-watchdog.secrets.json",
    "OE_API_KEY_DISABLED": "API key 已被停用，去 octoeverywhere.com 联系支持",
    "OE_API_KEY_BLOCKED_PAYMENT_FAILED": "账号扣款失败，去 gadgetapi 检查账单",
}


class GadgetError(RuntimeError):
    """带上 ErrorType 的 Gadget 错误，不然只能看到一句 'HTTP 429'。"""

    def __init__(self, status, error_type, details):
        self.status = status
        self.error_type = error_type or ""
        self.details = details or ""
        super().__init__("%s（HTTP %s）%s" % (
            self.error_type or "未知错误", status,
            ("：" + self.details) if self.details else ""))

    @property
    def fatal(self):
        return self.error_type in GADGET_FATAL


class GadgetClient:
    def __init__(self, api_key, warning_level=None, pause_level=None, timeout=30):
        self.api_key = api_key
        self.warning_level = warning_level
        self.pause_level = pause_level
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["X-API-Key"] = api_key

    def create_context(self):
        payload = {}
        if self.warning_level is not None:
            payload["WarningConfidenceLevel"] = self.warning_level
        if self.pause_level is not None:
            payload["PauseConfidenceLevel"] = self.pause_level
        r = self.session.post(GADGET_CREATE_CONTEXT_URL, json=payload, timeout=self.timeout)
        self._raise(r)
        d = r.json()
        return {"id": d["ContextId"], "url": d["ProcessRequestUrl"],
                "fallback_url": d["FallbackProcessRequestUrl"], "use_fallback": False}

    @staticmethod
    def _raise(r):
        """把 Gadget 的 JSON 错误体翻成人能看的话，别只抛 'HTTP 429'。"""
        if r.status_code == 200:
            return
        try:
            body = r.json()
        except ValueError:
            body = {}
        raise GadgetError(r.status_code, body.get("ErrorType"), body.get("ErrorDetails"))

    def process(self, context, jpeg_bytes):
        url = context["fallback_url"] if context["use_fallback"] else context["url"]
        r = self.session.post(
            url, files={"attachment": ("snapshot.jpg", jpeg_bytes, "image/jpeg")},
            timeout=self.timeout, allow_redirects=False)
        if r.status_code >= 500:
            context["use_fallback"] = True
        self._raise(r)
        d = r.json()
        interval = d.get("NextProcessIntervalSec") or {}
        return {
            "quality": d.get("PrintQuality"),
            # Score 是原始模型分：0 最好、100 最差，和 PrintQuality 正好相反。
            # 别当成"置信度"显示，98 分不等于 98% 的把握。
            "score": d.get("Score"),
            "warning": bool(d.get("WarningSuggested")),
            "pause": bool(d.get("PauseSuggested")),
            "faster": bool(d.get("FasterInspectionSuggested")),
            "next_min": int(interval.get("Minimum") or 5),
            "next_rec": int(interval.get("Recommended") or 20),
        }


# ---------------- 本地 AI 检测（klipper TFLite） ----------------

# 本地模型路径：环境变量 A1_WATCHDOG_MODEL 优先，其次 models 目录里认得的文件名，
# 再其次 models 目录里任意一个 tflite / onnx。
# 仓库不带模型权重（见 README 的「模型从哪来」），要自己放。
MODEL_DIR = BASE_DIR / "models"
MODEL_PREFERRED = ("spaghetti_yolov8n_4cls.tflite", "spagcls.onnx")
MODEL_SUFFIXES = (".tflite", ".onnx")


def resolve_model_path() -> Path:
    env = os.environ.get("A1_WATCHDOG_MODEL", "").strip()
    if env:
        return Path(env)
    for name in MODEL_PREFERRED:
        candidate = MODEL_DIR / name
        if candidate.exists():
            return candidate
    if MODEL_DIR.is_dir():
        found = sorted(f for f in MODEL_DIR.iterdir()
                       if f.suffix.lower() in MODEL_SUFFIXES)
        if found:
            return found[0]
    return MODEL_DIR / MODEL_PREFERRED[0]


LOCAL_MODEL_PATH = resolve_model_path()


def detector_model_path(cfg):
    """build_detector 实际会去加载的那个模型文件，用来在起进程前先判存在与否。"""
    dcfg = (cfg.get("detection") or {}) if isinstance(cfg, dict) else {}
    p = dcfg.get("model_path") or None
    return Path(p) if p else LOCAL_MODEL_PATH
LOCAL_NAMES = ["Spaghetti", "Blob", "Warping", "Crack"]


class LocalDetector:
    """本地 TFLite 检测器，接口与 GadgetClient 一致，可整体互换。

    云端单帧往返 2.5 到 5 秒，本地约 240 ms。云端要连续多帧才给暂停建议，
    从炒面出现到敢停要十几秒；本地每 1 到 2 秒检一次，连续两帧命中就能停。

    阈值：warn 0.30 以上只存帧、记日志、界面标观察中，不动打印；
    pause 0.40 以上且连续 2 帧才真暂停。轻微炒面常常下一帧就自修复，
    阈值定太低会把健康打印误停。
    """

    def __init__(self, model_path=None, pause_threshold=0.40,
                 warn_threshold=0.30, next_rec=2, input_size=640):
        import numpy as np
        from PIL import Image
        from ai_edge_litert.interpreter import Interpreter

        self.model_path = Path(model_path) if model_path else LOCAL_MODEL_PATH
        if not self.model_path.exists():
            raise FileNotFoundError("本地模型不存在: %s" % self.model_path)
        self._np = np
        self._Image = Image
        self.names = LOCAL_NAMES
        self.spag_id = LOCAL_NAMES.index("Spaghetti")
        self.blob_id = LOCAL_NAMES.index("Blob")
        self.pause_threshold = float(pause_threshold)
        self.warn_threshold = float(warn_threshold)
        self.next_rec = int(next_rec)
        self.input_size = int(input_size)
        # ai_edge_litert 的 C 层按 ANSI 打开文件，路径带中文（装在中文目录下）
        # 会报 model allocation is null/empty。这种路径改成内存加载，绕开它自己
        # 的 fopen。
        try:
            str(self.model_path).encode("ascii")
            self._it = Interpreter(model_path=str(self.model_path))
        except UnicodeEncodeError:
            self._it = Interpreter(model_content=self.model_path.read_bytes())
        self._it.allocate_tensors()
        self._inp = self._it.get_input_details()[0]
        self._out = self._it.get_output_details()[0]
        log.info("本地检测器就绪: %s（暂停阈值 %.3f / 警告阈值 %.3f / 每 %ds 一检）",
                 self.model_path.name, self.pause_threshold,
                 self.warn_threshold, self.next_rec)

    def create_context(self):
        # 本地模型没有"上下文"概念，返回占位符保持接口一致
        return {"id": "local", "url": "", "fallback_url": "", "use_fallback": False}

    def _infer(self, jpeg_bytes):
        """返回 {class_id: 最高置信度}"""
        import io
        np, Image = self._np, self._Image
        im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        im = im.resize((self.input_size, self.input_size), Image.BILINEAR)
        x = (np.asarray(im, dtype=np.float32) / 255.0)[None]     # NHWC
        self._it.set_tensor(self._inp["index"], x)
        self._it.invoke()
        raw = self._it.get_tensor(self._out["index"])[0]        # [8, 8400]
        if raw.shape[0] < raw.shape[1]:
            raw = raw.T                                           # [8400, 8]
        best = {}
        for row in raw:
            cls = row[4:]                                         # 4 个类别分
            if len(cls) != len(self.names):
                continue
            k = int(np.argmax(cls))
            c = float(cls[k])
            if c > best.get(k, 0.0):
                best[k] = c
        return best

    def process(self, context, jpeg_bytes):
        best = self._infer(jpeg_bytes)
        spag = best.get(self.spag_id, 0.0)
        blob = best.get(self.blob_id, 0.0)
        pause = spag >= self.pause_threshold
        warning = (not pause) and spag >= self.warn_threshold
        return {
            # quality 和 Gadget 同向：10 最好、0 最差
            "quality": int(round((1.0 - spag) * 10)),
            # score 和 Gadget 同向：0 最好、100 最差
            "score": int(round(spag * 100)),
            "warning": bool(warning),
            "pause": bool(pause),
            "faster": False,
            "next_min": 1,
            "next_rec": self.next_rec,
            # 本地模型独有的细粒度字段，日志和界面可以直接显示
            "spag_conf": round(spag, 4),
            "blob_conf": round(blob, 4),
        }


class OnnxSpagDetector(LocalDetector):
    """开源分类模型（2 类 clean / spaghetti）的本地检测器，接口与 LocalDetector 一致。

    与 LocalDetector 的差别：
    - 二分类，没有检测框，只有一个炒面概率
    - 等比缩放、抗锯齿降采样、中心裁剪都烧进 ONNX 图里（图内用两个预计算的
      权重矩阵做降采样），所以这里只把画面等比缩放并居中填充到模型声明的
      输入尺寸，再原样转成 uint8 NCHW
    - 输入尺寸固定，换分辨率要重新导出模型

    onnxruntime 同样按 ANSI 打开模型文件，所以用内存字节建 session。
    """

    def __init__(self, model_path=None, pause_threshold=0.40,
                 warn_threshold=0.30, next_rec=2, input_size=None):
        import numpy as np
        from PIL import Image
        import onnxruntime as ort

        self.model_path = Path(model_path) if model_path else LOCAL_MODEL_PATH
        if not self.model_path.exists():
            raise FileNotFoundError("本地模型不存在: %s" % self.model_path)
        self._np = np
        self._Image = Image
        self.names = ["clean", "spaghetti"]
        self.spag_id = 1
        self.blob_id = -1          # 没有 blob 类别，父类 process 会取到 0.0
        self.pause_threshold = float(pause_threshold)
        self.warn_threshold = float(warn_threshold)
        self.next_rec = int(next_rec)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
        opts.log_severity_level = 3
        self._sess = ort.InferenceSession(
            self.model_path.read_bytes(), opts,
            providers=["CPUExecutionProvider"])
        self._inp = self._sess.get_inputs()[0]
        shape = list(self._inp.shape)
        self.input_hw = (int(shape[2]), int(shape[3]))
        self.input_size = self.input_hw[0]
        log.info("本地检测器就绪: %s（onnx 分类 %dx%d，暂停阈值 %.3f，"
                 "警告阈值 %.3f，每 %ds 一检）",
                 self.model_path.name, self.input_hw[1], self.input_hw[0],
                 self.pause_threshold, self.warn_threshold, self.next_rec)

    def _infer(self, jpeg_bytes):
        """返回 {1: 炒面概率}，与父类 process 的读取方式对齐。"""
        np, Image = self._np, self._Image
        import io
        im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        ah, aw = self.input_hw
        if im.size != (aw, ah):
            scale = max(aw / float(im.size[0]), ah / float(im.size[1]))
            nw = max(aw, int(round(im.size[0] * scale)))
            nh = max(ah, int(round(im.size[1] * scale)))
            resized = im.resize((nw, nh), Image.BILINEAR)
            canvas = Image.new("RGB", (aw, ah), (0, 0, 0))
            canvas.paste(resized, ((aw - nw) // 2, (ah - nh) // 2))
            im = canvas
        x = np.asarray(im, dtype=np.uint8).transpose(2, 0, 1)[None]
        out = self._sess.run(None, {self._inp.name: x})[0]
        probs = np.asarray(out, dtype=np.float32).reshape(-1)
        spag = float(probs[self.spag_id]) if probs.size > self.spag_id else 0.0
        return {self.spag_id: spag}


class BothDetector:
    """本地主判，灰区再问云端（detection.engine = "both" 时用）。

    本地出结论快，炒面和正常都由它定。云端慢但跨域更稳，只在本地只给出
    警告、拿不准的时候花几秒复核一次。本地说停就停，本地说没事就没事。
    """

    def __init__(self, local, gadget):
        self.local = local
        self.gadget = gadget
        self.names = local.names

    def create_context(self):
        return {"local": self.local.create_context(),
                "gadget": self.gadget.create_context()}

    def process(self, context, jpeg):
        r = self.local.process(context["local"], jpeg)
        r["decided_by"] = "local"
        if r["pause"] or not r["warning"]:
            return r
        try:
            g = self.gadget.process(context["gadget"], jpeg)
        except Exception as e:
            log.warning("云端复核失败，按本地结论办: %s", e)
            return r
        g["decided_by"] = "gadget"
        return g


def build_detector(cfg, secrets, engine):
    """按 detection.engine 造检测器。run() 和 main() 共用这一个出口。"""
    engine = str(engine or "local").lower()
    if engine not in ("local", "gadget", "both"):
        raise ValueError("detection.engine 只能是 local / gadget / both，现在是 %r" % engine)
    dcfg = cfg.get("detection", {}) or {}
    model_path = dcfg.get("model_path") or None
    path = Path(model_path) if model_path else LOCAL_MODEL_PATH
    det_cls = OnnxSpagDetector if path.suffix.lower() == ".onnx" else LocalDetector
    local = det_cls(
        model_path=path,
        pause_threshold=dcfg.get("local_pause_threshold", 0.40),
        warn_threshold=dcfg.get("local_warn_threshold", 0.30),
        next_rec=dcfg.get("local_next_rec", 2))
    if engine == "local":
        return local
    gcfg = cfg.get("gadget", {}) or {}
    gadget = GadgetClient(
        secrets["gadget_api_key"],
        warning_level=gcfg.get("warning_confidence_level"),
        pause_level=gcfg.get("pause_confidence_level"))
    if engine == "gadget":
        return gadget
    return BothDetector(local, gadget)


# ---------------- 报警与存帧 ----------------

def save_frame(jpeg, tag):
    FRAMES_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = FRAMES_DIR / ("%s-%s.jpg" % (ts, tag))
    path.write_bytes(jpeg)
    return path


LATEST_PATH = FRAMES_DIR / "latest.jpg"


def save_latest(jpeg):
    """把最新一帧覆盖写到 frames/latest.jpg，供打印工作台显示预览。

    拓竹固件同一时刻只允许一个摄像头连接，所以由看门狗独占摄像头，
    顺手留一张最新帧给界面读。

    写文件要走原子替换：界面可能正在读，读到写了一半的 JPEG 会显示裂图。
    先写 .tmp 再 os.replace。
    """
    FRAMES_DIR.mkdir(exist_ok=True)
    tmp = LATEST_PATH.with_name("latest.jpg.tmp")
    try:
        tmp.write_bytes(jpeg)
        os.replace(tmp, LATEST_PATH)
    except OSError:
        pass


def popup(title, detail):
    """Windows 弹窗报警（best effort，失败只写日志）。"""
    try:
        ps = "power" + "shell.exe"
        subprocess.run(
            [ps, "-NoProfile", "-Command",
             "msg * /time:90 %s - %s" % (title, detail)],
            timeout=10, capture_output=True)
    except Exception:
        pass


# ---------------- 微信推送 ----------------

class Pusher:
    """报警推到手机，不要服务端、不要公网 IP。

    纯客户端直连第三方转发服务：报警时 POST 一条消息出去，微信那边由对方
    的公众号/服务号负责落地。所以本机断网就发不出，也不需要开端口。
    四个通道任选一个，按 push.type 分派：

      serverchan  Server酱  https://sct.ftqq.com  扫码取 SendKey，免费 5 条/天
      pushplus    PushPlus https://pushplus.plus  扫码取 token，免费 200 条/天
      wecom       企业微信群机器人  建群拿 webhook 地址，无限量，装企业微信 App 收
      wxpusher    WxPusher https://wxpusher.zjiecode.com  关注公众号取 UID，近乎无限

    为什么不用自己的公众号/小程序：个人主体拿不到长期订阅消息权限，
    每次推送都要用户点一次授权，跟"睡到半夜被炒面叫醒"这种场景天然冲突。
    详见 README「微信推送为什么用第三方通道」。
    """

    TYPES = ("serverchan", "pushplus", "wecom", "wxpusher")

    def __init__(self, cfg, secrets=None):
        p = (cfg.get("push") or {})
        self.type = str(p.get("type") or "").strip().lower()
        # key 优先取 secrets.json 的 push_key：config 是要发给别人看的，
        # 谁把自己的 token 写进 config 再截图求助，token 就泄了。
        self.key = str((secrets or {}).get("push_key") or p.get("key") or "").strip()
        self.uids = [str(u) for u in (p.get("uids") or [])]
        self.timeout = float(p.get("timeout_sec") or 8)
        # 兜底限流：同一标题最短间隔。检测循环可能连续抛多条，别把当天
        # 免费额度（Server酱 5 条/天）一次刷干净。
        self.min_interval = float(p.get("min_interval_sec") or 0)
        self.enabled = bool(p.get("enabled", False))
        if self.enabled and (self.type not in self.TYPES or not self.key):
            log.warning("push.enabled=true 但 type/key 不完整（type=%r key=%s字节），"
                        "推送已停用", self.type, len(self.key))
            self.enabled = False
        self._lock = threading.Lock()
        self._last_sent = {}

    def _post(self, title, body):
        """同步发一条，返回 (是否成功, 说明)。不抛异常。"""
        t = self.type
        try:
            if t == "serverchan":
                url = "https://sctapi.ftqq.com/%s.send" % self.key
                r = requests.post(url, data={"title": title, "desp": body},
                                  timeout=self.timeout)
                j = r.json()
                return (j.get("code") == 0,
                        "HTTP %s %s" % (r.status_code, j.get("message") or ""))
            if t == "pushplus":
                url = "https://www.pushplus.plus/send"
                r = requests.post(url, json={
                    "token": self.key, "title": title,
                    "content": body, "template": "markdown"}, timeout=self.timeout)
                j = r.json()
                return (j.get("code") == 200,
                        "HTTP %s %s" % (r.status_code, j.get("msg") or ""))
            if t == "wecom":
                # key 直接是完整 webhook 地址（企业微信给的就是一整条 URL）
                url = self.key if self.key.startswith("http") else \
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=%s" % self.key
                content = "### %s\n%s" % (title, body)
                r = requests.post(url, json={
                    "msgtype": "markdown",
                    "markdown": {"content": content}}, timeout=self.timeout)
                j = r.json()
                return (j.get("errcode") == 0,
                        "HTTP %s %s" % (r.status_code, j.get("errmsg") or ""))
            if t == "wxpusher":
                url = "https://wxpusher.zjiecode.com/api/send/message"
                r = requests.post(url, json={
                    "appToken": self.key, "content": body, "summary": title,
                    "contentType": 3, "uids": self.uids}, timeout=self.timeout)
                j = r.json()
                return (j.get("code") == 1000,
                        "HTTP %s %s" % (r.status_code, j.get("msg") or ""))
        except Exception as exc:
            return False, "%s: %s" % (type(exc).__name__, exc)
        return False, "未知通道 %r" % t

    def _send_bg(self, title, body):
        ok, info = self._post(title, body)
        if ok:
            log.info("微信推送已发出：%s（%s）", title, info)
        else:
            log.warning("微信推送失败：%s（%s）", title, info)

    def send(self, title, body, dedupe_key=""):
        """异步推一条。绝不阻塞调用方，requests 卡 8 秒会把检测循环拖废。"""
        if not self.enabled:
            return False
        now = time.time()
        k = dedupe_key or title
        with self._lock:
            if now - self._last_sent.get(k, 0.0) < self.min_interval:
                log.info("微信推送限流，跳过：%s", title)
                return False
            self._last_sent[k] = now
        threading.Thread(target=self._send_bg, args=(title, body),
                         daemon=True).start()
        return True


# ---------------- 看门狗主逻辑 ----------------

class Watchdog:
    PRINTING_STATES = ("PREPARE", "RUNNING", "PAUSE")

    def __init__(self, cfg, secrets, dry_run=False):
        self.cfg = cfg
        self.secrets = secrets
        det = cfg.get("detection", {}) or {}
        # 检测引擎：local（本地 TFLite，默认）/ gadget（OctoEverywhere 云端）/ both
        # 选 local 的原因见 LocalDetector 上面的注释：云端要 17~22 秒才敢暂停。
        self.engine = str(det.get("engine", "local")).lower()
        if self.engine not in ("local", "gadget", "both"):
            raise ValueError("detection.engine 只能是 local / gadget / both，现在是 %r"
                             % self.engine)
        # 只有 gadget/both 才需要 API key；local 纯本地跑，没 key 也不该降级成 dry-run
        needs_key = self.engine in ("gadget", "both")
        self.dry_run = dry_run or (needs_key and not secrets.get("gadget_api_key"))
        # 没模型不等于跑不起来：降级成「只取画面、存帧、看进度」，不做判定也不自动暂停。
        # 仓库不带模型权重（授权与体积），刚克隆下来的人第一次启动必然走到这里，
        # 与其抛 traceback 不如说清缺什么、放哪儿。
        self.detector_skip_reason = ""
        if self.dry_run:
            self.detector_skip_reason = "没配云端 API key"
        elif self.engine in ("local", "both"):
            _mp = detector_model_path(cfg)
            if not _mp.exists():
                self.dry_run = True
                self.detector_skip_reason = "本地检测模型不在：%s" % _mp
        # 连续几帧说"该暂停"才真暂停。默认值按引擎区分：
        #   local  → 20：本地模型单帧就可能误判，底座纹理、亮面、大块同色都能把
        #            分数顶过阈值，有时候只闪一两帧。要它连续二十次都判异常才认，
        #            每 2s 一检差不多 40 秒，真炒面撑得住这么久的观察。
        #            嫌迟钝就把 detection.pause_after_consecutive 调小（1 到 5 之间）。
        #   gadget → 1：Gadget 自己就是「图像分类 + 时序模型」，单帧异常只会压低
        #            质量分，要连续 3~4 帧才给 WarningSuggested、4~5 帧才给
        #            PauseSuggested，外面再套一层是重复门禁，只会白白推迟暂停。
        # 想覆盖默认就在配置里显式写 pause_after_consecutive。
        self.pause_after = int(det.get(
            "pause_after_consecutive", 20 if self.engine == "local" else 1))
        # 是否允许本程序向打印机发控制命令（目前只有 pause 一条）。
        # 默认 False：本软件的定位是监控与报警，控制权不在这里。原因见
        # 文件头「本软件不发送任何控制命令」与 README「关于自动暂停」。
        self.allow_pause = bool((cfg.get("alert") or {}).get("allow_pause", False))
        # 趋势检测，分数统一 0 到 100，越大越差。
        # 只对本地引擎开放：本地分数语义稳定，爬升就是真变重。云端 gadget
        # 分数抖动很大，同一炉健康打印能在 0 到 67 之间来回跳，拿它算斜率
        # 会被噪声骗成恶化并误停。gadget 引擎强制关闭趋势，只信它自己的
        # Warning / Pause 建议。
        if self.engine == "gadget":
            self.trend_enabled = False
        else:
            self.trend_enabled = bool(det.get("trend_enabled", True))
        # 本地趋势门槛，仅 local / both 生效。要求明显且持续地变重才停，
        # 避免单帧抖动或能自修复的轻微炒面误停。趋势线看的是 EMA 平滑值
        # 加持续爬升，单帧尖峰顶不上去。
        self.trend_window = float(det.get("trend_window_sec", 180))
        self.trend_slope_per_min = float(det.get("trend_slope_per_min", 20.0))
        self.trend_min_score = float(det.get("trend_min_score", 40.0))
        self.trend_min_points = int(det.get("trend_min_points", 6))
        # 趋势线也要连续够久才动手：单帧抖动加两个点就能凑出斜率，
        # 门槛跟阈值线对齐到 20 次，两条线都不容易靠噪声触发。
        self.trend_hold = int(det.get("trend_hold", 20))
        self._trend_hist = []
        self._score_ema = None
        self.consecutive_escalation = 0
        self.last_trend = None
        self.min_interval = int(det.get("min_interval_sec", 5))
        self.max_interval = int(det.get("max_interval_sec", 60))
        self.inspect_while_paused = bool(det.get("inspect_while_paused", False))
        self.camera = BambuCamera(cfg["printer"]["ip"], access_code_for(cfg, secrets))
        # 摄像头由 pump 独占并持续读；检测线程和 MJPEG 中转都从它拿帧，
        # 绝不能有两处同时读同一个 socket。
        self.pump = FramePump(self.camera)
        self._inspected_seq = -1
        self.stream_cfg = cfg.get("stream", {}) or {}
        self.alert_printer_error = bool(
            (cfg.get("alert", {}) or {}).get("printer_error", True))
        self._alerted_error = ""
        # 手机推送：只在报警时发一条，发在后台线程里，配置见 config 的 push 段。
        self.pusher = Pusher(cfg, secrets)
        if self.pusher.enabled:
            log.info("微信推送已启用：通道 %s", self.pusher.type)
        self.mq = BambuMqtt(cfg, secrets)
        # 云端任务信息：打印机自己的名字、这一炉的起始时刻、总用料。
        # 本地 MQTT 的 64 个字段里一样都没有，只能问拓竹账号（见 CloudJob）。
        self.cloud = CloudJob(cfg, secrets)
        # 实测账本：已进行时间靠本地秒级计时，已用耗材靠解析 gcode 的挤出量。
        # 两个都不能用「进度 × 总量」折算（见 job_metrics 模块头）。
        metrics_cfg = cfg.get("metrics", {}) or {}
        self.clock = job_metrics.JobClock(BASE_DIR)
        self.usage = job_metrics.GcodeUsage(
            BASE_DIR, cfg["printer"]["ip"], access_code_for(cfg, secrets),
            str(cfg["printer"].get("serial") or ""), log)
        self.gcode_usage_enabled = bool(metrics_cfg.get("gcode_usage", True))
        self._clock_view = self.clock.view()
        self.gadget = None
        self.context = None
        self.session_active = False
        self.consecutive_pause = 0
        self.next_inspect = 0.0
        self.paused_by_us = False
        # 给打印工作台看的一组字段（写进 STATUS_PATH）：
        # started_at/pid 用来判断进程是不是还活着，checks/failures 用来算
        # "这一炉被看了多少次、报了回故障"，last_* 是最近一次 AI 结论。
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.checks = 0
        self.failures = 0
        # AI 巡检开关的上一轮状态，用来只在切换时打一条日志
        self.ai_off_prev = None
        self.last_result = None
        self.last_check_at = ""
        self.last_frame = ""
        # Gadget 侧的故障（额度用完、key 失效…）。和"这一帧检测失败"不是一回事，
        # 得单独存下来给界面显示，否则用户只看到检测停住却不知道为什么。
        self.last_error = ""
        # 警告帧限流时间戳。轻微炒面（warn 级）可能持续几分钟，每 2s 存一张图
        # 会让 frames 目录迅速变大，所以 warn 帧最多每 60 秒存一张，pause 帧不限制。
        self._last_warn_saved = 0.0
        # 本次判定是不是被「已接受程度」压住了。只影响界面徽标，
        # 不改 AI 自己的结论，原始建议仍写进 last_pause。
        self.ack_suppressed = False

    def _start_session(self):
        self.session_active = True
        # 新打印 = 新的判定标准：上一炉接受的宽松程度不能带过来
        self._clear_ack()
        self.ack_suppressed = False
        self.consecutive_pause = 0
        self._inspected_seq = -1
        self.pump.start()  # 打印中才占摄像头，不打印时让给 Studio / 手机 App
        if not self.dry_run and self.context is None:
            try:
                self.context = self.gadget.create_context()
                self.last_error = ""
                log.info("Gadget 检测上下文已创建: %s", self.context["id"])
            except GadgetError as e:
                self.last_error = GADGET_FATAL.get(e.error_type, str(e))
                log.error("创建 Gadget 上下文失败: %s", self.last_error)
            except Exception as e:
                self.last_error = str(e)
                log.error("创建 Gadget 上下文失败: %s", e)
        log.info("=== 打印开始，进入监控（dry_run=%s）===", self.dry_run)

    def _end_session(self):
        if self.session_active:
            log.info("=== 打印结束，退出监控 ===")
        self.session_active = False
        self.context = None
        self.consecutive_pause = 0
        self.consecutive_escalation = 0
        self.paused_by_us = False
        # 趋势状态跟着会话走：新打印从零开始建基线，不继承上一炉的历史
        self._trend_hist = []
        self._score_ema = None
        self.last_trend = None
        self.ack_suppressed = False
        self.pump.stop()  # 连同 camera.close() 一起做

    def _trend_slope(self):
        """平滑分数序列的线性斜率，单位：分数点/分钟。样本不够返回 None。

        为什么要按时间而不是按检次归一：本地引擎 2s 一检、云端 12s 一检，
        按检次算斜率两者差 6 倍，没法用同一个阈值。按分钟算才跨引擎一致。
        """
        n = len(self._trend_hist)
        if n < 3:
            return None
        t0 = self._trend_hist[0][0]
        xs = [(t - t0) / 60.0 for t, _ in self._trend_hist]
        ys = [s for _, s in self._trend_hist]
        mx = sum(xs) / n
        my = sum(ys) / n
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = sum((x - mx) ** 2 for x in xs)
        if den <= 0:
            return None
        return num / den

    def _update_trend(self, score):
        """喂一个检测分数进去，返回 (斜率, 是否在恶化)。

        策略来自用户原则：炒一点可以接受，越来越重不能接受。
        所以暂停的第二条触发线是"趋势"：平滑分数本身已经不低（≥
        trend_min_score）并且还在稳定爬升（斜率 ≥ trend_slope_per_min），
        连续 trend_hold 次都这样才停。稳定的轻度（比如一直 0.3 左右）
        斜率为 0，不会误停；轻微自修复的分数会往下走，更不会停。
        """
        # EMA 平滑系数 0.7/0.3（偏平滑，抗单帧抖动；本地模型没训好时很重要）
        self._score_ema = (score if self._score_ema is None
                           else self._score_ema * 0.7 + score * 0.3)
        now = time.time()
        if self.trend_enabled:
            self._trend_hist.append((now, self._score_ema))
            # 只保留最近 trend_window 秒的样本，炒面是"最近在变重"不是"五分钟前高"
            self._trend_hist = [(t, s) for t, s in self._trend_hist
                                if now - t <= self.trend_window]
        slope = self._trend_slope()
        escalating = bool(
            self.trend_enabled and slope is not None
            and len(self._trend_hist) >= self.trend_min_points
            and self._score_ema >= self.trend_min_score
            and slope >= self.trend_slope_per_min)
        self.consecutive_escalation = (self.consecutive_escalation + 1
                                       if escalating else 0)
        self.last_trend = {
            "ema": round(self._score_ema, 1),
            "slope_per_min": round(slope, 2) if slope is not None else None,
            "samples": len(self._trend_hist),
            "escalating": escalating,
        }
        return slope, escalating

    def _inspect_once(self):
        # 从 pump 等一帧「比上次检测更新」的画面，语义和原来阻塞式读一样，
        # 但不独占 socket，OBS 的 MJPEG 流同时在读同一个 pump 的缓存。
        jpeg, ts, seq = self.pump.wait_frame(self._inspected_seq)
        if not jpeg:
            raise ConnectionError("还没有拿到任何画面")
        self._inspected_seq = seq
        # 先落一张最新帧：不管这次检测成不成功，界面都有东西可看。
        save_latest(jpeg)
        self.last_frame = LATEST_PATH.name
        if self.dry_run:
            log.info("[dry-run] 取到帧 %d 字节（%s，跳过 AI 检测）",
                     len(jpeg), self.detector_skip_reason or "dry-run")
            self.next_inspect = time.time() + self.max_interval
            return None
        result = self.gadget.process(self.context, jpeg)
        self.last_result = result
        self.last_check_at = datetime.now().isoformat(timespec="seconds")
        self.checks += 1
        if result["pause"]:
            self.consecutive_pause += 1
        else:
            self.consecutive_pause = 0
        slope, escalating = self._update_trend(float(result.get("score") or 0))
        trend_pause = escalating and self.consecutive_escalation >= self.trend_hold
        trend_txt = ("%+.1f/分钟%s" % (slope, "↑恶化" if escalating else "")
                     if slope is not None else "样本不足")
        log.info("检测: 质量=%s 分数=%s 警告=%s 暂停建议=%s"
                 "（连续暂停信号 %d/%d，趋势 %s，EMA %.1f，下次约 %ss）",
                 result["quality"], result["score"], result["warning"],
                 result["pause"], self.consecutive_pause, self.pause_after,
                 trend_txt, self._score_ema or 0.0, result["next_rec"])
        if result["warning"] or result["pause"] or trend_pause:
            tag = "pause" if (result["pause"] or trend_pause) else "warn"
            now = time.time()
            # pause 帧必存（证据）；warn 帧限流 60s 一张，够复盘趋势就行
            if result["pause"] or trend_pause or now - self._last_warn_saved >= 60:
                if not (result["pause"] or trend_pause):
                    self._last_warn_saved = now
                path = save_frame(jpeg, tag)
                log.info("已保存问题帧: %s", path)
        # 两条暂停触发线，任一命中就停：
        #   1. 阈值线：分数够高且连续 pause_after 帧（应对"已经很严重的炒面"）
        #   2. 趋势线：分数不算爆表但持续爬升（应对"越来越重"），连续
        #      trend_hold 次都在恶化才停。稳定的轻度炒面斜率为 0，不会误停
        # 人眼已经接受过这个程度：只有更严重才再提示。同一程度不再弹窗、
        # 不再推送也不再发暂停命令，用户已经明确表态这卷料可以接受。
        ack = self._ack_score()
        score_now = float(result.get("score") or 0)
        self.ack_suppressed = bool(ack is not None and score_now < ack + ACK_MARGIN)
        if self.ack_suppressed:
            log.info("已接受过 %.0f 分，本次 %.0f 分没更严重，不报警",
                     ack, score_now)
        elif result["pause"] and self.consecutive_pause >= self.pause_after:
            self._trigger_pause(jpeg, result, reason="阈值")
            self.consecutive_pause = 0
        elif trend_pause:
            self._trigger_pause(jpeg, result, reason="趋势")
            self.consecutive_escalation = 0
        interval = max(self.min_interval, min(self.max_interval, result["next_rec"]))
        self.next_inspect = time.time() + interval
        return result

    def _push_body(self, detail, extra=""):
        """拼推送正文。四通道都按 markdown 渲染。"""
        p = self.cfg.get("printer") or {}
        name = str(p.get("name") or p.get("serial") or "打印机")
        lines = [
            "打印机：%s（%s）" % (name, p.get("ip") or ""),
            "详情：%s" % detail,
            "时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ]
        if extra:
            lines.append("状态：%s" % extra)
        return "\n\n".join(lines)

    def _alert_errors(self):
        """打印机自己报的错只写日志，不弹窗也不推送。

        长任务里 AMS 堵料、耗材快用完这类报错很常见，每次都弹会让人学会无视，
        真正重要的那条也就被淹没了。弹窗和推送只留给炒面检测。
        """
        text = self.mq.error_text()
        if not text:
            self._alerted_error = ""
            return
        if text == self._alerted_error:
            return
        self._alerted_error = text
        log.warning("打印机报错（只记录，不弹窗）：%s", text)

    def _popup_detail(self, result, path, reason):
        """弹窗正文用中文描述严重程度，不直接摆分数和错误码。"""
        score = int(result.get("score") or 0)
        if score >= 70:
            head = "很可能已经出现炒面（喷头堆料、掉件或层错位）"
        elif score >= 40:
            head = "疑似出现打印异常，正在持续观察"
        else:
            head = "有轻微异常迹象"
        lines = [head, "异常程度 %d/100（越高越严重）" % score]
        if reason == "趋势":
            t = self.last_trend or {}
            lines.append("最近几分钟异常在持续加重（每分钟 %+.1f）" % (t.get("slope_per_min") or 0))
        lines.append("如果看到喷头堆料或模型被带倒，请在打印机屏幕上按暂停。")
        lines.append("如果只是表面毛糙，可以先不动，继续观察。")
        lines.append("现场照片已存到：%s" % path)
        return "\n".join(lines)

    def _trigger_pause(self, jpeg, result, reason="阈值"):
        path = save_frame(jpeg, "FAILED")
        self.failures += 1
        detail = "质量=%s 分数=%s 帧=%s" % (result["quality"], result["score"], path.name)
        if reason == "趋势":
            t = self.last_trend or {}
            detail += "（分数持续上升 %+.1f/分钟，越来越重）" % (t.get("slope_per_min") or 0)
        log.error("!!! 检测到打印故障（%s触发）：%s", reason, detail)
        popup("打印可能失败", self._popup_detail(result, path, reason))
        if not self.allow_pause:
            log.error("自动暂停未启用（alert.allow_pause=false）。"
                      "请立即到打印机上手动暂停，或开启开发者模式后用官方工具。")
            state = "自动暂停未启用，请立刻手动暂停"
        elif self.mq.pause():
            log.info("已自动暂停打印")
            self.paused_by_us = True
            state = "已发送暂停命令"
        else:
            log.error("自动暂停失败！请立即手动暂停！")
            state = "自动暂停失败，请立刻手动暂停"
        # 推送放在暂停之后：手机上要看到的正是"停没停成"。
        self.pusher.send("A1 炒面警报",
                         self._push_body(detail, "%s，帧文件 %s" % (state, path)))

    def run(self):
        if not self.mq.connect():
            return 1
        self.cloud.start()   # 后台线程，只读云，不占主循环
        if self.dry_run:
            log.warning("AI 检测没有开：%s。看门狗照常连打印机、存帧、报进度，"
                        "但不会判定炒面。放好模型（%s）或把 detection.engine 改成 gadget 后重启即可。",
                        self.detector_skip_reason, MODEL_DIR)
        else:
            try:
                self.gadget = build_detector(self.cfg, self.secrets, self.engine)
            except Exception as e:
                self.dry_run = True
                self.gadget = None
                self.detector_skip_reason = "检测器起不来：%s" % e
                log.error("AI 检测起不来（%s）。看门狗继续跑，只取画面不判定。"
                          "先 pip install -r requirements.txt，再确认模型文件能打开。", e)
        log.info("看门狗已启动，等待打印开始...（Ctrl+C 退出）")
        srv = None
        if self.stream_cfg.get("enabled", True):
            port = int(self.stream_cfg.get("port", 8099))
            try:
                srv = serve_stream(self.pump, port)
                log.info("画面中转已开启: http://127.0.0.1:%d/ （OBS 用浏览器源指这里）",
                         port)
            except OSError as e:
                # 端口被占不该拖垮监控本身，AI 检测比看画面重要。
                log.warning("画面中转启动失败（端口 %d 可能被占用）: %s", port, e)
        try:
            while True:
                state = self.mq.gcode_state
                # 云端任务靠这串名字跟本地这一炉对上号（subtask_name 最接近）
                self.cloud.hint = (self.mq.state.get("gcode_file")
                                   or self.mq.state.get("subtask_name") or "")
                ai_off = self._ai_off()
                if ai_off != self.ai_off_prev:
                    log.info("本机 AI 巡检已%s（工作台监控墙开关）",
                             "关闭，只取画面不判定" if ai_off else "打开")
                    self.ai_off_prev = ai_off
                printing = state in self.PRINTING_STATES
                if printing and not self.session_active:
                    self._start_session()
                elif not printing and self.session_active:
                    self._end_session()
                # 实测账本：每秒记一次时间账（落盘，重启不丢）。
                # 起点优先用云端 startTime 校正，看门狗可能中途才起来，
                # 本地第一次看到 RUNNING 的时刻只会晚于真实开打时刻。
                job_key = (self.mq.state.get("gcode_file")
                           or self.mq.state.get("subtask_name") or "")
                cloud_job = self.cloud.snapshot().get("job") or {}
                self._clock_view = self.clock.tick(
                    state, job_key, cloud_job.get("start_ts"))
                # 已用耗材：打印中才去拉 gcode（打印机 FTP 只有 0.09 MB/s，
                # 不打印时别占着；打印结束就停，缓存表留着给界面用）
                if self.gcode_usage_enabled and self.session_active and job_key:
                    self.usage.start(job_key, cloud_job.get("plate_index"))
                elif not self.session_active:
                    self.usage.stop()
                if self.session_active and not self._ai_off() and (
                        state == "RUNNING" or self.inspect_while_paused):
                    if time.time() >= self.next_inspect:
                        try:
                            self._inspect_once()
                            self.last_error = ""
                        except GadgetError as e:
                            if e.fatal:
                                # 额度用完 / key 失效：再试一万次也是同样结果，
                                # 别每 10 秒刷一遍日志，改成 10 分钟探一次。
                                hint = GADGET_FATAL.get(e.error_type, "")
                                self.last_error = hint or str(e)
                                log.error("AI 检测停摆：%s%s", e,
                                          ("。%s" % hint) if hint else "")
                                self.next_inspect = time.time() + 600
                            else:
                                self.last_error = str(e)
                                log.warning("本次检测失败: %s", e)
                                self.next_inspect = time.time() + 10
                        except Exception as e:
                            self.last_error = "%s: %s" % (type(e).__name__, e)
                            log.warning("本次检测失败: %s", e)
                            self.next_inspect = time.time() + 10
                            # 不要在这里关摄像头：socket 归 pump 管，
                            # 从外面关会让 pump 线程读到半截数据，它自己会重连。
                self._alert_errors()
                self._write_status(state)
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("手动停止")
        finally:
            self._end_session()
            self.usage.stop()      # gcode 采集线程跟着退出，别留孤儿
            self.mq.disconnect()
            if srv is not None:
                srv.shutdown()
                srv.server_close()
        return 0

    def once(self):
        # --once 走同一条 pump 路径，免得出现"检测正常但中转没画面"的两套逻辑。
        # 没有后台线程，云端的名字/任务同步刷一次，让状态文件是完整的。
        self.cloud.hint = (self.mq.state.get("gcode_file")
                           or self.mq.state.get("subtask_name") or "")
        self.cloud.refresh_now()
        self.pump.start()
        try:
            jpeg, ts, seq = self.pump.wait_frame(-1)
        finally:
            self.pump.stop()
        if not jpeg:
            log.error("没取到画面")
            return 1
        save_latest(jpeg)
        self.last_frame = LATEST_PATH.name
        path = save_frame(jpeg, "once")
        log.info("已取帧 %d 字节 -> %s", len(jpeg), path)
        if self.dry_run:
            log.info("[dry-run] 跳过 AI 检测（%s）", self.detector_skip_reason or "dry-run")
            self._write_status(self.mq.gcode_state)
            return 0
        if self.context is None:
            try:
                self.context = self.gadget.create_context()
            except GadgetError as e:
                self.last_error = GADGET_FATAL.get(e.error_type, str(e))
                log.error("创建检测上下文失败：%s", self.last_error)
                self._write_status(self.mq.gcode_state)
                return 2
        try:
            result = self.gadget.process(self.context, jpeg)
        except GadgetError as e:
            self.last_error = GADGET_FATAL.get(e.error_type, str(e))
            log.error("AI 检测失败：%s", self.last_error)
            self._write_status(self.mq.gcode_state)
            return 2
        self.last_error = ""
        self.last_result = result
        self.last_check_at = datetime.now().isoformat(timespec="seconds")
        self.checks += 1
        log.info("检测结果: 质量=%s 分数=%s 警告=%s 暂停建议=%s",
                 result["quality"], result["score"], result["warning"], result["pause"])
        # 写一次状态：工作台的「立即检测」按钮靠这个立刻看到结果，
        # 否则得等常驻进程下一轮才刷新。
        self._write_status(self.mq.gcode_state)
        return 0

    def _ack_score(self):
        """人眼已经接受过的严重程度；没接受过就是 None。"""
        try:
            raw = json.loads(ACK_MARKER.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return float(raw.get("score"))
        except (TypeError, ValueError):
            return None

    def _clear_ack(self):
        try:
            if ACK_MARKER.exists():
                ACK_MARKER.unlink()
        except OSError:
            pass

    def _ai_off(self):
        """本机 AI 巡检是否被关掉（工作台每块砖上的「AI」勾选）。"""
        try:
            return AI_OFF_MARKER.exists()
        except OSError:
            return False

    def _display_name(self, cloud_name: str) -> str:
        """瓷砖上显示的名字。

        优先级：配置里显式写的 ``printer.name_override`` → 拓竹云昵称 → 空。
        为什么要个显式覆盖：云端那个名字只在用户改完并同步之后才变，中间那段
        时间界面上挂着旧名字，而本地没有任何接口能改云端昵称（实测：在
        Studio / 手机 App 里改名后，api.bambulab.cn 的 bind 和 tasks 两个接口
        都还是旧名，台账也还是旧名）。不给个本地覆盖，用户只能干等。
        """
        override = str((self.cfg.get("printer") or {}).get("name_override")
                       or "").strip()
        return override or str(cloud_name or "").strip()

    def _job_fields(self, state):
        """这一炉的实测账：打印机名、已进行时间、已用耗材。

        已进行时间由 JobClock 本地秒级计时并落盘，重启不丢，起点用云端
        startTime 校正。已用耗材由 GcodeUsage 从打印机上正在执行的 gcode
        逐行累加挤出量 E，乘 gcode 头部的 g/mm，是挤出机真实走过的行程。

        gcode 还没解析到当前进度时（刚开打头几分钟）退回按进度折算，
        并标 job_used_est=True，界面会写按进度折算。
        """
        cloud = self.cloud.snapshot()
        job = cloud.get("job") or {}
        printing = state in self.PRINTING_STATES
        pct = _as_float(self.mq.state.get("mc_percent"))
        if state == "PREPARE":
            # 刚开始打印那一小段，mc_percent 还是上一炉的残留值（见过 100），
            # 拿它算已用耗材会直接报出"总共这么多"。PREPARE 就当没有进度。
            pct = None
        remain = _as_float(self.mq.state.get("mc_remaining_time"))
        speed_lvl = _as_float(self.mq.state.get("spd_lvl"))
        speed_pct = _as_float(self.mq.state.get("spd_mag"))
        weight = _as_float(job.get("weight_g"))
        cost = _as_float(job.get("cost_s"))
        clock = self._clock_view or self.clock.view(job.get("start_ts"))
        usage = self.usage.snapshot()
        header = usage.get("header") or {}
        # 总克数：gcode 头部那份（切片时算的）最可信，其次云端任务
        total_g = _as_float(header.get("weight_g")) or weight

        used_g, used_est, used_src = None, True, ""
        # 刚打完（FINISH/IDLE 但这一炉的账还在）也要给出最终数，用户要的是
        # 「记录」，打完就消失等于没记。这时进度可能已经不再上报，按 100% 算。
        # 只认 12 小时内的，别让三天前那炉的数字一直挂在墙上。
        finished_ts = None
        if clock.get("finished_at"):
            try:
                finished_ts = datetime.fromisoformat(clock["finished_at"]).timestamp()
            except (ValueError, OSError, TypeError):
                finished_ts = None
        job_done = bool(not printing and clock.get("key") and finished_ts
                        and clock.get("saw_running")
                        and 0 <= time.time() - finished_ts <= 12 * 3600)
        active_session = printing or job_done
        if active_session:
            eff_pct = pct if pct is not None else (100.0 if job_done else None)
            if eff_pct is not None:
                grams, covered = self.usage.grams_at(eff_pct)
                if covered and grams is not None:
                    used_g, used_est, used_src = grams, False, "gcode"
                elif weight is not None:
                    used_g = round(weight * eff_pct / 100.0, 1)
                    used_est, used_src = True, "progress"
        if used_g is not None:
            self.clock.remember(used_g, total_g, used_src)

        elapsed_sec = clock.get("elapsed_sec")
        active_sec = clock.get("active_sec")
        out = {
            "device_name": self._display_name(cloud.get("name")),
            "job_title": job.get("title") or "",
            "job_task_id": job.get("task_id"),
            "job_filaments": job.get("filaments") or [],
            "job_total_g": round(total_g, 1) if total_g is not None else None,
            "job_cost_min": int(round(cost / 60.0)) if cost else None,
            "job_used_g": used_g,
            "job_used_est": used_est,
            "job_used_source": used_src,
            # 已进行：实测秒数（墙钟）。active 是真正在打的时间，不含暂停。
            "job_started_at": clock.get("started_at") or "",
            "job_started_source": clock.get("started_source") or "",
            "job_elapsed_sec": int(elapsed_sec) if elapsed_sec is not None else None,
            "job_active_sec": int(active_sec) if active_sec is not None else None,
            "job_elapsed_min": (int(elapsed_sec // 60)
                                if elapsed_sec is not None else None),
            "job_active_min": (int(active_sec // 60)
                               if active_sec is not None else None),
            # 我们真正盯着这一炉的时长。看门狗中途才起来时它比 elapsed 短，
            # 界面靠这个判断「能不能拿 elapsed-active 当暂停时长」，
            # 否则会凭空算出几个小时的"暂停/换料等待"。
            "job_observed_sec": (int(clock["observed_sec"])
                                 if clock.get("observed_sec") is not None else None),
            "job_observed_from": clock.get("observed_from") or "",
            "job_eta": "",
            # 打印机自己的时间口径：mc_remaining_time 就是它屏幕上那个「剩余
            # 时间」，单位分钟（实测 2.7 分钟掉 3 分钟；参考源写的"秒"是错的，
            # 按秒算这炉早该结束了）。它跟着速度档位实时重算，所以比切片那份
            # 估计值靠谱。spd_lvl/spd_mag 是当前档位和速度百分比。
            "job_remaining_min": (int(round(remain)) if remain and remain > 0
                                  else None),
            "job_speed_level": SPEED_LEVELS.get(int(speed_lvl), "") if speed_lvl else "",
            "job_speed_pct": int(speed_pct) if speed_pct else None,
            # 机器口径的总时长 = 已进行 + 它说的剩余。只在 RUNNING 时算：
            # 暂停期间机器自己的剩余时间也冻住，拿它加墙钟会把总时长越撑越大。
            "job_total_min": (int(round(elapsed_sec / 60.0)) + int(round(remain))
                              if (elapsed_sec is not None and remain
                                  and state == "RUNNING") else None),
            # gcode 挤出量采集的进度（界面用它解释「为什么现在还是折算值」）。
            # 注意不能叫 gcode_state，那个名字是打印机自己的状态字段
            # （RUNNING/IDLE/…），这里覆盖掉会让整个界面读错状态。
            "gcode_usage_state": usage.get("state") or "",
            "gcode_name": usage.get("gcode") or "",
            "gcode_pct": usage.get("parsed_pct"),
            # 下载进度（字节）：实测要等整份 gcode 下完才算得出归一化系数，
            # 所以界面上真正该给用户看的是这个，不是已解析到的打印进度。
            "gcode_bytes_done": usage.get("bytes_done"),
            "gcode_bytes_total": usage.get("total_bytes"),
            "gcode_verified": bool(usage.get("verified")),
            # 解析出的挤出总量是否和切片声明对得上。False 表示这套表不能用，
            # job_used_est 会保持 True（界面按折算显示并说明原因）。
            "gcode_trusted": bool(usage.get("trusted")),
            "gcode_error": usage.get("error") or "",
            # 上一炉的实测留档（用户要「记录」，不只是实时显示）
            "job_last": (clock.get("history") or [None])[-1],
            "job_done": bool(job_done),
            "cloud_error": cloud.get("error") or "",
        }
        if not active_session:
            # 空闲且没有刚打完的账：别报「已进行」，这时候这两个数只会误导
            return out
        if not printing:
            return out          # 刚打完：时长和耗材已经填好，但没有「预计完成」可算
        if remain is not None:
            out["job_eta"] = (datetime.now() + timedelta(minutes=remain)).isoformat(
                timespec="seconds")
        return out

    def _write_status(self, state):
        result = self.last_result or {}
        status = {
            "time": datetime.now().isoformat(timespec="seconds"),
            # pid + started_at：工作台靠这两个判断"这文件是活进程写的还是上次残留的"
            "pid": os.getpid(),
            "started_at": self.started_at,
            "dry_run": self.dry_run,
            "engine": self.engine,
            "mqtt_mode": self.mq.mode_used,
            "gcode_state": state,
            "monitoring": self.session_active,
            # 本机 AI 巡检开关（工作台监控墙每块砖上的「AI」勾选）。
            # 关掉后照样连打印机、照样存帧、照样报进度，只是不送判定。
            "ai_off": self._ai_off(),
            # 人眼已接受的严重程度；ack_suppressed=True 表示这次判定没超过
            # 那个程度，所以没弹窗。界面据此把红徽标换成灰的「已接受」。
            "ack_score": self._ack_score(),
            "ack_suppressed": self.ack_suppressed,
            "consecutive_pause_signals": self.consecutive_pause,
            "pause_after_consecutive": self.pause_after,
            "paused_by_us": self.paused_by_us,
            "nozzle_temper": self.mq.state.get("nozzle_temper"),
            "bed_temper": self.mq.state.get("bed_temper"),
            "mc_percent": self.mq.state.get("mc_percent"),
            "mc_remaining_min": self.mq.state.get("mc_remaining_time"),
            "layer_num": self.mq.state.get("layer_num"),
            "total_layer_num": self.mq.state.get("total_layer_num"),
            "gcode_file": self.mq.state.get("gcode_file") or self.mq.state.get("subtask_name"),
            # 打印机自己报的错。hms 是"当前还没清除的硬件消息"，会自己变空。
            "print_error": self.mq.print_error,
            "hms": [format_hms(a, c) for a, c in self.mq.hms],
            "has_error": bool(self.mq.print_error or self.mq.hms),
            # 严重级：noise = 常驻但不影响打印（AMS 固件不匹配之类），
            # 界面不为它标红或告警，只在提示和详情里带一句说明。
            "error_level": self.mq.error_severity()[0],
            "error_note": self.mq.error_severity()[1],
            "error_text": self.mq.error_text(),
            # AI 检测汇总
            "checks": self.checks,
            "failures": self.failures,
            "last_check_at": self.last_check_at,
            "last_quality": result.get("quality"),
            "last_score": result.get("score"),
            "last_warning": result.get("warning"),
            "last_pause": result.get("pause"),
            "last_frame": self.last_frame,
            # AI 侧的故障（额度用完、key 失效）。为空表示 AI 检测本身是好的。
            "gadget_error": self.last_error,
            # 下一次检测的倒计时（秒）。界面靠它显示"再过多久会看下一帧"，
            # 否则用户只能看到一个不动的"最近检测"，分不清是卡住了还是节奏慢。
            "next_inspect_in": (max(0, int(self.next_inspect - time.time()))
                                if self.next_inspect else None),
            # 炒面程度趋势：ema 是平滑分数，slope_per_min 是爬升速度
            # （分数点/分钟，正=越来越重），escalating=已判定恶化中
            "trend": self.last_trend,
        }
        # 设备名 / 已进行时间 / 已用耗材：来自拓竹云（见 CloudJob 的注释），
        # 单列一组，免得和本地实测字段混在一起看不清来源。
        status.update(self._job_fields(state))
        try:
            STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        except OSError:
            pass


# ---------------- 自检与入口 ----------------

def cmd_push_test(cfg, secrets, override=""):
    """--push-test [type:key]：验证微信通道配好没有。不连打印机、不占单实例锁。

    带参数就直接用参数试，不必先改配置，刚扫码拿到 token 的人最需要这个。
    形如 --push-test pushplus:abc123 或 --push-test wecom:https://qyapi...
    """
    if override:
        cfg = dict(cfg)
        t, _, k = override.partition(":")
        cfg["push"] = dict(cfg.get("push") or {},
                           enabled=True, type=t.strip().lower(), key=k.strip())
    p = Pusher(cfg, secrets)
    if not p.enabled:
        print("推送未启用。在 a1-watchdog.config.json 里填：")
        print('  "push": {"enabled": true, "type": "pushplus", "key": "你的token"}')
        print("（type 可选 serverchan / pushplus / wecom / wxpusher，见 README）")
        return 2
    print("通道 %s，正在发送测试消息..." % p.type)
    body = ("这是 a1-watchdog 的推送测试\n\n"
            "收到这条就说明通道已经通了，报警会走同一条路。\n\n"
            "时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    ok, info = p._post("A1 看门狗推送测试", body)
    print(("成功：" if ok else "失败：") + info)
    if not ok:
        print("常见原因：key 填错/过期、通道选错、本机访问不了该服务（代理/防火墙）。")
    return 0 if ok else 1


def cmd_check(cfg, secrets):
    ok = True
    engine = str((cfg.get("detection", {}) or {}).get("engine", "local")).lower()

    print("== 1/5 配置 ==")
    printer = cfg.get("printer", {}) or {}
    ip = str(printer.get("ip", "") or "")
    serial = str(printer.get("serial", "") or "")
    code = access_code_for(cfg, secrets)
    if is_placeholder(ip) or ip in ("127.0.0.1", "localhost"):
        ok = False
        print("   打印机 IP 还没填，去 a1-watchdog.config.json 改 printer.ip")
    else:
        print("   打印机 IP: %s" % ip)
    if is_placeholder(serial):
        ok = False
        print("   序列号还没填，去 printer.serial 填（只有云端 MQTT 用它，本地模式可留空）")
    if is_placeholder(code):
        ok = False
        print("   访问码还没填，去 a1-watchdog.secrets.json 填 access_codes")
    if ok:
        print("   配置文件齐全")

    print("== 2/5 本地检测模型 ==")
    if engine == "gadget":
        print("   跳过（detection.engine = gadget，只用云端）")
    else:
        is_onnx = LOCAL_MODEL_PATH.suffix.lower() == ".onnx"
        print("   模型: %s（%s）" % (LOCAL_MODEL_PATH, "存在" if LOCAL_MODEL_PATH.exists() else "不存在"))
        print("   运行时: %s" % ("onnxruntime" if is_onnx else "ai_edge_litert"))
        try:
            det_cls = OnnxSpagDetector if is_onnx else LocalDetector
            det = det_cls(model_path=LOCAL_MODEL_PATH)
            print("   OK 模型加载成功，输入尺寸 %s" % str(
                det.input_hw if is_onnx else det.input_size))
        except Exception as e:
            ok = False
            print("   失败: %s: %s" % (type(e).__name__, e))
            if isinstance(e, FileNotFoundError):
                print("   仓库不带模型权重。两条路选一条：")
                print("   一、自己准备模型放进 %s" % LOCAL_MODEL_PATH.parent)
                print("       支持 .tflite（4 类检测）和 .onnx（2 类分类），详见 README 的「模型从哪来」")
                print("   二、改 a1-watchdog.config.json 的 detection.engine 为 gadget，用云端模型")
                print("       云端需要 OctoEverywhere 的 API key，填在 secrets 的 gadget_api_key")

    print("== 3/5 摄像头（6000 端口）==")
    if is_placeholder(code):
        ok = False
        print("   失败: 访问码还没填，去 a1-watchdog.secrets.json 填 access_codes")
        print("   访问码在打印机屏幕上：设置 → 网络 → 局域网模式 → 访问码")
    else:
      try:
        cam = BambuCamera(cfg["printer"]["ip"], code)
        jpeg = cam.get_frame()
        path = save_frame(jpeg, "check")
        print("   OK 抓到 %d 字节帧 -> %s" % (len(jpeg), path.name))
        cam.close()
      except Exception as e:
        ok = False
        print("   失败: %s" % e)

    print("== 4/5 MQTT ==")
    try:
        mq = BambuMqtt(cfg, secrets)
        if not mq.connect():
            raise RuntimeError("连接失败")
        time.sleep(2)
        mq._publish(mq.client, {"info": {"sequence_id": "1", "command": "get_version"}})
        time.sleep(3)
        print("   OK 状态=%r 固件=%s" % (mq.gcode_state, mq.firmware or "未知"))
        mq.disconnect()
    except Exception as e:
        ok = False
        print("   失败: %s" % e)

    print("== 5/5 云端 Gadget key ==")
    key = secrets.get("gadget_api_key", "")
    if is_placeholder(key):
        print("   未配置（可选；用云端检测才需要，本地模型不用填）")
    else:
        try:
            g = GadgetClient(key)
            ctx = g.create_context()
            print("   OK 上下文创建成功: %s..." % ctx["id"][:16])
        except Exception as e:
            ok = False
            print("   失败: %s" % e)

    print()
    print("总结: %s" % ("全部通过，可以开跑" if ok else "有项目需要修复"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="A1 炒面监控看门狗")
    ap.add_argument("--check", action="store_true", help="自检四项：检测模型、摄像头、MQTT、云端 key")
    ap.add_argument("--once", action="store_true", help="单次取帧+检测后退出")
    ap.add_argument("--push-test", nargs="?", const="", default=None,
                    metavar="TYPE:KEY",
                    help="发一条微信推送测试后退出，可临时指定 通道:密钥")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    cfg = load_json(CONFIG_PATH)
    secrets = load_json(SECRETS_PATH, required=False)

    if args.push_test is not None:
        return cmd_push_test(cfg, secrets, args.push_test)

    # --check 也要挡：它一样会去连 6000 摄像头，正主在跑的时候它必然失败，
    # 而失败信息会被误读成"摄像头坏了"。
    if not acquire_single_instance():
        # 锁文件本身被锁住读不了，PID 从状态文件里拿，那才是给人看的位置。
        other = ""
        try:
            other = str(load_json(STATUS_PATH).get("pid") or "")
        except SystemExit:
            pass
        except Exception:
            pass
        msg = "已经有一个看门狗在跑%s，不要重复启动。" % ("（PID %s）" % other if other else "")
        log.error(msg)
        print(msg)
        return 3

    if args.check:
        return cmd_check(cfg, secrets)

    if not str((cfg.get("printer") or {}).get("ip") or "").strip():
        msg = ("这台还没填打印机 IP。在工作台「炒面监控」页点「自动接入打印机」，"
               "或点「去配置」手填；也可以在 %s 里写 printer.ip 和访问码。"
               % CONFIG_PATH)
        log.error(msg)
        print(msg)
        return 2

    wd = Watchdog(cfg, secrets)
    if wd.dry_run:
        msg = "AI 检测没有开：%s。看门狗照常连打印机、存帧、报进度，但不会判定炒面。" % wd.detector_skip_reason
        log.warning(msg)
        print(msg)
    else:
        try:
            wd.gadget = build_detector(cfg, secrets, wd.engine)
        except Exception as e:
            wd.dry_run = True
            wd.gadget = None
            wd.detector_skip_reason = "检测器起不来：%s" % e
            msg = ("AI 检测起不来（%s）。看门狗继续跑，只取画面不判定。"
                   "先 pip install -r requirements.txt，再确认模型文件能打开。" % e)
            log.error(msg)
            print(msg)
    if args.once:
        if not wd.mq.connect():
            return 1
        try:
            return wd.once()
        finally:
            wd.mq.disconnect()
    return wd.run()


if __name__ == "__main__":
    sys.exit(main())
