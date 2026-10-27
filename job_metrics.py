#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记录这一炉打了多久、用了多少料。

这两个数都没法用进度乘总量折算，MQTT 的 report 里也没有：
  mc_print_line_number 恒为 "0"（A1/P1 固件不填这个字段）
  ams 是空数组，vt_tray.tray_weight 是 "0"
  没有任何累计耗材或已用克数字段

所以只能自己测：
  JobClock   秒级计时并落盘，重启不丢。
  GcodeUsage 从打印机上正在执行的 gcode 逐行累加挤出量 E（毫米），
             乘每毫米多少克得到已用克数，是挤出机真正走过的行程。

GcodeUsage 的实现约束：
  打印机 FTP（990，隐式 FTPS，bblp 加访问码）能读到 /cache/<名字>_plate_N.gcode
  吞吐约 0.09 MB/s，数据通道明文也一样慢，瓶颈在打印机一侧
  打印进度只推进约 2.7 KB/s，比 FTP 慢 30 多倍，边下边解析能跑在打印前面
  gcode 头部自带 total filament weight [g] 和 total filament length [mm]，
  两者相除就是该耗材的 g/mm，不用猜密度
"""

from __future__ import annotations

import difflib
import ftplib
import hashlib
import json
import re
import socket
import ssl
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------- 通用小工具 ----------------


def _as_ts(value: Any) -> float | None:
    """接受 epoch 秒 / ISO 字符串 / None，统一成 epoch 秒。坏值一律当没有。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError, OverflowError):
        # Windows 上 datetime.timestamp() 遇到 1970 前的时刻会抛 OSError，
        # 别让一个坏时间戳把整轮状态写入搞崩。
        return None


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ---------------- 计时账本 ----------------


CLOCK_FILE = "a1-watchdog.job.json"
HISTORY_KEEP = 30


class JobClock:
    """记录这一炉的经过时间与有效打印时间，落盘保存。

    不能只信云端 startTime：云令牌约三个月过期，看门狗重启也会丢掉内存里的
    计时，所以本地要有一份自己的账。

    云端 startTime 只用来校正起点。看门狗可能中途才起来，本地记的起点偏晚；
    两者都不会比真实开打更早，取更早的那个更接近真值。

    两个时间口径分开记：
      elapsed_sec  墙钟，从开打到现在的总经过时间，含暂停和换料
      active_sec   有效打印，只有 gcode_state 为 RUNNING 时才累加
    """

    PRINTING = ("PREPARE", "RUNNING", "PAUSE")

    # 同一炉在 MQTT 里有两个名字：打印中是 `gcode_file`（带 .3mf），
    # 结束后只剩 `subtask_name`（不带扩展名）。不归一化的话每次重启都被
    # 判成"换炉"，刚打完的账直接被抹掉。全角标点和空白同理。
    _SUFFIXES = (".3mf", ".gcode", ".stl", ".step", ".stp", ".3mf.gcode")
    _PUNCT = str.maketrans("，。；：？！（）【】、", ",.;:?!()[],")

    @classmethod
    def norm_key(cls, value) -> str:
        k = str(value or "").strip().translate(cls._PUNCT)
        low = k.lower()
        for suf in cls._SUFFIXES:
            if low.endswith(suf):
                k = k[: -len(suf)]
                break
        return " ".join(k.split())

    def __init__(self, base_dir, now_fn=time.time):
        self.path = Path(base_dir) / CLOCK_FILE
        self._now = now_fn
        self.data = self._load()
        self._last_tick: float | None = None

    # -- 持久化 --
    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if not isinstance(raw.get("history"), list):
            raw["history"] = []
        return raw

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass

    # -- 计时 --
    def tick(self, state: str, job_key: str,
             cloud_started_ts: float | None = None) -> dict[str, Any]:
        """主循环每秒调一次。换了一炉就自动开新账。"""
        now = self._now()
        prev = str(self.data.get("key") or "")
        key = self.norm_key(job_key)
        # 只有「拿到了一个新的非空 key」才算换炉。key 变空（打印结束后 MQTT
        # 不再上报 gcode_file、或者看门狗刚起来还没收到报告）是"暂时不知道"，
        # 不是换炉，按换炉处理会把上一炉刚打完的账直接抹掉。
        if key and prev and key != prev:
            self._archive(now)
        if key and key != prev:
            self.data["key"] = key
            self.data["started_at_ts"] = now
            self.data["started_at"] = _iso(now)
            # 我们自己是几点开始盯这一炉的。跟云端开打时刻（started_at_ts）
            # 是两件事：看门狗可能中途才起来，那前面的时长根本没被观测到，
            # 「有效打印」就不能拿 elapsed 去减（会凭空算出几个小时的"暂停"）。
            self.data["first_tick_ts"] = now
            self.data["active_sec"] = 0.0
            self.data["finished_at"] = ""
            # 这一炉我们到底见没见过它 RUNNING。没见过的账是启动时从 MQTT
            # 残留的 subtask_name 里捡来的，起始时刻只能靠云端猜，结束时刻
            # 更是无从得知，这种账一律不许当"刚打完"展示，否则时长是假的。
            self.data["saw_running"] = False
            self.data["last_used_g"] = None
            self.data["last_total_g"] = None
            self.data["last_used_source"] = ""
            self._last_tick = None
            # 新账立刻落盘：不然刚要打的那几十秒里进程被 killed，这一炉就
            # 直接从账本上消失了（remember() 要等耗材数变化才写，太晚）。
            self._save()
        if cloud_started_ts:
            self.data["cloud_started_ts"] = float(cloud_started_ts)
        if state == "RUNNING":
            self.data["saw_running"] = True
            # 又在打了就得把结束时刻撤掉。看门狗重启后第一跳往往还没收到
            # MQTT 报告（state 不是打印中），下面会把 finished_at 盖上；
            # 而 view() 拿 finished_at 当墙钟终点，之后「已进行」就冻在
            # 那一刻不涨了，打印还在跑，数字却是上一段的。实际遇到过：
            # 15:38:41 重启盖上结束时刻，之后 45 分钟 elapsed 一直停在 387 分钟。
            if self.data.get("finished_at"):
                self.data["finished_at"] = ""
                self._save()
            if self._last_tick is not None:
                # 上限 5 秒：进程被系统挂起过的话，不能把那段空档算成打印时间
                delta = min(max(0.0, now - self._last_tick), 5.0)
                self.data["active_sec"] = float(self.data.get("active_sec") or 0) + delta
        self._last_tick = now
        if state not in self.PRINTING and self.data.get("key"):
            if not self.data.get("finished_at"):
                self.data["finished_at"] = _iso(now)
        return self.view(cloud_started_ts)

    def remember(self, used_g, total_g, source: str) -> None:
        """把当前实测到的耗材量记下来，归档时一起写进历史。"""
        changed = False
        if used_g is not None and self.data.get("last_used_g") != used_g:
            self.data["last_used_g"] = used_g
            changed = True
        if total_g is not None and self.data.get("last_total_g") != total_g:
            self.data["last_total_g"] = total_g
            changed = True
        if source and self.data.get("last_used_source") != source:
            self.data["last_used_source"] = source
            changed = True
        if changed:
            self._save()

    def view(self, cloud_started_ts: float | None = None) -> dict[str, Any]:
        now = self._now()
        cloud_ts = _as_ts(cloud_started_ts) or _as_ts(self.data.get("cloud_started_ts"))
        local_ts = _as_ts(self.data.get("started_at_ts"))
        start_ts, source = None, ""
        if cloud_ts and local_ts:
            if cloud_ts <= local_ts:
                start_ts, source = cloud_ts, "cloud"
            else:
                start_ts, source = local_ts, "local"
        elif cloud_ts:
            start_ts, source = cloud_ts, "cloud"
        elif local_ts:
            start_ts, source = local_ts, "local"
        finished = _as_ts(self.data.get("finished_at"))
        # 打印结束后墙钟要停在结束那一刻，否则「已进行」会一直往上涨
        end = finished or now
        first = _as_ts(self.data.get("first_tick_ts")) or local_ts
        return {
            "started_at_ts": start_ts,
            "started_at": _iso(start_ts),
            "started_source": source,
            "finished_at": self.data.get("finished_at") or "",
            "elapsed_sec": max(0.0, end - start_ts) if start_ts else None,
            "active_sec": float(self.data.get("active_sec") or 0),
            # 我们真正盯着这一炉的时长。比 elapsed 短说明看门狗中途才起，
            # 前面那段没记录，界面不能拿差额冒充暂停。
            "observed_sec": max(0.0, end - first) if first else None,
            "observed_from": _iso(first),
            # 我们到底见没见过它 RUNNING。没见过就不许当"刚打完"展示。
            "saw_running": bool(self.data.get("saw_running")),
            "key": self.data.get("key") or "",
            "history": list(self.data.get("history") or []),
        }

    def _archive(self, now: float) -> None:
        d = self.data
        if not d.get("key"):
            return
        start = _as_ts(d.get("cloud_started_ts")) or _as_ts(d.get("started_at_ts"))
        end = _as_ts(d.get("finished_at")) or now
        first = _as_ts(d.get("first_tick_ts")) or _as_ts(d.get("started_at_ts"))
        entry = {
            "key": d.get("key"),
            "started_at": d.get("started_at") or "",
            "finished_at": d.get("finished_at") or _iso(now),
            "elapsed_sec": int(max(0.0, end - start)) if start else None,
            "active_sec": int(float(d.get("active_sec") or 0)),
            "observed_sec": int(max(0.0, end - first)) if first else None,
            "used_g": d.get("last_used_g"),
            "total_g": d.get("last_total_g"),
            "used_source": d.get("last_used_source") or "",
        }
        hist = d.setdefault("history", [])
        hist.append(entry)
        del hist[:-HISTORY_KEEP]
        self._save()


# ---------------- gcode 挤出量 ----------------

GCODE_CACHE_DIR = "gcode-cache"
FTP_PORT = 990
FTP_USER = "bblp"
MAX_GCODE_BYTES = 600 * 1024 * 1024      # 超过这个就不下了，别把打印机 FTP 占死
CHUNK = 65536

# E 值必须允许前导点：拓竹/Orca 切出来的 gcode 里 99% 是 `E.3742` 这种写法
# （实测 68334 个 E 值里 67839 个带前导点）。写成 `\d+` 会只捞到 0.7%，
# 算出来的耗材少一个数量级，而且它看起来"有数"，比报错更难发现。
_RE_E = re.compile(rb"\bE(-?\d*\.?\d+)")
_RE_M73P = re.compile(rb"^M73\s+P(\d+(?:\.\d+)?)")
_RE_HDR_G = re.compile(rb"total filament weight \[g\]\s*:\s*([\d.]+)")
_RE_HDR_MM = re.compile(rb"total filament length \[mm\]\s*:\s*([\d.]+)")
_RE_HDR_DIA = re.compile(rb"filament_diameter\s*[:=]\s*([\d.]+)")

# 常见耗材密度（g/cm³），只在 gcode 头部没有 g/mm 时兜底用
DENSITY = {
    "PLA": 1.24, "PLA-CF": 1.22, "PETG": 1.27, "PETG-CF": 1.25,
    "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "PC": 1.21, "PA": 1.14,
    "PA-CF": 1.12, "PVA": 1.23, "HIPS": 1.04, "PPS": 1.35,
}


def _safe_name(name: str) -> str:
    """gcode 名字 → 缓存文件名。

    截断到 80 字符会让「同名不同盘/不同参数」的 gcode 撞在一起，所以尾巴上
    挂 8 位哈希；空格留着（打印机自己就用空格）但两头必须剪掉，Windows 上
    末尾带空格的路径会被静默吞掉。
    """
    keep = []
    for ch in name:
        keep.append(ch if (ch.isalnum() or ch in "-_. ") else "_")
    stem = "".join(keep).strip()[:60]
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return "%s-%s" % (stem, digest)


_MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _list_mtime(parts) -> float:
    """解析 `LIST` 的时间列：近半年是 `Oct 25 10:17`，更早是 `Oct 25 2024`。

    解析不出来就返回 0.0（调用方把 0 当「没有时间信息」，不参与排序）。
    """
    try:
        mon = _MONTHS[str(parts[5])[:3]]
        day = int(parts[6])
        tok = str(parts[7])
    except (KeyError, IndexError, ValueError):
        return 0.0
    now = datetime.now()
    try:
        if ":" in tok:
            hh, mm = tok.split(":")[:2]
            dt = datetime(now.year, mon, day, int(hh), int(mm))
            if dt > now:                      # 跨年：12 月的文件在 1 月看到
                dt = datetime(now.year - 1, mon, day, int(hh), int(mm))
        else:
            dt = datetime(int(tok), mon, day)
        return dt.timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


def pick_gcode(entries, wanted_base: str, plate_index=None) -> str | None:
    """在 /cache 的文件列表里挑出这一炉的 gcode。

    `entries` 可以是纯文件名列表，也可以是 `(名字, 字节数, mtime)` 元组列表
    （后者能用 mtime 兜底，实际调用都传后者）。

    名字排序键是三元组，优先级从高到低：
      1. 公共前缀长度
      2. 整体相似度，用来区分「0.2mm 层高, 2 层墙, 15% 填充」和
         「0.2mm 层高, 6 层墙, 25% 填充」这种只差中间几个字的
      3. 盘号是否等于云端的 plateIndex，同一个文件多盘时才起作用
    只按公共前缀排会把 6 层墙那份误当成同一炉，所以第 2 项必须在盘号之前。

    兜底：名字截断到只剩一两个字符时前缀门槛过不去，就退回 /cache 里最新
    那份。打印机上传完立刻开打，正在打的必然是 mtime 最大的。这条只在有
    mtime 信息时启用。
    """
    base_want = str(wanted_base or "").strip()
    for suffix in (".3mf", ".gcode"):
        if base_want.lower().endswith(suffix):
            base_want = base_want[: -len(suffix)]
    if not base_want:
        return None
    try:
        want_plate = int(plate_index) if plate_index is not None else None
    except (TypeError, ValueError):
        want_plate = None

    rows: list[tuple[str, float]] = []
    for item in entries or []:
        if isinstance(item, (tuple, list)):
            name = str(item[0]) if item else ""
            try:
                mtime = float(item[2]) if len(item) > 2 else 0.0
            except (TypeError, ValueError):
                mtime = 0.0
        else:
            name, mtime = str(item), 0.0
        rows.append((name, mtime))

    best: tuple[tuple[float, float, int, float], str] | None = None
    for name, mtime in rows:
        if not name.endswith(".gcode") or "_plate_" not in name:
            continue
        stem = name[: -len(".gcode")]
        base, _, plate = stem.rpartition("_plate_")
        if not plate.isdigit():
            continue
        common = 0
        for a, b in zip(base, base_want):
            if a != b:
                break
            common += 1
        if common < 3:
            continue
        ratio = difflib.SequenceMatcher(None, base, base_want).ratio()
        plate_hit = 1 if (want_plate is not None and int(plate) == want_plate) else 0
        key = (common, round(ratio, 4), plate_hit, mtime)
        if best is None or key > best[0]:
            best = (key, name)
    if best is not None:
        return best[1]

    # 名字彻底对不上时的兜底：最新的那份
    timed = [(m, n) for n, m in rows if m > 0 and n.endswith(".gcode")]
    if timed:
        return max(timed)[1]
    return None


class _Parser:
    """增量 gcode 解析器：喂字节块，吐出 (M73 进度 → 累计挤出毫米)。

    Bambu 的 gcode 用相对挤出（M83），但 M82 绝对模式也要认，不然万一切了
    参数会把累计量算飞。G92 E0 是重置原点，绝对模式下必须跟着改基准。
    """

    def __init__(self) -> None:
        self.buf = b""
        self.rel = True          # M83 相对挤出（Bambu 默认）
        self.cum = 0.0           # 累计挤出（毫米，带符号：回抽会减）
        self.last_abs = 0.0
        self.points: list[list[float]] = []   # [[M73 进度, 累计毫米], ...]
        self.header: dict[str, float] = {}
        self.lines = 0
        self.bytes = 0
        self.last_pct = 0.0
        self._header_done = False

    def feed(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        self.buf += chunk
        parts = self.buf.split(b"\n")
        self.buf = parts.pop()
        for line in parts:
            self._line(line)

    def flush(self) -> None:
        if self.buf:
            self._line(self.buf)
            self.buf = b""

    def _line(self, line: bytes) -> None:
        self.lines += 1
        if not self._header_done:
            if b"total filament weight" in line:
                m = _RE_HDR_G.search(line)
                if m:
                    self.header["weight_g"] = float(m.group(1))
            elif b"total filament length" in line:
                m = _RE_HDR_MM.search(line)
                if m:
                    self.header["length_mm"] = float(m.group(1))
            elif b"filament_diameter" in line:
                m = _RE_HDR_DIA.search(line)
                if m:
                    self.header.setdefault("diameter", float(m.group(1)))
            if self.lines > 60000:
                self._header_done = True
        code = line.split(b";", 1)[0].strip()
        if not code:
            return
        head = code[:3]
        if head == b"M83":
            self.rel = True
        elif head == b"M82":
            self.rel = False
        elif head == b"G92":
            m = _RE_E.search(code)
            if m:
                self.last_abs = float(m.group(1))
        elif head == b"M73":
            m = _RE_M73P.match(code)
            if m:
                self.last_pct = float(m.group(1))
                self.points.append([self.last_pct, round(self.cum, 3)])
        elif head[:2] in (b"G0", b"G1", b"G2", b"G3") and head[2:3] in (b" ", b"\t"):
            m = _RE_E.search(code)
            if m:
                e = float(m.group(1))
                if self.rel:
                    self.cum += e
                else:
                    self.cum += e - self.last_abs
                    self.last_abs = e

    def g_per_mm(self) -> float | None:
        g = self.header.get("weight_g")
        mm = self.header.get("length_mm")
        if g and mm:
            return g / mm
        return None


class GcodeUsage:
    """后台把这一炉的 gcode 拉下来边下边算，产出「进度 → 已用克数」表。

    单开线程的理由和 CloudJob 一样：FTP 吞吐只有 0.09 MB/s，一次传输可能
    持续几十分钟，绝不能挂在主循环上。主循环只读 `snapshot()`。
    """

    def __init__(self, base_dir, ip: str, access_code: str,
                 serial: str = "", log=None) -> None:
        self.base = Path(base_dir)
        self.cache_dir = self.base / GCODE_CACHE_DIR
        self.ip = str(ip or "")
        self.code = str(access_code or "")
        self.serial = serial
        self._log = log
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.wanted = ""          # 这一炉的 3mf 名字（gcode_file）
        self.plate_index = None
        self.gcode_name = ""
        self.total_bytes = 0
        self.state = "idle"       # idle / 找文件 / 下载中 / 完成 / 失败
        self.bytes_done = 0
        self.parsed_pct = 0.0
        self.points: list[list[float]] = []
        self.g_per_mm: float | None = None
        # 归一化后的系数：切片声明的总克数 ÷ 整份 gcode 的累计挤出毫米。
        # 算「已用多少克」用的是它，不是头部的 g/mm。
        self.eff_gpm: float | None = None
        self.header: dict[str, float] = {}
        self.total_mm: float | None = None
        self.verified = False
        # 解析出来的挤出总量和 gcode 头部声明的长度对不上时，这套数就不可信。
        # 不能因为它"看起来有个数字"就当成实测，宁可退回折算并说明原因。
        self.trusted = False
        self.error = ""
        self._persist_at = 0.0
        self._failed_at = 0.0

    # -- 对外 --
    def start(self, gcode_file: str, plate_index=None) -> None:
        """打印开始时调用。同一个文件重复调用不会重复起线程。

        主循环每秒都会调一次，所以这里必须是幂等的；失败过的任务要等
        冷却期过了才重试，否则每秒起一个线程去撞同一堵墙。
        """
        want = str(gcode_file or "").strip()
        if not want:
            return
        with self._lock:
            if self._thread is not None and self.wanted == want:
                if self.state != "失败":
                    return
                if time.time() - self._failed_at < 600:
                    return
        self.stop()
        self.wanted = want
        self.plate_index = plate_index
        self.state = "找文件"
        self.error = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="gcode-usage", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "gcode": self.gcode_name,
                "bytes_done": self.bytes_done,
                "total_bytes": self.total_bytes,
                "parsed_pct": round(self.parsed_pct, 2),
                "points": list(self.points),
                "g_per_mm": self.g_per_mm,
                "eff_gpm": self.eff_gpm,
                "header": dict(self.header),
                "total_mm": self.total_mm,
                "verified": self.verified,
                "trusted": self.trusted,
                "error": self.error,
            }

    def grams_at(self, pct) -> tuple[float | None, bool]:
        """返回 (已用克数, 是否可用作实测)。

        第二个值为 False 时调用方必须退回按进度折算并标明原因。三种情况：
          - 表还没下完（归一化系数要等整份文件才知道，中途给的数不可信）
          - 解析结构和切片声明对不上（漏读了挤出指令）
          - 请求的进度超出已解析范围
        """
        try:
            want = float(pct)
        except (TypeError, ValueError):
            return None, False
        with self._lock:
            points = list(self.points)
            gpm = self.eff_gpm
            head_g = self.header.get("weight_g")
            state = self.state
            trusted = self.trusted
        if not points or not gpm:
            return None, False
        # 归一化系数是拿「切片声明的总克数 ÷ 整份 gcode 的累计挤出」算出来的，
        # 下完才存在。中途给的那点数据连系数都没有，不能当实测。
        if state != "完成" or not trusted:
            return None, False
        if want > points[-1][0]:
            return None, False
        # 超范围的情况上面已经挡掉了，这里两端都是表内
        lo, hi = points[0], points[-1]
        if want <= lo[0]:
            mm = lo[1]
        elif want >= hi[0]:
            mm = hi[1]
        else:
            mm = hi[1]
            for i in range(1, len(points)):
                if points[i][0] >= want:
                    a, b = points[i - 1], points[i]
                    span = b[0] - a[0]
                    ratio = 0.0 if span <= 0 else (want - a[0]) / span
                    mm = a[1] + (b[1] - a[1]) * ratio
                    break
        grams = mm * gpm
        # 上限就是切片声明的总克数：归一化之后本来就该收敛到它，
        # 但插值端点可能略超，别让「已用」大于「共」。
        if isinstance(head_g, (int, float)) and head_g > 0:
            grams = min(grams, head_g)
        return round(max(0.0, grams), 1), True

    # -- 线程 --
    def _log_info(self, msg: str, *args) -> None:
        if self._log is not None:
            try:
                self._log.info(msg, *args)
            except Exception:  # noqa: BLE001
                pass

    def _log_warn(self, msg: str, *args) -> None:
        if self._log is not None:
            try:
                self._log.warning(msg, *args)
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        if not self.ip or not self.code:
            with self._lock:
                self.state = "失败"
                self.error = "缺打印机 IP 或访问码，读不了 gcode"
            return
        tries = 0
        while not self._stop.is_set() and tries < 3:
            tries += 1
            try:
                self._once()
                return
            except Exception as exc:  # noqa: BLE001 - 后台线程不能死
                with self._lock:
                    self.error = "%s: %s" % (type(exc).__name__, exc)
                    self.state = "失败"
                    self._failed_at = time.time()
                self._log_warn("gcode 挤出量采集失败（第 %d 次）：%s", tries, exc)
                if self._stop.wait(20):
                    return
        with self._lock:
            if self.state != "完成":
                self.state = "失败"
                self._failed_at = time.time()

    def _connect(self) -> ftplib.FTP_TLS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        raw = socket.create_connection((self.ip, FTP_PORT), timeout=20)
        sock = ctx.wrap_socket(raw)
        ftp = ftplib.FTP_TLS(context=ctx)
        # 拓竹是隐式 FTPS：TCP 一连上就得做 TLS 握手，不能等 220 横幅。
        # 所以绕开 connect()，手工把 wrap 过的 socket 塞进去。
        ftp.sock = sock
        ftp.af = sock.family
        # host/port 必须补：PASV 数据连接 wrap TLS 时要拿它当 server_hostname，
        # 没走过 connect() 的话是空串，会报 "server_hostname cannot be empty"。
        ftp.host = self.ip
        ftp.port = FTP_PORT
        ftp.file = sock.makefile("r", encoding="utf-8", errors="replace")
        ftp.welcome = ftp.getresp()
        ftp.login(FTP_USER, self.code)
        # 不设 PROT 的话拓竹会直接断掉控制通道（实测 EOFError）
        ftp.prot_p()
        return ftp

    def _list(self, ftp) -> list[tuple[str, int, float]]:
        """返回 [(文件名, 字节数, mtime)]。

        mtime 是必需的：打印机把 gcode 名字截得很短
        （0.2mm 层高, 2 层墙, 15% 填充.3mf 变成 0_plate_2.gcode），
        公共前缀只剩一个字符，纯按名字对不上。正在打的那份永远是 /cache
        里最新的，用 mtime 兜底才可靠。
        """
        lines: list[str] = []
        ftp.retrlines("LIST /cache", lines.append)
        out = []
        for line in lines:
            parts = line.split(maxsplit=8)
            if len(parts) < 9:
                continue
            try:
                out.append((parts[8], int(parts[4]), _list_mtime(parts)))
            except ValueError:
                continue
        return out

    def _once(self) -> None:
        ftp = self._connect()
        try:
            files = self._list(ftp)
            name = pick_gcode(files, self.wanted, self.plate_index)
            if not name:
                raise RuntimeError("打印机 /cache 里找不到这一炉的 gcode")
            size = next((f[1] for f in files if f[0] == name), 0)
            with self._lock:
                self.gcode_name = name
                self.total_bytes = size
                self.state = "下载中"
            if size > MAX_GCODE_BYTES:
                raise RuntimeError("gcode %.0f MB 太大，跳过采集" % (size / 1048576.0))
            # 缓存命中就别再下一遍：打印机 FTP 不支持 REST（实测 502），
            # 重下一份 275MB 的 gcode 要 50 分钟起步，每次重启都重来受不了。
            cached = self._load_cache(name, size)
            if cached is not None and cached["final"]:
                with self._lock:
                    self.points = cached["points"]
                    self.header = cached["header"]
                    self.g_per_mm = cached["g_per_mm"]
                    self.eff_gpm = cached["eff_gpm"]
                    self.total_mm = cached["total_mm"]
                    self.verified = cached["verified"]
                    self.trusted = cached["trusted"]
                    self.bytes_done = cached["bytes_done"]
                    self.parsed_pct = 100.0
                    self.state = "完成"
                    self.error = ""
                self._log_info("gcode 挤出量表命中缓存：%s（%d 个进度点，%s）",
                               name, len(cached["points"]),
                               "已校验" if cached["verified"] else "未校验")
                return
            seed = cached["points"] if cached is not None else None
            self._log_info("开始采集 gcode 挤出量：%s（%.1f MB）",
                           name, size / 1048576.0)
            self._download(ftp, name, size, seed)
        finally:
            try:
                ftp.quit()
            except Exception:  # noqa: BLE001
                pass

    def _load_cache(self, name: str, size: int) -> dict[str, Any] | None:
        """读回上一次采到的表。文件大小对不上就作废（同名换了另一份文件）。"""
        path = self.cache_dir / (_safe_name(name) + ".json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("gcode") != name:
            return None
        try:
            if size and data.get("size") and int(data["size"]) != int(size):
                return None
        except (TypeError, ValueError):
            return None
        points: list[list[float]] = []
        for p in data.get("points") or []:
            if isinstance(p, (list, tuple)) and len(p) == 2:
                try:
                    points.append([float(p[0]), float(p[1])])
                except (TypeError, ValueError):
                    continue
        header = {}
        for k, v in (data.get("header") or {}).items():
            if isinstance(v, (int, float)):
                header[str(k)] = float(v)
        gpm = data.get("g_per_mm")
        eff = data.get("eff_gpm")
        total_mm = data.get("total_mm")
        gpm = float(gpm) if isinstance(gpm, (int, float)) and gpm else None
        eff = float(eff) if isinstance(eff, (int, float)) and eff else None
        total_mm = float(total_mm) if isinstance(total_mm, (int, float)) else None
        try:
            bytes_done = int(data.get("bytes_done") or 0)
        except (TypeError, ValueError):
            bytes_done = 0
        return {
            # 只有「解析完了 + 归一化系数在」的缓存才算命中。对不上的表宁可重下，
            # 也不能让它冒充实测值。
            "final": (bool(data.get("final")) and bool(data.get("trusted"))
                      and bool(points) and eff is not None),
            "points": points,
            "header": header,
            "g_per_mm": gpm,
            "eff_gpm": eff,
            "total_mm": total_mm,
            "verified": bool(data.get("verified")),
            "trusted": bool(data.get("trusted")),
            "bytes_done": bytes_done,
        }

    def _download(self, ftp, name: str, size: int,
                  seed_points: list[list[float]] | None = None) -> None:
        parser = _Parser()
        last_persist = 0.0
        seed = seed_points or []

        def _points_now() -> tuple[list[list[float]], float]:
            pts = list(parser.points)
            pct = parser.last_pct
            # 上一轮已经解析到更远处（缓存），重下的开头不能把它缩回去
            if seed and len(pts) < len(seed):
                return list(seed), max(pct, seed[-1][0])
            return pts, pct

        def on_chunk(chunk: bytes) -> None:
            nonlocal last_persist
            if self._stop.is_set():
                raise ftplib.error_temp("cancelled")
            parser.feed(chunk)
            now = time.time()
            if now - last_persist >= 2.0:
                last_persist = now
                pts, pct = _points_now()
                with self._lock:
                    self.bytes_done = parser.bytes
                    self.parsed_pct = pct
                    self.points = pts
                    self.g_per_mm = parser.g_per_mm()
                    self.header = dict(parser.header)
                self._persist(parser, size, final=False, points=pts,
                              parsed_pct=pct)
                # 让出 GIL：主循环要每秒跑一轮 AI 检测，不能被解析抢死
                time.sleep(0.005)

        ftp.retrbinary("RETR /cache/" + name, on_chunk, blocksize=CHUNK)
        parser.flush()
        total_mm = parser.cum
        gpm = parser.g_per_mm()
        head_mm = parser.header.get("length_mm")
        head_g = parser.header.get("weight_g")
        ratio = (total_mm / head_mm) if (head_mm and total_mm) else None
        # 绝对刻度用切片自己声明的总克数做归一化：`weight_g / 累计挤出毫米`。
        # 这样 100% 时必然等于 BambuStudio 显示的总耗材，中间段的比例则来自
        # 真实 E 值累加（每层的耗材分布是量出来的，不是拿进度均摊的）。
        # 顺带消掉系统性偏差，实测正常文件的比例在 1.03~1.14（多出来的是
        # 起始清洗料），直接用头部 g/mm 会一路高估。
        eff = None
        if total_mm > 0 and head_g:
            eff = head_g / total_mm
        elif head_g and head_mm:
            eff = head_g / head_mm
        else:
            eff = gpm
        # 结构校验：解析出的挤出量不能远小于切片声明。远小于说明漏读了挤出
        # 指令（曾经 E 值前导点没解析到，只拿到 15%，还算出 15.7 g 当"实测"）。
        # 偏大则可能是重复计入清洗料，归一化会把它抹平，不必拦。
        low_ok = (ratio is None) or (ratio >= 0.5)
        trusted = bool(eff and low_ok)
        verified = bool(ratio is not None and abs(ratio - 1.0) <= 0.20)
        with self._lock:
            self.bytes_done = parser.bytes
            self.parsed_pct = max(parser.last_pct, 100.0 if trusted else parser.last_pct)
            self.points = list(parser.points)
            self.g_per_mm = gpm
            self.eff_gpm = eff if trusted else None
            self.header = dict(parser.header)
            self.total_mm = total_mm
            self.verified = verified
            self.trusted = trusted
            self.state = "完成"
            if trusted:
                self.error = ""
            else:
                self.error = ("gcode 解析出 %.0f mm 挤出量，只有切片声明 %.0f mm 的"
                              "%.0f%%，漏读了挤出指令，改用按进度折算"
                              % (total_mm, head_mm or 0.0,
                                 100.0 * (total_mm / head_mm) if head_mm else 0.0))
        self._persist(parser, size, final=True)
        self._log_info(
            "gcode 挤出量采集完成：累计 %.1f mm / 切片声明 %.1f mm（比例 %s），"
            "归一化 %.6f g/mm，共 %d 个进度点",
            total_mm, head_mm or 0.0,
            ("%.3f" % ratio) if ratio is not None else "无",
            eff or 0.0, len(parser.points))

    def _persist(self, parser: _Parser, size: int, final: bool,
                 points: list[list[float]] | None = None,
                 parsed_pct: float | None = None) -> None:
        """把表存成 JSON：进程重启后不用重下几十 MB 才恢复。"""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "gcode": self.gcode_name,
                "size": size,
                "bytes_done": parser.bytes,
                "final": final,
                "verified": bool(self.verified),
                "trusted": bool(self.trusted),
                "parsed_pct": parser.last_pct if parsed_pct is None else parsed_pct,
                "points": parser.points if points is None else points,
                "header": parser.header,
                "total_mm": parser.cum,
                "g_per_mm": parser.g_per_mm(),
                "eff_gpm": self.eff_gpm,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            }
            (self.cache_dir / (_safe_name(self.gcode_name) + ".json")).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
