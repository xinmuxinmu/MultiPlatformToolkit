"""Background streamed uploads for native local-service operations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import httpx
from PySide6.QtCore import QRunnable, Slot

from .api import Signals


class UploadRequest(QRunnable):
    def __init__(self, base_url, path, file, *, params=None, headers=None):
        super().__init__()
        self.signals = Signals()
        self.base_url, self.path, self.file = base_url, path, Path(file)
        self.params, self.headers = params or {}, dict(headers or {})

    @Slot()
    def run(self):
        try:
            size = self.file.stat().st_size
            if size <= 0 or size > 2 * 1024 * 1024 * 1024:
                raise ValueError("请选择大小在 1 字节至 2 GB 之间的 SQLite 数据库文件。")
            headers = {**self.headers, "Content-Type": "application/octet-stream", "Content-Length": str(size)}
            with self.file.open("rb") as source:
                if source.read(16) != b"SQLite format 3\x00":
                    raise ValueError("所选文件不是 SQLite 3 数据库。")
                source.seek(0)
                with httpx.Client(base_url=self.base_url, timeout=600, trust_env=False) as client:
                    response = client.post(self.path, content=source, params=self.params, headers=headers)
            try:
                data = response.json()
            except ValueError:
                data = {"status": response.status_code, "text": response.text[:8000]}
            if response.is_error or (isinstance(data, dict) and data.get("ok") is False):
                detail = data.get("error") or data.get("detail") or data
                raise ValueError(detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False))
            self.signals.result.emit(data)
        except httpx.RequestError:
            self.signals.error.emit("数据库上传中断，请确认本机服务仍在运行。")
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


def upload(api, path, file, params=None, callback=None, done=None):
    """Use Api's pool, credentials, lifetime tracking and error channel."""
    request = UploadRequest(api.base_url, path, file, params=params, headers=dict(api.headers))
    api.pending.add(request)
    if callback:
        request.signals.result.connect(callback)
    request.signals.error.connect(api.error.emit)
    request.signals.finished.connect(lambda: api.pending.discard(request))
    if done:
        request.signals.finished.connect(done)
    api.pool.start(request)
    return request


class DownloadRequest(QRunnable):
    """Save a PNG response as bytes; error JSON must never replace an image file."""
    def __init__(self, base_url, path, file, *, params=None, headers=None):
        super().__init__()
        self.signals = Signals()
        self.base_url, self.path, self.file = base_url, path, Path(file)
        self.params, self.headers = params or {}, dict(headers or {})

    @Slot()
    def run(self):
        temporary = self.file.with_name(self.file.name + "." + uuid.uuid4().hex + ".part")
        try:
            with httpx.Client(base_url=self.base_url, timeout=600, trust_env=False) as client:
                with client.stream("GET", self.path, params=self.params, headers=self.headers) as response:
                    if response.is_error:
                        response.read()
                        try:
                            value = response.json()
                            detail = value.get("detail") or value.get("error") or value
                        except ValueError:
                            detail = response.text[:1000]
                        raise ValueError(str(detail))
                    if response.headers.get("content-type", "").split(";", 1)[0].lower() != "image/png":
                        raise ValueError("服务未返回 PNG 图片，原目标文件未修改。")
                    size, signature = 0, b""
                    with temporary.open("wb") as target:
                        for chunk in response.iter_bytes():
                            if not chunk:
                                continue
                            if len(signature) < 8:
                                signature += chunk[:8 - len(signature)]
                            target.write(chunk)
                            size += len(chunk)
                    if signature != b"\x89PNG\r\n\x1a\n":
                        raise ValueError("图片响应内容不完整或格式不正确，原目标文件未修改。")
            os.replace(temporary, self.file)
            self.signals.result.emit({"path": str(self.file), "bytes": size})
        except httpx.RequestError:
            self.signals.error.emit("长图生成或下载中断，请保持本机服务运行后重试。")
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.signals.finished.emit()


def download(api, path, file, params=None, callback=None, done=None):
    request = DownloadRequest(api.base_url, path, file, params=params, headers=dict(api.headers))
    api.pending.add(request)
    if callback:
        request.signals.result.connect(callback)
    request.signals.error.connect(api.error.emit)
    request.signals.finished.connect(lambda: api.pending.discard(request))
    if done:
        request.signals.finished.connect(done)
    api.pool.start(request)
    return request
