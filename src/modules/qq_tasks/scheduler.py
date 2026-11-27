"""QQ 音乐每日调度；同一北京时间日期最多触发一次，手动执行不受限制。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
from typing import Callable
import uuid

from modules.qq_tasks import core


# 中国标准时间现行规则为 UTC+8，使用标准库即可在没有 tzdata 的 Windows 上运行。
SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
DEFAULT_TIME = "09:30"
_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_LOG = logging.getLogger(__name__)


def validate_config(config: dict) -> None:
    """供保存配置时共用；旧配置未提供定时字段时按关闭、09:30 处理。"""
    if not isinstance(config.get("schedule_enabled", False), bool):
        raise ValueError("schedule_enabled 必须是布尔值")
    value = config.get("schedule_time", DEFAULT_TIME)
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        raise ValueError("schedule_time 必须是 24 小时制 HH:MM，例如 09:30")


def _local_now() -> datetime:
    return datetime.now(SHANGHAI)


def _beijing(value: datetime) -> datetime:
    return value.replace(tzinfo=SHANGHAI) if value.tzinfo is None else value.astimezone(SHANGHAI)


class DailyScheduler:
    """到点后补跑当天一次；当天已触发后改时间或重新启用也不重复。

    每轮读取最新配置。未触发时修改时间立即影响当天；已触发后新时间从次日
    起生效。忙时、未登录或所有任务关闭时不占用当天名额，稍后继续检查。
    先持久化日期占位，再启动任务；若进程恰好在两者之间退出，当天保留占位，
    可手动执行补跑，避免重启造成重复动作。
    """

    def __init__(self, *, config_loader: Callable | None = None, task=None,
                 record_dir: Path | None = None, clock: Callable | None = None,
                 interval: float = 20):
        self._config_loader = config_loader or core.load_music
        self._task = task or core.TASK
        self._record_dir = Path(record_dir) if record_dir is not None else core.HOME / "schedule_runs"
        self._clock = clock or _local_now
        self._interval = interval
        self._tick_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._last_error = ""

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return False
            event = threading.Event()
            self._stop_event = event
            self._thread = threading.Thread(target=self._loop, args=(event,),
                                            name="qq-music-scheduler", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lifecycle_lock:
            event, thread = self._stop_event, self._thread
            if event:
                event.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        with self._lifecycle_lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
                self._stop_event = None

    def _loop(self, event: threading.Event) -> None:
        while not event.is_set():
            try:
                self.tick()
                self._last_error = ""
            except Exception as exc:  # noqa: BLE001 - 一次错误不能结束每日调度。
                # 不记录异常正文，避免配置或第三方异常带出登录信息。
                self._last_error = type(exc).__name__
                _LOG.warning("QQ 音乐定时检查失败（%s）", self._last_error)
            event.wait(self._interval)

    def _record_path(self, date: str) -> Path:
        return self._record_dir / (date + ".json")

    @staticmethod
    def _read_record(path: Path) -> dict:
        try:
            record = json.loads(path.read_text("utf-8"))
            if isinstance(record, dict):
                return record
        except (OSError, ValueError):
            pass
        # 即使中途断电导致记录不完整，也不能据此自动重跑当天任务。
        return {"date": path.stem, "status": "unreadable"}

    @staticmethod
    def _write_record(path: Path, record: dict) -> None:
        temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("x", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def tick(self, now: datetime | None = None) -> bool:
        """检查一次，返回是否启动；不请求账号接口，实际任务沿用 core.TASK。"""
        if not self._tick_lock.acquire(blocking=False):
            return False
        try:
            config = self._config_loader()
            validate_config(config)
            if not config.get("schedule_enabled", False) or not config.get("cookie"):
                return False
            if not any(config.get(key, True) for key in ("sign_enable", "medal_enable", "card_pool_enable")):
                return False
            current = _beijing(now or self._clock())
            hhmm = config.get("schedule_time", DEFAULT_TIME)
            if current.strftime("%H:%M") < hhmm or self._task.running:
                return False
            date = current.date().isoformat()
            path = self._record_path(date)
            self._record_dir.mkdir(parents=True, exist_ok=True)
            record = {"date": date, "timezone": "Asia/Shanghai", "scheduled_time": hhmm,
                      "claimed_at": current.isoformat(), "status": "claimed"}
            try:
                # 独占创建也保护多个应用进程同时检查同一天的情况。
                handle = path.open("x", encoding="utf-8")
            except FileExistsError:
                return False
            try:
                with handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                path.unlink(missing_ok=True)  # 尚未调用 start，可以安全重试。
                raise
            try:
                started = self._task.start()
            except Exception as exc:
                # start 抛错时可能已启动线程，保留占位，避免不确定状态下重跑。
                record.update(status="start_error", error=type(exc).__name__)
                self._write_record(path, record)
                raise
            if not started:
                path.unlink(missing_ok=True)  # 与手动执行竞争失败，空闲后再检查。
                return False
            record.update(status="started", started_at=current.isoformat())
            self._write_record(path, record)
            return True
        finally:
            self._tick_lock.release()

    def snapshot(self) -> dict:
        config = self._config_loader()
        with self._lifecycle_lock:
            active = bool(self._thread and self._thread.is_alive())
        files = sorted(self._record_dir.glob("????-??-??.json"), reverse=True)
        return {"enabled": config.get("schedule_enabled", False),
                "time": config.get("schedule_time", DEFAULT_TIME), "timezone": "Asia/Shanghai",
                "active": active, "last_trigger": self._read_record(files[0]) if files else None,
                "last_error": self._last_error}


_SCHEDULER = DailyScheduler()


def start_scheduler() -> bool:
    return _SCHEDULER.start()


def stop_scheduler() -> None:
    _SCHEDULER.stop()


def snapshot() -> dict:
    return _SCHEDULER.snapshot()
