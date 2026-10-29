# GameSaveCloud

跨平台桌面端的游戏存档备份与同步工具，对标 UU 加速器「云存档」。
核心承诺：**任何情况下不丢存档** —— 覆盖、回滚、冲突、删除，每一步之前都先留底。

约 6200 行，PySide6 界面 + 标准库后端，零强制第三方依赖（`zstandard` 仅为可选加速）。

**当前版本 v1.3.1** —— 下载与更新日志见 [Releases](https://github.com/lanjiechong/GameSaveCloud/releases) 与 [CHANGELOG.md](CHANGELOG.md)。

**许可证：[MIT](LICENSE)** —— 可自由使用、修改与再分发；第三方组件与游戏商标声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

**本地备份为主，云备份为可选项。** 开箱即用，不配任何云端也能完整工作：
备份、历史版本、回滚、冲突检测全部在本地完成。只有在设置里勾选「启用云端同步」
并填好位置后，才会把存档同步到本地目录 / 移动硬盘 / NAS / WebDAV。

---

## 1. 快速开始

### 方式一：直接用打包好的 exe（推荐，无需装任何东西）

到 [Releases 页面](https://github.com/lanjiechong/GameSaveCloud/releases) 下载最新版：

```
GameSaveCloud.exe        图形界面版（无控制台窗口，双击即用）
GameSaveCloud-CLI.exe    命令行版，供计划任务 / 脚本调用
```

两个都是**单文件**，拷到任何一台 Windows 机器上都能直接跑。
数据存放在 `%LOCALAPPDATA%\GameSaveCloud\`，换机器用只需把 exe 拷过去。

**校验下载物**（建议）：Release 页面会附上每个 exe 的 SHA256，下载后本地核对：

```powershell
Get-FileHash .\GameSaveCloud.exe -Algorithm SHA256
```

exe 当前未做代码签名（个人开发者暂无证书），首次运行若被 SmartScreen 拦截，
点「更多信息 → 仍要运行」即可；哈希一致就说明文件未被篡改。

也可以从源码自行构建（产物在 `dist/`）：

```bash
pyinstaller GameSaveCloud.spec --noconfirm
```

```cmd
GameSaveCloud-CLI.exe status    :: 各游戏同步状态
GameSaveCloud-CLI.exe scan      :: 扫描本机存档目录
GameSaveCloud-CLI.exe backup    :: 立即备份全部
GameSaveCloud-CLI.exe sync      :: 同步全部（未配云端时等同 backup）
GameSaveCloud-CLI.exe --version :: 显示版本号
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
python tests/test_core.py                                # 123 个核心引擎单元测试
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
两个 exe 各 50 MB 左右。排除列表是保守的：QtWidgets 运行时会拉
QtGui / QtCore / QtNetwork / QtOpenGL / QtPrintSupport，这些都不排。
spec 的 `datas` **只打包应用自己的图标**（app_logo.ico/png），
不打包 `ui/assets/logos/` —— 第三方游戏 logo 只在运行时从 Steam CDN
获取，避免商标素材随安装包再分发（见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）。


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
| 交互 | **图标与中文名自动匹配** | `gamesave/gamemeta.py`（Steam 搜索 + 中文名解析 + 退避），`ui/metaservice.py`（后台批量调度），落库见 `vault.py::save_meta`（见 §10.3） |
| 数据 | **离线资料索引 + 多数据源调研** | `gamesave/gameindex.py` + `ui/assets/gameindex.json`（随包分发），构建工具 `scripts/build_game_index.py`（见 §10.5） |
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
│  logos.py                 图标：落盘图 / 缓存 / 离线包 / 预设    │
│  metaservice.py           后台批量匹配图标与中文名（3 并发）     │
│  workers.py               QRunnable + QThreadPool，信号回传     │
│  theme.py                配色与样式表                          │
│  assets/                  仅应用自己的图标（PNG + 多尺寸 ICO）   │
│  assets/logos/            第三方 logo 不入库（gitignore，       │
│                           运行时从 Steam CDN 拉取并缓存）       │
├───────────────────────────────────────────────────────────────┤
│  scripts/                 维护工具（非运行时代码）               │
│  fetch_top_logos.py       本机可选预取离线 logo 包（不分发）     │
│  build_game_index.py      构建随包分发的游戏资料索引            │
│  check_licenses.py        发布前依赖许可证自查                  │
├───────────────────────────────────────────────────────────────┤
│  version.py               版本号（发版只改这里，配合 git tag）   │
│  run.py / run_cli.py      入口：GUI / 命令行                    │
│  CHANGELOG.md             版本更新记录                          │
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
│  gamemeta.py                Steam 名字解析 / 中文名 / 退避策略   │
│  gameindex.py              离线资料索引（零网络认出榜单游戏）    │
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

chunk 层面共享了，但**快照清单**不会：连着点几次「立即备份」、定时备份每小时
跑一轮而存档根本没变，历史列表就会堆着一排时间不同、内容完全相同的条目。
所以每次刷新会先做一次 `merge_duplicate_snapshots`：按内容指纹
（路径 + 哈希 + 大小 + 分块序列，不含 mtime）分组，保留组内最新的一份，
其余删除并 gc。同步基准 `base_snapshot_id` 指向的快照受保护不删——
它是三方合并的参照物。测试见 `TestMergeDuplicates`。

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
| `game_meta` | 图标与中文名（name_zh / appid / logo_file / state / attempts / next_retry_at） |

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

### 重复备份自动合并

`Vault.merge_duplicate_snapshots(game_id)` 在每次**刷新**时先跑：
按内容指纹（路径 + 哈希 + 大小 + 分块序列，不含 mtime）把快照分组，
保留组内最新的一份，其余删除并 gc。手动连点备份、定时备份遇上
存档没变，都不再堆出一排内容相同的历史条目。同步基准同样受保护
（测试 `TestMergeDuplicates`，6 个用例）。

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
python tests/test_core.py                  # 123 个单元测试
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
- 重复快照合并：内容指纹相同即合并、同步基准受保护
- 游戏元数据：Steam 搜索四分支（命中中文名 / 只有英文名 / 没搜到 / 网络错误）、
  DLC 过滤、名字不像的候选被拒、中文名挑选
- `game_meta` 的 patch 语义（只补图标不抹掉已存的中文名）、重启后仍能读到、
  移除游戏时清理
- 匹配调度：已完整的不再排队、失败按指数退避、退避到期自动重试

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

### 10.3 图标与中文名：自动匹配 + 逐级保障

左侧每款游戏都有图标和显示名，两者由同一套匹配流程补齐。

**匹配时机**：扫描/添加游戏之后、每次点「刷新」之后。只处理
「缺图标 **或** 缺中文名」的游戏 —— 已经齐了的一个网络请求都不发。
匹配跑在独立线程池（3 并发），刷新先把列表渲染出来，结果逐个回填。

**图标来源（按优先级）**：

```
1. 匹配落盘     %LOCALAPPDATA%/GameSaveCloud/gameicons/<game_id>.png
                —— 匹配成功后按 game_id 存，改名、换语言都不丢
2. 磁盘缓存     %LOCALAPPDATA%/GameSaveCloud/logocache/<md5>.png
                —— 早期按名字哈希的缓存，兼容旧数据
3. 可选离线包   %LOCALAPPDATA%/GameSaveCloud/logopack/<appid>.png
                —— 用户自行安装的离线 logo 包，默认不存在
4. 内置预设     logos.py 的 BUILTIN 表，约 90 款热门游戏
                （配色 + 短代号，离线立刻可画，断网也不会空白）
5. 占位兜底     首字母色块，与真实图标同尺寸同圆角
```

**中文名来源（按优先级）**：

```
1. Steam 国区商店名   storesearch?l=schinese 返回的本地化名
2. 离线包社区译名     logo_pack.json 中该 appid 的 names 里挑含汉字的
3. 原名              以上都没有就继续显示扫描时的名字，界面不受影响
```

匹配到的中文名进 `game_meta.name_zh`，列表主标题优先显示它，
原名挂在 tooltip 上（对照存档目录时不会认错）。

**商标合规**：游戏 logo 的版权归各游戏厂商所有，所以仓库和发布包
**不再内置任何第三方 logo** —— 真实图标一律在运行时从 Steam 公共 CDN
获取并缓存在用户本机（指示性使用，仅用于标识对应游戏）。
`scripts/fetch_top_logos.py` 仍保留，但只是开发者/用户在**本机**预取
离线包的工具，产物目录 `ui/assets/logos/` 已被 `.gitignore` 排除，
spec 也不会把它打进 exe。想要离线即有真实图标，可把预取产物放到
`%LOCALAPPDATA%\GameSaveCloud\logopack\`（或开发时直接生成到
`ui/assets/logos/`）。

**匹配流程**（`gamesave/gamemeta.py` + `ui/metaservice.py`）：

```
刷新完成
  └→ MetaService.ensure(games)           只挑缺图标 / 缺中文名的
       └→ 独立线程池（3 并发）
            ├→ gamemeta.lookup(name)       拆词搜 Steam，做名字校验
            ├→ 下载 logo.png / header.jpg，解码成 QImage
            └→ 回主线程：QImage → 96px 圆角方块 → 存 PNG → 写 game_meta
                 └→ meta_ready(game) → 就地重绘那一行 + 更新右侧卡片
```

**匹配失败怎么办**：

- 只写 `state` / `attempts` / `next_retry_at`，**不动游戏本身的任何字段** ——
  列表照常显示原名与占位图标，不影响任何操作。
- 失败退避：10 分钟 → 20 分钟 → 40 分钟 …… 封顶 7 天。避免每次刷新都往
  Steam 打一遍注定失败的请求；退避到期后刷新会自动重试。
- 「查到了但官方没中文名」（大量独立游戏国区只有英文名）记为 `ok`，
  不算失败 —— 否则这类游戏会**永远**被判成「缺中文名」而无限重试。

**目录名不等于游戏名**（这条最容易被忽略）：

LocalLow 是 `LocalLow\<工作室>\<游戏>` 两级结构。扫描器早期只取到工作室
那一层，于是「AIHASTO」「House House」「Team Salvato」被登记成游戏 ——
这些在 Steam 上对应的是**发行商**，搜一条都搜不到，实际游戏是
MiSide / Big Walk / DDLC Plus。现在两层都兜：扫描器下钻一层取真游戏目录，
匹配层还会从存档路径里的子目录名推断游戏名（`path_hints`），
把已经存在的旧记录也救回来。

**为什么必须拆词搜**（实测踩出来的坑）：

扫描器给游戏起的是「中文俗名 英文名」组合名（`荒野大镖客2 Red Dead Redemption 2`），
而 Steam 搜索只认官方名 —— 整个组合名丢进 `storesearch` **一个结果都返回不了**
（实测：`term=荒野大镖客2 Red Dead Redemption 2` → 空；
单独搜 `Red Dead Redemption 2` → 1174180）。所以检索前会拆出英文部分
优先搜（`gamemeta.search_terms`）；热门游戏第一次联网取到图后即落盘，
离线包则为完全断网的机器提供可选补充。

**两字中文标题曾经匹配不上**：「只狼」归一化后只有 2 个字符，被
`len >= 3` 的包含判定挡在门外，导致 `只狼 Sekiro` 永远搜不到
`只狼：影逝二度`。现在规则是「查询词是候选子串时，含汉字的两字词也算命中」——
一个两字中文词的信息量远大于两个拉丁字母，不该用同一把尺子。

**离线包的名字匹配**（`_pack_lookup`，四轮从严到宽，天然兼容组合名）：

```
1. 整个名字精确命中            「GTA V」→ 271590
2. 整个名字包含某个包内名       「荒野大镖客2 Red Dead Redemption 2」包含
                               "reddeadredemption2" → 1174180（取最长命中）
3. 按空格切 token 精确命中      「巫师3 The Witcher 3」→ token「巫师3」命中
4. token 被某个包内名包含       「The Witcher 3」的 token「Witcher」不够长，
                               但「thewitcher3」作为包内名被第 2 轮收掉
```

**跨语言怎么认亲**（这条踩得最深）：

国区的 `storesearch` 直接返回本地化名，「查英文词、拿中文名」是常态：
`Cyberpunk 2077` 返回 `赛博朋克 2077`。拿英文查询词和中文候选算字符串
相似度必然是 0 —— 正确条目被排除，反而选中名字里恰好带查询词的 DLC
（实测命中 `2060310 Cyberpunk 2077 REDmod`，不是本体 `1091500`；
搜 `SplitFiction` 明明返回了「双影奇境」却一条都匹配不上）。

现在的顺序是：

1. 先看搜索结果**第一条**（Steam 的相关度排序基本可靠），名字能对上就用；
2. 对不上且「查询是英文、候选是中文」时，问 `appdetails?l=english` 要官方
   英文名再比一次（阈值 0.8；只对第一条发，不逐个候选打，避免触发限流）；
3. 都不行才走宽松的名字匹配兜底。

另两条配套规则：英文名靠分隔符接副标题（`Sekiro™: Shadows Die Twice`）
算正常命名；靠空格硬接一截（`Cyberpunk 2077 REDmod`）判为不同产品。
常见缩写（`GTA V` / `RDR2`）先展开成全名再搜，否则永远搜不到。

**联网来源与校验：**

```
① https://store.steampowered.com/api/storesearch/?term=<游戏名>&cc=cn&l=schinese
   免申请 key，返回 appid
② https://cdn.akamai.steamstatic.com/steam/apps/<appid>/logo.png   ← 透明底，首选
③ https://cdn.akamai.steamstatic.com/steam/apps/<appid>/header.jpg ← 兜底
```

选 Steam 是因为覆盖面最广、免密钥、有公开 CDN。风险是**名称匹配可能对不上**，
所以加了一道 `difflib.SequenceMatcher` 相似度校验（相等 / 互相包含 / 相似度 ≥ 0.62）。
判「不像」就当作没找到，回落到预设或占位 —— **错的 logo 比没有 logo 更误导人**。

另外，检索前会先把组合名拆出英文部分优先搜（`gamemeta.search_terms`）：
`荒野大镖客2 Red Dead Redemption 2` → 先搜 `Red Dead Redemption 2`。

三个容易踩的坑，代码里都有对应处理：

- **`QPixmap` / `QPainter` 只能在 GUI 线程用。** 工作线程只做网络 IO 和
  `QImage` 解码；规整成圆角方块、写 PNG 都在主线程的
  `MetaService._on_result` 里做。
- **`hash(str)` 按进程随机化。** 占位色块的颜色索引用 `zlib.crc32`，
  保证同一款游戏每次启动都是同一个颜色。
- **`QThreadPool.start()` 不持 Python 侧引用。** worker 若被 GC，
  `result` 信号永远发不出来，界面会一直卡在「匹配中 n/N」。
  `MetaService` 自己维护了一份强引用表。

**本机预取离线 logo 包**（可选，需要能访问 Steam 商店；产物不提交、不分发）：

```powershell
# 抓当前周销量榜前 100（store.steampowered.com 搜索页，国内需代理）
# 把 appid 每行一个存成 top_ids.txt，然后：
python scripts/fetch_top_logos.py --ids top_ids.txt
# 默认输出到开发目录 ui/assets/logos/（gitignore，仅本机调试用）；
# 想让日常使用的程序也加载，输出到用户包目录：
python scripts/fetch_top_logos.py --ids top_ids.txt --out "$env:LOCALAPPDATA\GameSaveCloud\logopack"
```

脚本只依赖 Pillow（构建期），运行时不依赖；已有图标不会重复下载。

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

### 10.5 数据源与离线索引

**主源只有 Steam，这是实测出来的结论。** 2026-10-30 在本机逐个探测候选源：

| 来源 | 覆盖 | 可靠性 | 本机可达 | 结论 |
|---|---|---|---|---|
| Steam Store API | Steam 全量十余万款 | 官方、免密钥、有公开 CDN | ✅ 200 | **主源** |
| Bangumi | 中文 ACG / 独立游戏强，3A 一般 | 社区维护、免密钥、结构简单 | ❌ 连接超时 | 已接入，默认关 |
| Wikidata | 全球游戏（含主机独占） | 官方库、免密钥 | ❌ 连接超时 | 不接：要「搜 QID → 取 zh label」两步 |
| Wikipedia | 热门游戏 | 高 | ❌ 连接超时 | 不接：没有结构化图标 |
| RAWG | 80 万+ 跨平台 | 中高 | ❌ 连接超时 | 不接：需 API key，免费 2 万次/月 |
| SteamSpy | Steam 全量（销量估算） | 第三方估算、有延迟 | ⚠️ 403 | 不接：Cloudflare 挑战页 |

代理通道同样不通（所有外站 2 秒内失败）。所以「多接几个源」在当前网络下
只会给每次刷新多几个超时 —— 真正的可靠性来自**离线索引**，不是更多联网源。

**离线索引**（`ui/assets/gameindex.json`，约 50 KB，随包分发）：

```
Steam 畅销榜（中英双语各抓 2 页 ≈ 200 个候选）
  └→ 逐个取 appdetails 详情（名称 / 发行商 / 类型 / 评价数 / 图标地址）
       └→ 剔除评价数 < 1 的新游（刚上架、评价为 0，不属于「经典」）
            └→ 按累计评价数降序取前 100，原子写入 JSON
```

它是**元数据**（没有图片二进制），所以能随仓库和发布包分发 ——
商标素材仍然只在运行时从 CDN 取，合规边界不变。

匹配顺序因此变成三层：

```
1. 本地索引   命中即返回，零网络请求（榜单内游戏必定认得出）
2. Steam      索引没有的，联网搜索 + 英文名复核
3. Bangumi    仅当 Steam 查到但没给中文名、且配置开着时才用
```

评价数取 `recommendations.total` —— 最接近「历史销量」的公开数字。
长期霸榜的老游戏会浮到前面、新游自然沉底，符合「经典畅销」的语义。

重建索引（需能访问 Steam，约 3 分钟）：

```bash
python scripts/build_game_index.py            # 断点续传，只补缺的
python scripts/build_game_index.py --refresh  # 全部重抓
```

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
  点「更多信息 → 仍要运行」即可；下载后建议用 Release 页面的
  SHA256 核对文件完整性。
- 图标与中文名匹配依赖 Steam 公开接口。离线时只有内置预设/占位图标，
  中文名保持原名；联网后下次刷新会自动补上。名字对不上时宁可判
  「没找到」，绝不显示张冠李戴的图标或译名。

---

## 12. 许可证与声明

**本项目代码：[MIT License](LICENSE)**，Copyright (c) 2026 lanjiechong。
可自由使用、修改、再分发（包括商业用途），唯须保留版权与许可声明。

**第三方组件**（PySide6/Qt 为 LGPL-3.0、zstandard 为 BSD-3-Clause、
Python 运行时为 PSF）的完整声明、动态链接合规说明与 Qt 可替换要约，
见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
发布前可用 `python scripts/check_licenses.py` 自查依赖许可证清单。

**游戏名称与图标**：各游戏名称、logo、商标的版权归其各自所有者所有，
与本项目不存在授权、赞助或关联关系；程序仅在运行时从 Steam 公开接口
获取图标用于标识对应游戏，不再分发任何第三方素材。

**关于 AI 辅助开发的说明**：本项目在开发过程中使用了 AI 编程助手
参与代码编写与问题排查。需求取舍、架构设计、安全模型（留底机制、
三方合并基准、内容哈希判定等）与最终代码均由人类作者审阅、测试并负责；
全部核心行为由 `tests/test_core.py` 的 123 个单元测试钉住。如发现任何
疑似侵权内容，请在 [Issues](https://github.com/lanjiechong/GameSaveCloud/issues)
反馈，核实后立即处理。
