"""按目标文件协调跨进程写入，并原子替换JSON。"""
from __future__ import annotations

import json
import errno
import os
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import time
from weakref import WeakValueDictionary

WRITE_LOCK = threading.RLock()
_RESOURCE_LOCKS = WeakValueDictionary()
_RESOURCE_GUARD = threading.Lock()


def _resource_lock(path):
    key=os.path.normcase(str(path.resolve()))
    with _RESOURCE_GUARD:
        lock=_RESOURCE_LOCKS.get(key)
        if lock is None:
            lock=threading.RLock();_RESOURCE_LOCKS[key]=lock
        return lock


def _acquire(lock):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(lock):
    lock.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked_file(path: Path, timeout: float | None = None, cancelled=None):
    """跨进程文件锁；等待时可取消，超时给出可读错误而不是无限挂起。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout if timeout is not None else None
    def check_wait():
        if cancelled:cancelled()
        if deadline is not None and time.monotonic()>=deadline:
            raise TimeoutError(f"等待文件锁超时：{path.name}")
    mutex=_resource_lock(path)
    check_wait()
    while not mutex.acquire(timeout=.1):check_wait()
    try:
        with path.with_name(path.name + ".lock").open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            while True:
                try:
                    _acquire(lock)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES,errno.EAGAIN,errno.EDEADLK):raise
                    lock.seek(0)
                    check_wait()
                    time.sleep(.1)
            try:
                yield
            finally:
                _release(lock)
    finally:
        mutex.release()


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as fh:
            temp_name = fh.name
            json.dump(data, fh, ensure_ascii=False, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
