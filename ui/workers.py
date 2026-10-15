# -*- coding: utf-8 -*-
"""后台工作线程。

所有磁盘/网络操作都放这里，主线程只负责画界面。
每个 worker 通过信号回报进度与结果，绝不直接碰 UI 对象。
"""
import traceback

from PySide6.QtCore import QObject, QRunnable, Signal, QThreadPool


class OperationCancelled(Exception):
    """用户点了「取消」，由进度回调在工作线程内主动抛出。

    在文件粒度中止：当前正在处理的那个文件走完，下一个文件开始前停。
    """


class WorkerSignals(QObject):
    progress = Signal(str, int, int)      # 阶段, 当前, 总计
    log = Signal(str)
    result = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    """把任意可调用对象丢进线程池执行。"""

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self):
        try:
            out = self.fn(self.signals, *self.args, **self.kwargs)
            self.signals.result.emit(out)
        except Exception as e:
            try:
                self.signals.error.emit("%s\n%s" % (e, traceback.format_exc(limit=3)))
            except Exception:
                pass


# 运行中的 worker 强引用表。
# QRunnable 被 start() 后如果 Python 侧没有任何引用，垃圾回收可能在我们还在
# run() 执行时就把 Worker 连它的 signals QObject 一起回收掉，导致 result/error
# 信号永远发不到主线程（现象：worker 明明跑完了，界面却一直卡在忙碌状态）。
# 在这里留一份强引用，任务结束时移除。
_ACTIVE = set()
_LOCK = __import__("threading").Lock()


def run_async(fn, on_result=None, on_error=None, on_progress=None, on_log=None):
    """提交任务，返回 (worker, signals)。调用方自行连接信号。"""
    w = Worker(fn)
    s = w.signals
    with _LOCK:
        _ACTIVE.add(w)

    def _release(*_a):
        with _LOCK:
            _ACTIVE.discard(w)

    s.result.connect(_release)
    s.error.connect(_release)
    if on_result:
        s.result.connect(on_result)
    if on_error:
        s.error.connect(on_error)
    if on_progress:
        s.progress.connect(on_progress)
    if on_log:
        s.log.connect(on_log)
    QThreadPool.globalInstance().start(w)
    return w, s


def active_count():
    with _LOCK:
        return len(_ACTIVE)
