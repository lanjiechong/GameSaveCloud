# 更新日志

本项目的版本变更记录。格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.3.1] - 2026-10-30

### 修复

- **工作室名被当成游戏名**（根因）：扫描器对 LocalLow（Unity 的
  `<工作室>\<游戏>` 布局）只取到第一层，于是「AIHASTO」「House House」
  「Team Salvato」这类**发行商名**被登记成游戏，Steam 上当然一条都搜不到。
  现在会下钻一层取真正的游戏目录（MiSideFull / Big Walk / DDLC Plus）。
  已经存在的记录不重建，靠匹配层从存档路径推断游戏名救回来。
- **相似度只看「共享一个词」**：`Supermarket Together` 与
  `Don't Starve Together` 共享 Together 就判成同一款，`Ogu Forest` 与
  `The Forest` 同理，导致图标张冠李戴。现在要求首字符相同、
  长度比 ≥ 0.6、相似度 ≥ 0.75。
- **连写目录名搜不到**：`SplitFiction`、`TheFarmerWasReplaced` 这类
  去空格的驼峰名原样搜必然落空，现在先拆词再搜；`MiSideFull` 这类
  带冗余后缀的先去掉 Full / Plus / Game 再搜。
- **软件目录污染候选词**：`Roaming` 下的 `Code Cache` / `Crashpad`
  等缓存目录名被当成游戏名去搜，曾把加速器 ACLOS 认成 R.E.P.O.。
  子目录线索现在只对 LocalLow 启用。
- **网络抖动被记成失败**：Steam 偶发丢连接时一次失败就判 error，
  用户看到「失败 N 款」这种假失败。取数增加一次重试。
- 移除 Rockstar `Social Club` 目录的误报条目（那是账号配置，不是存档）。

## [1.3.0] - 2026-10-30

### 新增

- **离线游戏资料索引**：内置 Steam 畅销榜 Top100 的中英文名 / 发行商 /
  类型 / 累计评价数 / 图标地址（`ui/assets/gameindex.json`）。
  匹配时**索引优先**，命中即返回、零网络请求 —— 断网、被限流、商店改版
  都不影响列表显示；它同时是「Steam 搜不到这款游戏」时的兜底。
  索引是元数据，不含任何图片二进制，商标素材仍只在运行时从 CDN 取。
- **中文名补充源 Bangumi**（`gamemeta.bangumi_name_zh`）：Steam 查到游戏
  但只给英文名时补一个中文名。默认关闭，`metadata.use_bangumi` 开启 ——
  该接口在当前网络下不可达，常开会给每次刷新白加一个超时。
- `scripts/build_game_index.py`：索引构建工具。中英双语榜单各抓两页取
  候选（周榜混着零评价新游，一页过滤完凑不满 100），再逐个取详情、
  按累计评价数降序取前 100。支持断点续传。

### 变更

- `gamemeta.lookup` 新增 `local_index` / `use_bangumi` 参数，
  返回体加 `source` 字段（`index` / `steam` / `steam+bangumi`）。
- `MetaService` 默认接本地索引，索引命中时一个网络请求都不发。
- `config.metadata` 新增配置段（目前只有 `use_bangumi` 一个开关）。

### 数据源调研结论（2026-10-30 本机实测）

| 来源 | 覆盖 | 可靠性 | 本机可达 | 结论 |
|---|---|---|---|---|
| Steam Store API | Steam 全量十余万款 | 官方、免密钥 | ✅ 200 | 主源 |
| Bangumi | 中文 ACG/独立强，3A 一般 | 社区维护，免密钥 | ❌ 超时 | 接入但默认关 |
| Wikidata | 全球游戏（含主机独占） | 官方库，免密钥 | ❌ 超时 | 不接：需两步请求取 zh label |
| Wikipedia | 热门游戏 | 高 | ❌ 超时 | 不接：无结构化图标 |
| RAWG | 80 万+ 跨平台 | 中高，需 API key | ❌ 超时 | 不接：要 key + 免费额度限制 |
| SteamSpy | Steam 全量（销量估算） | 第三方估算，有延迟 | ⚠️ 403 | 不接：Cloudflare 挑战页 |

结论：能稳定连上的只有 Steam 官方接口，因此把「离线索引」作为第二层
可靠性保障，而不是再堆一个同样会超时的联网源。

## [1.2.0] - 2026-10-30

### 新增

- **图标与中文名自动匹配**：扫描或刷新后，自动为每款游戏补齐图标与
  官方中文名。列表优先显示中文名，原名挂 tooltip，对照存档目录不丢线索。
- **增量匹配**：只处理「缺图标或缺中文名」的游戏，已有的一个网络请求
  都不发。匹配成功后图标按 `game_id` 落盘、中文名写进 `game_meta` 表，
  下次启动直接读本地数据，不再重复联网。
- **失败不影响列表**：匹配失败只记状态、保留原名与占位图标，界面照常
  可用；失败次数按 10 分钟 → 翻倍 → 封顶 7 天退避，后续刷新自动重试。
- **不阻塞刷新**：匹配跑在独立线程池（3 并发），刷新先把列表渲染出来，
  结果逐个回填并就地重绘。左侧标题栏显示「匹配中 n/N」，完成后写日志汇总。
- 中文名来源三层：Steam 国区商店名（`l=schinese`）→ 可选离线 logo 包里的
  社区译名 → 原名。Steam 查到但官方无中文名时记为 `ok`，
  不会因为「永远缺中文名」反复重试。

### 变更

- `gamesave/gamemeta.py`（新）：Steam 搜索、名字校验、中文名挑选、退避策略。
  纯 stdlib，不依赖 PySide6，可用假 fetch 完整单测。
- `ui/metaservice.py`（新）：批量匹配调度。工作线程只做网络 + 解码，
  QPixmap 转换与落盘留在主线程。
- `ui/logos.py`：删掉原先「按名字异步联网取图」的整条链路（`request` /
  `logo_ready`），联网职责统一收敛到 `MetaService`；本模块改为纯本地、
  同步返回，新增按 `game_id` 取图与落盘。
- 名字归一化 / 相似度判定从 `ui/logos.py` 下沉到 `gamesave/gamemeta.py`，
  两处不再各写一份。
- `game_meta` 表（`index.db`）新增，`Game` 增加 `name_zh` / `logo_file` /
  `meta_state` 与 `display_name` 属性；`games` 表结构未变。

### 修复

- **跨语言误匹配**：国区搜索结果返回中文名（「赛博朋克 2077」），
  与英文查询词做字符串相似度必然是 0，导致正确条目被排除、反而选中
  名字里恰好带查询词的 DLC / 工具。实测 `Cyberpunk 2077` 命中
  `2060310 REDmod` 而不是本体 `1091500`；`SplitFiction` 明明返回了
  「双影奇境」却一条都匹配不上。改为先判定搜索结果第一条、
  必要时用 `appdetails` 的英文名复核。
- **副标题被当成另一款游戏**：`Sekiro` 匹配 `Sekiro™: Shadows Die
  Twice - GOTY Edition` 时因长度差被判「不像」。现在区分「靠分隔符
  接副标题」（正常命名）与「靠空格硬接一截」（多半是别的产品）。
- 常见缩写（`GTA V` / `RDR2`）先展开成全名再搜，否则永远搜不到。
- 两字中文标题（「只狼」vs「只狼：影逝二度」）原先被 `len>=3` 的包含
  判定挡住，永远匹配不上。改为「查询词是候选子串时，含汉字的
  两字词也算命中」。

## [1.1.0] - 2026-10-15

### 新增

- **重复备份自动合并**：刷新时自动识别并合并内容完全相同的快照。
  连着点几次「立即备份」、定时备份每小时跑一轮而存档没变时，历史列表
  不再堆着一排时间不同、内容相同的条目。判定看内容（路径 + 哈希 +
  大小 + 分块序列），mtime 抖动不影响；保留组内最新的一份，
  同步基准（base_snapshot_id）指向的快照受保护不删，删除后自动 gc
  回收失去引用的 chunk。合并数量在日志里报告。

## [1.0.0] - 2026-10-15

首个正式版本。本机游戏存档的备份、历史版本、回滚与多设备同步工具。

### 备份引擎

- 自动扫描本机存档目录（Steam / WeGame / Epic / 常见文档位置），
  去重后交由人工确认添加，不碰任何未确认的路径
- 分块存储（chunker）：默认 1 MiB 块 + zstd 压缩，
  按内容寻址去重——同一文件跨版本共享未变化的块
- 快照机制：每次备份生成一个快照，保留文件清单、大小与校验和
- 断点续传：chunk 级上传/下载，中断后重试只传缺失部分

### 历史与回滚

- 历史版本列表，支持查看内容、逐文件 diff 对比
- 一键回滚到任意历史快照；回滚前自动把当前版本另存备份
- 自动裁剪：默认保留最近 30 个快照，可配置

### 云端同步（可选）

- 默认纯本地备份模式：未配置云端时不创建任何远端目录、不发任何网络请求
- 云端支持本地文件夹与 WebDAV 两种后端
- 冲突检测：本地与云端都改了同一文件时绝不静默二选一，
  弹窗交人决策——保留本地 / 使用云端 / 两边各存一份

### 界面与调度

- PySide6 图形界面：游戏列表（图标 + 状态）、同步状态卡片、历史表、操作日志
- 定时自动备份、托盘常驻、可选开机自启
- 危险操作（回滚、覆盖、删除游戏）一律二次确认，默认选项为取消

### 游戏图标（逐级保障，商标合规）

- **不内置、不再分发任何第三方游戏 logo**：真实图标运行时从
  Steam 公开 CDN 获取并缓存到本机（指示性使用，标识对应游戏）
- 名称匹配兼容扫描器的「中文俗名 英文名」组合名
- 依次走：磁盘缓存 → 可选离线 logo 包（用户自行安装）→
  内置配色预设 → Steam 联网检索 → 首字母占位色块
- `scripts/fetch_top_logos.py` 供开发者/用户本机预取离线包，
  产物 gitignore、不进发布包

### 安全加固

- WebDAV 密码以 Windows DPAPI 加密存入 config.json（旧明文自动迁移）；
  使用 http:// 明文地址时弹二次确认
- 快照清单内路径强制安全校验（`..` 越界 / 盘符绝对路径一律拒绝），
  防止被篡改的云端快照写出存档目录
- 下载的 chunk 落盘后立即重算哈希校验，损坏块当场丢弃，不进入快照
- 快照清单 JSON 改为原子写（.tmp + replace）
- 状态刷新走 size+mtime 快速路径；同步决策仍以内容哈希为唯一依据

### 许可证

- 本项目代码以 MIT 许可证发布，新增 LICENSE 与 THIRD_PARTY_NOTICES.md
  （PySide6 LGPL-3.0 动态链接声明与可重链接要约、zstandard BSD、
  Python PSF、游戏商标归各自所有者）
- 新增 `scripts/check_licenses.py` 发布前依赖许可证自查

### 质量保障

- 60 个单元测试覆盖引擎层（chunker / vault / sync / 冲突策略 /
  路径安全 / 凭据加密 / 快速扫描 / 原子写）
- GUI 冒烟测试（离屏运行）：后台线程 SQLite 访问、8 线程并发读、
  对话框构造、本地/云端两条同步链路、无 logo 包时的图标兜底
- 危险操作边界由「一被调用就抛 AssertionError」的假后端测试守住

[1.3.1]: https://github.com/lanjiechong/GameSaveCloud/releases/tag/v1.3.1
[1.3.0]: https://github.com/lanjiechong/GameSaveCloud/releases/tag/v1.3.0
[1.2.0]: https://github.com/lanjiechong/GameSaveCloud/releases/tag/v1.2.0
[1.1.0]: https://github.com/lanjiechong/GameSaveCloud/releases/tag/v1.1.0
[1.0.0]: https://github.com/lanjiechong/GameSaveCloud/releases/tag/v1.0.0
