# GameSaveCloud

跨平台桌面端的游戏存档备份与同步工具，对标 UU 加速器「云存档」。
核心承诺：**任何情况下不丢存档** —— 覆盖、回滚、冲突、删除，每一步之前都先留底。

约 5600 行，PySide6 界面 + 标准库后端，零强制第三方依赖（`zstandard` 仅为可选加速）。
已打包为 Windows 单文件 exe，**双击即用，无需安装 Python**。

**本地备份为主，云备份为可选项。** 开箱即用，不配任何云端也能完整工作：
备份、历史版本、回滚、冲突检测全部在本地完成。只有在设置里勾选「启用云端同步」
并填好位置后，才会把存档同步到本地目录 / 移动硬盘 / NAS / WebDAV。

---

## 1. 快速开始

### 方式一：直接用打包好的 exe（推荐，无需装任何东西）

```
dist\GameSaveCloud.exe        双击启动图形界面（无控制台窗口）
dist\GameSaveCloud-CLI.exe    命令行版，供计划任务 / 脚本调用
```

两个都是**单文件**，拷到任何一台 Windows 机器上都能直接跑。
数据存放在 `%LOCALAPPDATA%\GameSaveCloud\`，换机器用只需把 exe 拷过去。

```cmd
GameSaveCloud-CLI.exe status    :: 各游戏同步状态
GameSaveCloud-CLI.exe scan      :: 扫描本机存档目录
GameSaveCloud-CLI.exe backup    :: 立即备份全部
GameSaveCloud-CLI.exe sync      :: 同步全部（未配云端时等同 backup）
```

CLI 版退出码：`0` 成功 / `1` 用法错误 / `2` 存在未处理的冲突 —— 可以直接被计划任务判断。

### 方式二：从源码运行

```bash
pip install -r requirements.txt
python run.py                    # 图形界面
python run.py --cli status       # 命令行（注意要带 --cli 前缀）
```

> `python run.py` 报 `ModuleNotFoundError: No module named 'PySide6'`
> 是因为系统 Python 没装依赖。装一下 `requirements.txt`，或者直接用上面的 exe。

### 跑测试

```bash
python tests/test_core.py                                # 47 个核心引擎单元测试
QT_QPA_PLATFORM=offscreen python tests/smoke_gui.py      # GUI 离屏冒烟测试
```

无 `zstandard` 也能完整运行，压缩自动降级到标准库 `zlib`。

### 自己重新打包

```bash
pip install pyinstaller
pyinstaller GameSaveCloud.spec --noconfirm --clean
```

`GameSaveCloud.spec` 里排除了约 30 个确定用不到的 Qt 模块
（WebEngine / 3D / Quick / Multimedia / 蓝牙 / 定位等），
两个 exe 各 46 MB 左右。排除列表是保守的：QtWidgets 运行时会拉
QtGui / QtCore / QtNetwork / QtOpenGL / QtPrintSupport，这些都不排。
`ui/assets/`（应用图标）通过 spec 的 `datas` 一起打进去。


---

## 2. 需求对照

| # | 需求 | 实现位置 |
|---|------|---------|
| 1 | 自动扫描常见存档目录 + 手动添加自定义路径 | `gamesave/scanner.py`（150 款已知游戏查表 + 启发式扫描），`ui/dialogs.py::AddGameDialog` |
| 2 | 上传/下载/增量同步、压缩、去重、断点续传 | `gamesave/chunker.py` + `gamesave/store.py` + `gamesave/remote.py` + `gamesave/sync.py` |
| 3 | 版本历史、查看、对比、一键回滚 | `gamesave/vault.py::restore_snapshot`，`HistoryDialog` / `DiffDialog` |
| 4 | 冲突检测与三选一处理 | `gamesave/sync.py::_decide`，`ConflictDialog` |
| 5 | 覆盖/恢复前自动本地留底 | `gamesave/vault.py::_backup_existing` → `localcopies/` |
| 交互 | 图形界面展示状态/时间/占用 | `ui/main_window.py`（自绘列表 + 状态卡片），图标见 `ui/logos.py`、排版见 `ui/game_list.py` |
| 交互 | 危险操作二次确认 | `ConfirmDialog`（默认焦点在「取消」，回滚还需勾选确认框） |
| 交互 | 定时自动备份 + 静默同步 | `ui/main_window.py::_build_timer` / `_on_tick`，`config.schedule` |
| 交互 | 网络异常/冲突清晰提示 | 后台失败只记日志 + 置错误态；冲突中断同步并弹窗交人决策 |
| 交互 | **本地备份为主，云端可选** | `config.remote.enabled` 默认 `false`；未配置时 `remote` 后端对象根本不创建，界面文案从「同步」切换为「备份」（见 §10） |

---

## 3. 整体架构

```
┌───────────────────────────────────────────────────────────────┐
│  ui/                      界面层（PySide6，只画不干活）        │
│  main_window.py           主窗口、队列式同步编排                │
│  dialogs.py               确认 / 冲突 / 历史 / 差异 / 设置      │
│  game_list.py             游戏列表自绘委托（图标+两行文字）      │
│  logos.py                 游戏图标三级保障 + Steam 联网检索      │
│  workers.py               QRunnable + QThreadPool，信号回传     │
│  theme.py                配色与样式表                          │
│  assets/                  应用图标（PNG + 多尺寸 ICO）           │
└──────────────────────────┬────────────────────────────────────┘
                           │ 信号（progress / log / result / error）
┌──────────────────────────▼────────────────────────────────────┐
│  gamesave/                引擎层（纯 Python，可独立于 GUI 用）  │
│                                                               │
│  sync.py      SyncEngine     三方合并判定 + 执行编排            │
│  vault.py     Vault          快照 / 还原 / 留底 / SQLite 索引   │
│  store.py     ChunkStore     内容寻址 chunk 存储（去重实体）     │
│  chunker.py                 分块 / SHA-256 / 自描述压缩         │
│  remote.py   RemoteBackend  ┬ LocalFolderBackend               │
│                             └ WebDAVBackend（纯标准库）         │
│  scanner.py                 存档目录发现                        │
│  models.py                  dataclass 数据模型                  │
│  config.py                  跨平台配置（含云端总开关）           │
└──────────────────────────┬────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   本地游戏目录       vault/（本地）      remote/（云端）
```

三条设计红线：

1. **界面层不碰 IO。** 所有磁盘/网络操作都通过 `workers.run_async` 丢进线程池，
   主线程只接收信号更新控件。`MainWindow` 里没有任何 `open()` / `urlopen()`。
2. **引擎层不碰 Qt。** `gamesave/` 不 import PySide6，因此可以在
   `python run.py --cli` 下、在 CI 里、在测试里直接跑。
3. **上层不关心云端是什么。** `RemoteBackend` 只暴露 5 类原语
   （快照清单 / 读写快照 / chunk 存在性 / 上传 / 下载），
   换 WebDAV、换对象存储都不用动同步逻辑。

---

## 4. 数据同步流程

### 4.1 为什么必须是三方合并

只有「本地」和「云端」两个点时，**「哪边改的」在数学上无法判断** ——
两边不一样就只能是冲突。于是任何一次正常的上传/下载都会被当成冲突弹窗，
产品直接不可用。

引入 **base**（上次成功同步时两边一致的那个快照）之后，每个文件都能归因：

```
       本地      云端      基准      判定
       ────      ────      ────      ─────────────────────
        A         A        A        无事
        A         B        A        云端改了   → 下载
        B         A        A        本地改了   → 上传
        B         C        A        两边都改了 → 冲突，交人
        A        —         A        云端删了   → 本地也删（先留底）
        —         A        A        本地删了   → 云端也删
        B         C        —        两边新增   → 冲突
```

这正是 Git / Syncthing / Dropbox 共同的做法。判定函数在
`SyncEngine._decide(l_sha, r_sha, b_sha)`，是一个无副作用的纯函数，
`tests/test_core.py::TestDecide` 把整张真值表逐格钉住。

### 4.2 为什么一律用内容哈希，不用修改时间

存档文件的时间戳不可信。最典型的是 Cemu（Wii U 模拟器）——
它用虚拟时钟写存档，`mtime` 永远是 **2077-07-07**。按 mtime 判断新旧，
「2077 年的文件比 2024 年的新」会直接覆盖掉玩家的真实进度。

所以 `FileEntry.sha256` 是整文件内容的 SHA-256，`mtime` 只用于界面展示。
`chunk_file()` 在分块的同时增量算出整文件哈希，不需要为算校验值再把文件读一遍。

### 4.3 一次同步的六步

`SyncEngine.execute(game, plan, resolutions)`：

```
1. 应用冲突裁决
   keep_local  → 改成上传        use_remote  → 改成下载
   save_both   → 上传本地为主，云端版本另存为「文件名.云端版本_时间戳」
   未裁决的冲突 → 记 error，不推进基准（下次重试，不把半成品当已同步）

2. 上传缺失 chunk（断点续传，云端已有的直接跳过）

3. 下载缺失 chunk
   ACT_DOWNLOAD 要取；save_both 的云端版本也要取（它要另存一份）

4. 构建合并后的快照 → 写本地 vault → 传云端清单
   任一步失败即返回，base 不推进

5. 把「下载」和「另存的云端版本」写到游戏目录
   覆盖前一律先 _backup_existing() 留底

6. 本地删除（云端已删的文件），同样先留底

最后 set_sync_state(base_snapshot_id=新快照, last_sync_at=now)
```

**「有失败就不推进 base」** 是关键：如果传了一半就更新基准，
下次同步会认为「云端那半截已经是共识」，玩家的进度就真丢了。

### 4.4 增量同步是怎么做到不重传的

```
存档文件（3 MiB）
   │  chunker.chunk_file()：固定 1 MiB 切块，逐块 SHA-256
   ▼
┌──────────┐ ┌──────────┐ ┌──────────┐
│ chunk 0  │ │ chunk 1  │ │ chunk 2  │     改第 2 块中间 10 字节
│ sha=aa..│ │ sha=bb..│ │ sha=cc..│     → 只有 bb 变，aa/cc 不变
└──────────┘ └──────────┘ └──────────┘
      │            │            │
      ▼            ▼            ▼
   chunks/aa/  chunks/bb/  chunks/cc/     内容寻址，按哈希命名
```

同步前先问云端 `missing_chunks([aa, bb, cc])`，只传云端没有的那几个。
测试 `test_incremental_only_changed_chunks` 验证：3 MiB 文件改 10 字节，
重传量远小于 3 MiB。

同一个 chunk 在磁盘上**只有一份**。游戏 A 和游戏 B 的存档只要内容相同就共享；
同一游戏相邻两个版本通常只差几个 chunk，所以「保留 30 个历史版本」的
额外磁盘开销远小于 30 倍。测试 `test_same_content_two_games_share_one_chunk`。

### 4.5 断点续传

- **本地目录后端**：上传写 `_tmp/<sha>.part`，若 `.part` 大小已达目标就直接 `os.replace` 改名；下载同理比对 `.part` 已下载字节数。
- **WebDAV 后端**：上传前先 `HEAD` 问云端已有多少字节，用 `Content-Range` 从断点续传；下载发 `Range: bytes=N-` 追加到 `.part`，完成再原子改名。

### 4.6 chunk 自描述压缩

```
磁盘上的一个 chunk 文件：
┌────────┬──────────────────────────────┐
│ 1 字节  │ 压缩载荷                      │
│ z/l/r  │ zstd / zlib / 原样            │
└────────┴──────────────────────────────┘
文件名 = 原始内容的 SHA-256（与压缩算法无关）
```

首字节是算法标签，**任何后端拿到文件就能解压，不依赖外部元数据**。
这一点是踩过坑才这么设计的：早期把压缩算法存在数据库里，
结果换一个后端/换一台机器就读不出来。

压缩后如果更大（随机数据、已压缩的视频），就原样存放并打 `r` 标签 ——
不为「用了压缩」这个形式感付出体积代价。

---

## 5. 存储结构

```
vault/                                  本地保险库（默认 %LOCALAPPDATA%/GameSaveCloud/GameSaveVault）
├── chunks/                             内容寻址 chunk store
│   ├── aa/
│   │   └── a1b2c3...                   首字节算法标签 + 压缩载荷
│   ├── bb/
│   │   └── ...
│   └── _tmp/                           正在写的 .part（count/total_size 不计入）
├── snapshots/
│   └── <snapshot_id>.json              每个快照一份人可读 JSON，便于排查
├── localcopies/                        覆盖 / 回滚 / 删除前的安全留底
│   └── <game_id8>_<时间戳>_<路径>.副本
├── index.db                            SQLite（WAL 模式）
└── config.json                         应用配置

remote/                                 云端（默认 %LOCALAPPDATA%/GameSaveCloud/GameSaveCloud）
├── chunks/                             与 vault 相同的布局 → 整块盘拔下来就是备份
└── snapshots/<sid>.json
```

### SQLite 表结构

| 表 | 作用 |
|---|---|
| `games` | 游戏清单（id / 名称 / 多个存档路径 / 平台） |
| `snapshots` | 快照元数据（id / 游戏 / 时间 / 类型 manual·auto·sync / 体积） |
| `files` | 快照内的文件（路径 / 整文件哈希 / 大小 / mtime / chunk 序列） |
| `chunk_refs` | 快照 → chunk 的反查表，`gc()` 靠它算「谁还被引用」 |
| `sync_state` | 每游戏的 base_snapshot_id / last_sync_at / last_error |
| `local_copies` | 留底流水（游戏 / 原路径 / 备份路径 / 原因） |

### 多存档目录的虚拟根

一个游戏可能有多个存档目录（例如 `Documents/My Games/X` + `AppData/Roaming/X`）。
单目录时快照里的路径是相对路径；多目录时加 `序号/` 前缀：

```
快照内路径          还原到
0/saves/a.sav    →  <第 0 个目录>/saves/a.sav
1/config/b.ini   →  <第 1 个目录>/config/b.ini
```

序号前缀超出当前目录数时**跳过**而不是猜一个位置写下去
（测试 `test_stale_index_is_ignored`）。

### 快照保留策略

`Vault.prune_snapshots(game_id, keep)` 与
`LocalFolderBackend.prune_snapshots(game_id, keep)` 分别裁剪本地与云端，
只保留最近的 `safety.keep_snapshots` 份，裁完跑一次 gc 回收失去引用的 chunk。

两个保护：

1. **当前同步基准不删。** `sync_state.base_snapshot_id` 是三方合并的参照物，
   删了会让下次同步退化成「拿云端倒数第二版当基准」。
2. **无变化的同步不生成快照。** `execute()` 里若所有动作都是 `ACT_NONE`
   就直接返回。否则一小时一次的定时同步会在无变化时也造一份，
   一年就是近九千份几乎相同的清单，把保留配额全部占满
   （测试 `test_noop_sync_creates_no_snapshot`）。

---

## 6. 关键设计决策与踩坑记录

这些不是「理论上的考虑」，每一条背后都是一个真实发生过的故障。

### 6.1 SQLite 连接必须能被工作线程用

Vault（含 SQLite 连接）在主线程构造，但所有 IO 都跑在 `QThreadPool` 里：

```
ProgrammingError: SQLite objects created in a thread can only be used
in that same thread. The object was created in thread id 43400 and
this is thread id 48072.
```

修法：连接开 `check_same_thread=False`，再用 `_LockedDB` 套一把可重入锁。
光开 `check_same_thread=False` 不够 —— 连接内部的语句缓存、事务状态、
行游标都不是线程安全的。

`_LockedDB.execute()` 返回 `fetchall()` 的结果而**不是 Cursor**：
Cursor 只有在被迭代时才与连接交互，若把 Cursor 传到锁外面再 `for`，
锁就形同虚设。返回物化的行列表才能保证「取数」完整地待在锁内。

### 6.2 后台任务失败绝不弹模态框

早期 `_err()` 一律 `QMessageBox.critical()`。启动后的首次刷新从工作线程
失败回来时，模态框霸占了事件循环 —— 用户没在看窗口，对话框却挂着，
后续所有刷新都进不来，界面永久卡在忙碌状态（离屏自测里就是死锁）。

现在拆成两个：

- `_err()` —— 用户主动点了按钮后在等结果，弹模态框合理；
- `_err_bg()` —— 后台任务（启动刷新、定时同步、托盘触发）只写日志 + 置错误态。

### 6.3 Worker 必须被强引用

`QRunnable.start()` 之后如果 Python 侧没有任何引用，垃圾回收可能在
`run()` 还没执行完时就把 Worker 连同它的 signals QObject 一起回收掉，
`result` / `error` 信号永远发不到主线程。现象是 worker 明明跑完了，
界面却一直卡在忙碌状态。

`workers.run_async()` 用模块级 `_ACTIVE` set + `threading.Lock` 持有强引用，
任务结束移除。

### 6.4 chunk 写入的临时文件要带线程号

多个工作线程可能同时拿到同一个 chunk。若共用 `_tmp/<sha>.part`，
两个写流会交错写同一个文件，且一方 `os.replace` 时另一方还持有打开句柄
（Windows 下 replace 直接 PermissionError）。
现在临时文件名是 `<sha>.<线程号>.part` —— 各自为政，而同一 sha 的内容必然相同。

### 6.5 原子写入

chunk 一律先写 `.part` 再 `os.replace()` 改名。`os.replace` 在同盘是原子的，
绝不会出现「半个文件被当成有效 chunk」。这一条配合 `store.verify()`
（解压后重新哈希比对）构成最后一道防线。

### 6.6 扫描器要降噪

第一版扫描器在普通用户机器上报了 **203 条**「疑似存档」，
里面混着 AMD 驱动、剪映、BitComet、网易云音乐……

降噪三条：

1. `AppData\Local` 与 `ProgramData` 被普通软件大量占据，**不参与启发式扫描**
   （已知游戏查表仍然覆盖它们）；
2. `.txt/.json/.ini/.xml/.cfg` 这类通用扩展名不能作为存档特征 ——
   几乎所有软件都会写这些文件；
3. 维护一份约 200 项的 `NOISE` 名单。

结果：203 → 22 → **15 条，且全部为真实存档目录**。

---

## 7. 安全模型

| 场景 | 保护措施 |
|---|---|
| 回滚覆盖现有存档 | 覆盖前 `shutil.copy2` 到 `localcopies/<game>_<时间戳>_<路径>` |
| 同步下载覆盖本地 | 同上，`reason="sync-overwrite"` |
| 云端删除传播到本地 | 删之前留底，`reason="sync-delete"` |
| 冲突选「使用云端」 | 被覆盖的本地版本留底后才写入 |
| 冲突选「另存为新版本」 | 两边都落盘，一个都不丢 |
| 回滚操作本身 | `ConfirmDialog` + 强制勾选「我已了解上述后果」 |
| 同步中途失败 | 不推进 base，下次重试，不会把半成品当共识 |
| chunk 静默损坏 | `store.verify()` 解压后重算哈希 |

**删档类操作永远不自动做。** 快照里没有但本地存在的文件，还原时不删 ——
把删档的决定权留给人。

---

## 8. 测试

```bash
python tests/test_core.py                  # 36 个单元测试
QT_QPA_PLATFORM=offscreen python tests/smoke_gui.py
```

`tests/test_core.py` 覆盖：

- 分块边界（正好 1 块 / 超 1 字节 / 正好 2 块）、整文件哈希与单独哈希一致
- 三种压缩标签的往返、压缩无益时原样存放
- chunk 去重、跨游戏共享、`verify()` 发现篡改、`gc()` 只清理无引用 chunk
- `_decide` 三方合并真值表逐格
- 快照 → 修改 → 回滚（含 `only_paths` 单文件回滚、`safety=False`）
- 覆盖前留底、留底内容正确
- 首次全量上传、第二次零上传、改 10 字节只重传受影响 chunk
- 云端领先时下载并覆盖本地
- 删除双向传播（本地删→云端删；云端删→本地删且留底）
- 冲突三选一，未裁决则阻断且不推进 base
- 多目录虚拟根往返、陈旧序号前缀跳过
- 无变化的同步不生成新快照
- 快照保留策略：只留最近 N 份、保护同步基准、回收失去引用的 chunk

`tests/smoke_gui.py` 在离屏模式下驱动真实 `MainWindow`，
重点盯两个曾经真实发生过的故障：后台线程读 SQLite 抛异常、
后台失败弹模态框挂死界面。

---

## 9. 已验证的运行数据

在本机（Windows）用 CLI 实测：

```
$ python run.py --cli sync          # 首次
CLI测试        上传 2 · 下载 0 · 冲突 0   上传 4 / 下载 0 chunk

$ python run.py --cli sync          # 无变化
CLI测试        上传 0 · 下载 0 · 冲突 0   上传 0 / 下载 0 chunk

$ python run.py --cli sync          # 改动 a.sav 后
CLI测试        上传 1 · 下载 0 · 冲突 0   上传 1 / 下载 0 chunk
```

第三次只传了 1 个 chunk —— 2.5 MB 的存档里改了一个 100 KB 的文件，
`sub/big.bin` 的 3 个 chunk 因内容未变全部命中去重，零重传。

把 `keep_snapshots` 调到 2 后连续同步 4 次：

```
已裁剪旧快照：本地 4 / 云端 4（保留最近 2 版）
本地快照 2 份 / 云端快照 2 份 / 共 5 个 chunk
```

5 个 chunk = `big.bin` 的 3 个（跨版本共享）+ 2 个不同版本的 `a.sav`，
去重与保留策略同时生效。

本地模式（未配置云端）下的同一批命令：

```
$ python run.py --cli backup
（未配置云端，当前为本地备份模式）
CLI测试        1 文件 14 B

$ python run.py --cli status
（未配置云端，当前为本地备份模式）
CLI测试    已备份   本地 14 B   最新备份 14 B   快照 1   已备份到本地保险库

$ python run.py --cli sync          # 未配云端时自动退化为本地备份
（未配置云端，当前为本地备份模式）
未配置云端，转为本地备份：
CLI测试        1 文件 14 B
```

联网取图实测（`艾尔登法环`）：Steam `storesearch` 返回 appid `1245620`，
取到 `logo.png` 并规整为 96×96 圆角方块，落盘 `logocache/`；
不存在的名字进入 `_failed` 不再重试。

---

## 10. 本地备份为主，云备份为可选项

这是本项目的默认形态，也是与「一上来就要求配云端」的同类产品最大的区别。

### 10.1 两种模式

| | 本地备份模式（默认） | 云端同步模式（可选） |
|---|---|---|
| 开关 | `config.json` 的 `remote.enabled = false` | 设置里勾选「启用云端同步」并填位置 |
| 后端对象 | **不创建**（`MainWindow.remote is None`） | LocalFolderBackend / WebDAVBackend |
| 快照存到 | 本地保险库 `vault/` | 本地保险库 + 云端 |
| 「同步全部」按钮 | 「备份全部」—— 逐款存快照 | 「同步全部」—— 三方合并 + 上传下载 |
| 状态语义 | 本地目录 vs 本地最新快照 | 本地 vs 基准 vs 云端 |
| 状态文案 | 已备份 / 待备份 / 未备份 | 已同步 / 待上传 / 待下载 |
| 云端提醒 | 无 | 左侧模式标签显示「云端待同步 N 款」 |

状态机在两种模式下**复用同一套常量**（`SYNC_SYNCED` / `SYNC_LOCAL_AHEAD` /
`SYNC_UNKNOWN` …），只是 `theme.state_label(state, cloud_on)` 换文案、
`SyncEngine.status()` 换对比对象。不为本地模式单开一套状态常量，
避免两套真值表各自漂移。

### 10.2 为什么默认不碰云端

`Config.cloud_enabled` 要求「总开关打开 **且** 对应后端填了地址/目录」。
只勾开关没填 WebDAV 地址时按本地模式跑 —— 否则每次同步都会去连一个空地址，
然后把每款游戏都标成红色错误态，用户根本不知道发生了什么。

代码上的保证：`SyncEngine` 持有 `cloud_on` 标志，
`status()` / `plan()` 在该标志为 False 时**不调用任何远端原语**
（单元测试用一个「一被调用就抛 AssertionError」的后端来守住这条边界）。

### 10.3 游戏图标：三级保障

左侧列表每款游戏都有一个图标，来源按优先级：

```
1. 磁盘缓存     ~/.gamesavecloud（或 %LOCALAPPDATA%）/logocache/<md5>.png
                —— 第二次启动直接用，离线也有图
2. 内置预设     logos.py 的 BUILTIN 表，约 90 款热门游戏
                （配色 + 短代号，离线立刻可画，断网也不会空白）
3. 联网检索     Steam 公开接口，见下
4. 占位兜底     首字母色块，与真实图标同尺寸同圆角
```

**联网那一层的来源与校验：**

```
① https://store.steampowered.com/api/storesearch/?term=<游戏名>&cc=cn&l=schinese
   免申请 key，返回 appid
② https://cdn.akamai.steamstatic.com/steam/apps/<appid>/logo.png   ← 透明底，首选
③ https://cdn.akamai.steamstatic.com/steam/apps/<appid>/header.jpg ← 兜底
```

选 Steam 是因为覆盖面最广、免密钥、有公开 CDN。风险是**名称匹配可能对不上**，
所以加了一道 `difflib.SequenceMatcher` 相似度校验（相等 / 互相包含 / 相似度 ≥ 0.62）。
判「不像」就当作没找到，回落到预设或占位 —— **错的 logo 比没有 logo 更误导人**。

三个容易踩的坑，代码里都有对应处理：

- **`QPixmap` / `QPainter` 只能在 GUI 线程用。** 工作线程只做网络 IO 和
  `QImage` 解码，规整成圆角方块这一步放回 `_on_fetched`（主线程）做。
- **`hash(str)` 按进程随机化。** 占位色块的颜色索引用 `zlib.crc32`，
  保证同一款游戏每次启动都是同一个颜色。
- **`QThreadPool.start()` 不持 Python 侧引用。** worker 若被 GC，
  `logo_ready` 永远发不出来。`LogoService` 自己维护了一份强引用表。

### 10.4 左侧列表排版

原来的实现是「每个条目塞一个 QWidget + 上下两个 QLabel」：
N 个条目就是 N 套 layout，窗口缩放时全部重新布局，
而且 QWidget 拿不到原生的 hover / selected 状态。

现在改成 `QStyledItemDelegate` 自绘（`ui/game_list.py`）：

- **固定行高 64px**，写死，不随内容伸缩 —— 这是「不挤在一起」的关键
- **图标 + 文本严格两栏**，间距是常量 `GAP = 12`
- **文字过长省略号截断**（`QFontMetrics.elidedText`），
  **悬浮显示全称**（`helpEvent` 里给出完整名称、存档目录、状态、体积）
- **三态底色**：普通 / 悬停（`#f0f1f4`）/ 选中（浅蓝 + 左侧 3px 强调条）
- 图标到手后只 `update()` 受影响的几行，不整表刷新
  （整表刷新会重置选中位置和滚动位置）

---

## 11. 已知限制

- WebDAV 后端实现了 MKCOL / PUT / GET / PROPFIND / HEAD / Range 续传，
  但只在标准库 `urllib` 上实现，未做连接池与重试退避；弱网环境下大文件
  仍可能失败（失败不推进 base，重试即可，不会丢数据）。
- 定时同步依赖应用常驻（托盘）。完全无人值守可用打包好的
  `GameSaveCloud-CLI.exe sync` 配操作系统的计划任务
  （退出码 2 表示有冲突待处理）。
- 加密同步（端到端）未实现。云端目录是明文 chunk，
  放在 NAS / 移动硬盘上请自行做磁盘级加密。
- exe 未做代码签名，首次运行可能被 SmartScreen 拦一下，
  点「更多信息 → 仍要运行」即可。
