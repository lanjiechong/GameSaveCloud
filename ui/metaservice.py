# -*- coding: utf-8 -*-
"""游戏图标与中文名的批量匹配服务。

为什么单独一层：
    「搜 Steam + 下图 + 写库」全是网络和磁盘 IO，绝不能挂在刷新流程里
    同步跑 —— 刷新是用户点一下就要看到列表的动作，几十款游戏逐个等
    Steam 响应会卡到没法用。这里丢进独立线程池，刷新先把列表画出来，
    匹配结果随后一个个回填。

线程边界（重要）：
    工作线程：Steam 搜索 + 下载原图 + 解码成 QImage（QImage 线程安全）
    主线程：  QImage → QPixmap 圆角方块 → 存 PNG → 写 game_meta 表
    QPixmap / QPainter 只能在 GUI 线程用，所以规整与落盘都在主线程。

失败策略：
    匹配失败只写状态，不改游戏本身的任何字段 —— 列表照常显示原名、
    照常显示占位图标。下次刷新按指数退避重试（见 gamemeta.should_attempt）。
"""
import threading
import time

from PySide6.QtCore import QObject, QThreadPool, Signal

from gamesave import gamemeta, gameindex
from .workers import Worker


class MetaService(QObject):
    """批量补图标与中文名。"""

    meta_ready = Signal(object)      # Game（字段已回填，可直接塞回列表项）
    progress = Signal(int, int)      # 已完成, 总任务数
    finished = Signal(int, int)      # 成功数, 失败数

    def __init__(self, vault, logos, parent=None, lookup=None,
                 local_index="auto", use_bangumi=False):
        super().__init__(parent)
        self.vault = vault
        self.logos = logos
        # 查找函数可注入：测试里换成假实现，就不会真去连 Steam。
        self._lookup = lookup or gamemeta.lookup
        # 本地离线索引：命中即返回、零网络。
        # "auto" 用自带索引；传 None 显式禁用（测试里避免读到真实索引）。
        self._local_index = (gameindex.lookup if local_index == "auto"
                             else local_index)
        self._use_bangumi = bool(use_bangumi)
        # 独立线程池，3 并发：搜索接口对突发请求比较敏感，
        # 而且图标下载只占带宽不占 CPU，没必要开更多。
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(3)
        self._queued = set()          # 已在队列/进行中的 game_id，防重复提交
        self._lock = threading.Lock()
        self._active = set()          # Worker 强引用，防止 run() 中被 GC
        self._total = 0
        self._done = 0
        self._ok = 0
        self._bad = 0

    # ==========================================================
    # 对外接口
    # ==========================================================
    def ensure(self, games) -> int:
        """为缺图标/中文名的游戏排队匹配，返回本次新排队的数量。

        已有图标与中文名的不产生任何网络请求；上次失败且还在退避期内的
        也跳过。返回 0 表示这一轮没有要做的事。
        """
        metas = self.vault.list_meta()
        now = time.time()
        todo = []
        for g in games or ():
            if g is None or not getattr(g, "id", ""):
                continue
            if g.id in self._queued:
                continue
            if not gamemeta.should_attempt(g.name, metas.get(g.id),
                                           self.logos.has_game_icon(g), now):
                continue
            todo.append((g, metas.get(g.id)))
        if not todo:
            return 0

        with self._lock:
            self._total += len(todo)
            total = self._total
        for g, meta in todo:
            self._queued.add(g.id)
            self._start(g)
        self.progress.emit(self._done, total)
        return len(todo)

    def pending(self) -> int:
        """还没回填结果的数量。"""
        with self._lock:
            return max(self._total - self._done, 0)

    def wait_idle(self, msec: int = 8000) -> bool:
        """阻塞到线程池清空。仅供测试与退出前收尾。"""
        return self._pool.waitForDone(msec)

    # ==========================================================
    # 任务提交
    # ==========================================================
    def _start(self, game):
        def job(_sig):
            # Worker.run() 会把 signals 作为第一个位置参数传进来，
            # 这里用不到但必须收下，否则 TypeError。
            res = self._lookup(game.name, fetch=self.logos.fetch_bytes,
                               local_index=self._local_index,
                               use_bangumi=self._use_bangumi)
            img = None
            if res.get("status") == gamemeta.STATUS_OK and res.get("appid"):
                img = self.logos.fetch_logo_image(res.get("appid"))
            return game, res, img

        w = Worker(job)
        w.signals.result.connect(self._on_result)

        def on_error(msg):
            # Worker 把异常和 traceback 拼成一段，落库只要第一行
            self._on_failure(game, msg)

        w.signals.error.connect(on_error)

        def _release(*_a):
            with self._lock:
                self._active.discard(w)

        w.signals.result.connect(_release)
        w.signals.error.connect(_release)
        with self._lock:
            self._active.add(w)
        self._pool.start(w)

    # ==========================================================
    # 结果回填（主线程）
    # ==========================================================
    def _on_result(self, res):
        game, lookup, img = res
        meta = self.vault.get_meta(game.id) or {}
        attempts = int(meta.get("attempts") or 0) + 1

        # QPixmap 转换与落盘必须在主线程。图没下到就留空，
        # 沿用上一轮已经存下的那张（如果有）。
        logo_file = ""
        if img is not None and not img.isNull():
            pm = self.logos.to_tile(img)
            if pm is not None and not pm.isNull():
                logo_file = self.logos.save_game_icon(game.id, pm)

        name_zh = (lookup.get("name_zh") or "").strip()
        if not name_zh and lookup.get("appid"):
            # Steam 国区只有英文名时，用离线包里的社区译名补一手
            name_zh = self.logos.pack_name_zh(lookup.get("appid"))

        state = lookup.get("status") or gamemeta.STATUS_ERROR
        ok = state == gamemeta.STATUS_OK
        now = time.time()
        try:
            self.vault.save_meta(
                game.id,
                name_zh=name_zh or meta.get("name_zh") or "",
                official_name=(lookup.get("official_name")
                               or meta.get("official_name") or ""),
                appid=lookup.get("appid") or meta.get("appid") or "",
                logo_file=logo_file or meta.get("logo_file") or "",
                source="steam",
                state=state,
                attempts=attempts,
                checked_at=now,
                # 查到了就清掉退避；没查到按次数往后推
                next_retry_at=0.0 if ok else now + gamemeta.retry_delay(attempts),
                error=(lookup.get("error") or "")[:500])
        except Exception as e:
            # 写库失败不能让匹配流程整体崩掉：图标已经落盘，
            # 下次刷新按「缺记录」重跑一遍即可。
            print("保存游戏元数据失败：%s" % e)

        self._apply(game, meta, name_zh, logo_file, state)
        self._finish_one(game, ok)
        self.meta_ready.emit(game)

    def _on_failure(self, game, msg):
        meta = self.vault.get_meta(game.id) or {}
        attempts = int(meta.get("attempts") or 0) + 1
        now = time.time()
        first = ((msg or "").splitlines() or ["未知错误"])[0][:500]
        try:
            self.vault.save_meta(
                game.id, state=gamemeta.STATUS_ERROR, attempts=attempts,
                checked_at=now,
                next_retry_at=now + gamemeta.retry_delay(attempts),
                error=first)
        except Exception:
            pass
        game.meta_state = gamemeta.STATUS_ERROR
        self._finish_one(game, False)
        self.meta_ready.emit(game)

    @staticmethod
    def _apply(game, old_meta, name_zh, logo_file, state):
        """把匹配结果贴回内存对象，供界面直接使用。"""
        game.name_zh = name_zh or old_meta.get("name_zh") or ""
        game.logo_file = logo_file or old_meta.get("logo_file") or ""
        game.meta_state = state

    # ==========================================================
    # 计数
    # ==========================================================
    def _finish_one(self, game, ok):
        self._queued.discard(getattr(game, "id", ""))
        with self._lock:
            self._done += 1
            if ok:
                self._ok += 1
            else:
                self._bad += 1
            done, total = self._done, self._total
            ok_n, bad_n = self._ok, self._bad
        self.progress.emit(done, total)
        if done >= total:
            with self._lock:
                self._total = self._done = self._ok = self._bad = 0
            self.finished.emit(ok_n, bad_n)
