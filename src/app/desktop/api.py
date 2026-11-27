"""Small asynchronous-to-Qt client for the local Python service."""
from __future__ import annotations

import json
from typing import Callable

import httpx
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class Signals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Request(QRunnable):
    def __init__(self, base_url, method, path, *, body=None, params=None, headers=None):
        super().__init__()
        self.signals = Signals()
        self.args = base_url, method, path, body, params, headers

    @Slot()
    def run(self):
        base_url, method, path, body, params, headers = self.args
        try:
            with httpx.Client(base_url=base_url, timeout=120, trust_env=False) as client:
                response = client.request(method, path, json=body, params=params, headers=headers)
            try:
                data = response.json()
            except ValueError:
                data = {"status": response.status_code, "text": response.text[:8000]}
            if response.is_error or (isinstance(data, dict) and
                    (data.get("ok") is False or str(data.get("code", "200")) not in ("0", "200"))):
                detail = data.get("error") or data.get("detail") or data.get("data") or data
                if not isinstance(detail, str):
                    detail = json.dumps(detail, ensure_ascii=False)
                raise ValueError(detail)
            self.signals.result.emit(data)
        except httpx.RequestError:
            self.signals.error.emit("无法连接本机 Python 服务，请检查服务状态和日志。")
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class Api(QObject):
    error = Signal(str)

    def __init__(self, base_url: str, parent=None):
        super().__init__(parent)
        self.base_url = base_url.rstrip("/")
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(6)
        self.pending: set[Request] = set()
        self.headers: dict[str, str] = {}

    def call(self, method: str, path: str, callback: Callable | None = None,
             *, body=None, params=None, quiet=False, done=None, error=None):
        request = Request(self.base_url, method, path, body=body, params=params,
                          headers=dict(self.headers))
        self.pending.add(request)
        if callback:
            request.signals.result.connect(callback)
        if not quiet:
            request.signals.error.connect(self.error.emit)
        if error:
            request.signals.error.connect(error)
        request.signals.finished.connect(lambda: self.pending.discard(request))
        if done:
            request.signals.finished.connect(done)
        self.pool.start(request)

    def get(self, path, callback=None, **kwargs):
        self.call("GET", path, callback, **kwargs)

    def post(self, path, callback=None, **kwargs):
        self.call("POST", path, callback, **kwargs)
