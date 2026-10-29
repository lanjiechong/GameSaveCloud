# -*- coding: utf-8 -*-
r"""
常见游戏存档目录的自动探测。

两条腿走路：
1. 内置一批主流单机游戏的「已知存档位置」——这些位置没有规律可言
   （Unity 死锁 LocalLow\<公司>\<游戏>，R星在 文档\Rockstar Games，
   老 Bethesda 在 我的游戏\The Elder Scrolls V Skyrim ...），只能查表。
2. 通用启发式扫描——遍历各平台的存档根目录，把「含存档特征文件」的
   子目录报告出来，用于覆盖查表之外的游戏。

去噪规则很重要：大量目录只是缓存/日志/崩溃转储，不报告；
已经添加过的路径不会重复报告。
"""
import os
import sys

from .models import Game

# ---------------------------------------------------------------
# 平台存档根目录
# 第三个字段标记该根是否参与「启发式扫描」：
#   AppData\Local 与 ProgramData 被绝大多数普通软件占用，
#   在那里做启发式扫描只会得到一堆应用数据目录，所以不扫。
# ---------------------------------------------------------------
def _roots():
    """返回 [(根目录, 平台标签, 是否参与启发式扫描), ...]"""
    out = []
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", "")
        local = os.environ.get("LOCALAPPDATA", "")
        low = os.path.normpath(os.path.join(local, "..", "LocalLow")) if local else ""
        docs = os.path.join(home, "Documents")
        pd = os.environ.get("PROGRAMDATA", "")
        for p, tag, heur in ((local, "Windows", False),
                             (low, "Windows", True),
                             (appdata, "Windows", True),
                             (pd, "Windows", False)):
            if p and os.path.isdir(p):
                out.append((p, tag, heur))
        for sub in ("My Games", "Saved Games"):
            p = os.path.join(docs, sub)
            if os.path.isdir(p):
                out.append((p, "Windows", True))
        for steam in ("C:\\steam", "C:\\Program Files (x86)\\Steam",
                      "C:\\Program Files\\Steam", "D:\\steam", "E:\\steam"):
            p = os.path.join(steam, "userdata")
            if os.path.isdir(p):
                out.append((p, "Steam 云", False))
    elif sys.platform == "darwin":
        p = os.path.join(home, "Library", "Application Support")
        if os.path.isdir(p):
            out.append((p, "macOS", True))
    else:
        for sub in (".local/share", ".config"):
            p = os.path.join(home, sub)
            if os.path.isdir(p):
                out.append((p, "Linux", True))
    return out


# ---------------------------------------------------------------
# 内置查表：主流单机/独立游戏的已知存档位置（相对各根目录）
# ---------------------------------------------------------------
KNOWN_GAMES = [
    ("艾尔登法环 Elden Ring", "Steam", [" Elden Ring".strip()], "AppData\\Roaming"),
    ("博德之门3 Baldur's Gate 3", "Steam", ["Larian Studios\\Baldur's Gate 3"], "AppData\\Local"),
    ("赛博朋克2077 Cyberpunk 2077", "Steam/GOG", ["CD Projekt Red\\Cyberpunk 2077"], "AppData\\Roaming"),
    ("巫师3 The Witcher 3", "Steam/GOG", ["The Witcher 3"], "Documents"),
    ("荒野大镖客2 Red Dead Redemption 2", "Rockstar", ["Rockstar Games\\Red Dead Redemption 2\\Profiles"], "Documents"),
    ("GTA V", "Rockstar/Steam", ["Rockstar Games\\GTA V\\Profiles"], "Documents"),
    ("黑神话：悟空 Black Myth Wukong", "Steam", ["Black Myth Wukong"], "AppData\\Local"),
    ("只狼 Sekiro", "Steam", ["Sekiro"], "AppData\\Roaming"),
    ("黑暗之魂3 Dark Souls III", "Steam", ["DarkSoulsIII"], "AppData\\Roaming"),
    ("血源诅咒 Bloodborne", "PS模拟", ["Bloodborne"], "AppData\\Roaming"),
    ("艾尔登法环 夜之统治", "Steam", ["Nightreign"], "AppData\\Roaming"),
    ("怪物猎人：世界 Monster Hunter World", "Steam", ["MonsterHunterWorld"], "AppData\\Roaming"),
    ("怪物猎人：崛起 Monster Hunter Rise", "Steam", ["MonsterHunterRise"], "AppData\\Roaming"),
    ("尼尔：机械纪元 NieR Automata", "Steam", ["NieR Automata"], "AppData\\Roaming"),
    ("最终幻想15 Final Fantasy XV", "Steam", ["My Games\\FINAL FANTASY XV"], "Documents"),
    ("最终幻想7 重制版 FF7 Remake", "Steam", ["My Games\\FINAL FANTASY VII REMAKE"], "Documents"),
    ("鬼泣5 Devil May Cry 5", "Steam", ["DevilMayCry5"], "AppData\\Roaming"),
    ("生化危机4 重制版 RE4 Remake", "Steam", ["RESIDENT EVIL 4 BIOHAZARD"], "AppData\\Roaming"),
    ("生化危机8 村庄 RE Village", "Steam", ["RESIDENT EVIL VILLAGE"], "AppData\\Roaming"),
    ("怪物猎人xx", "Steam", ["MonsterHunterXX"], "AppData\\Roaming"),
    ("空洞骑士 Hollow Knight", "Steam", ["Hollow Knight"], "AppData\\LocalLow\\Team Cherry"),
    ("丝之歌 Silksong", "Steam", ["Hollow Knight Silksong"], "AppData\\LocalLow\\Team Cherry"),
    ("空洞骑士 本地", "Steam", ["Team Cherry\\Hollow Knight"], "AppData\\LocalLow"),
    ("死亡细胞 Dead Cells", "Steam", ["dead-cells"], "AppData\\Roaming"),
    ("哈迪斯 Hades", "Steam/GOG", ["Hades"], "AppData\\Roaming\\Supergiant Games"),
    ("哈迪斯2 Hades II", "Steam", ["Hades II"], "AppData\\Roaming\\Supergiant Games"),
    ("晶体管 Transistor", "Steam", ["Transistor"], "AppData\\Roaming\\Supergiant Games"),
    ("传说之下 Undertale", "Steam", ["UNDERTALE"], "AppData\\LocalLow\\tobyfox"),
    ("三角符文 Deltarune", "Steam", ["DELTARUNE"], "AppData\\LocalLow\\tobyfox"),
    ("星露谷物语 Stardew Valley", "Steam/GOG", ["StardewValley"], "AppData\\Roaming"),
    ("泰拉瑞亚 Terraria", "Steam", ["Terraria"], "Documents\\My Games"),
    (" Minecraft Java 版", "Mojang", [".minecraft\\saves"], "AppData\\Roaming"),
    ("Valheim 英灵神殿", "Steam", ["Valheim"], "AppData\\LocalLow\\IronGate"),
    ("荒野大镖客：救赎 RDR1", "Rockstar", ["Rockstar Games\\Red Dead Redemption"], "Documents"),    ("双人成行 It Takes Two", "Steam", ["It Takes Two"], "AppData\\Local"),
    ("逃出生天 A Way Out", "Steam", ["A Way Out"], "AppData\\Local"),
    ("双影奇境 Split Fiction", "Steam", ["SplitFiction"], "AppData\\Local"),
    ("霍格沃茨之遗 Hogwarts Legacy", "Steam/Epic", ["HogwartsLegacy\\Saved\\SaveGames"], "AppData\\Local"),
    ("地平线：西之绝境 Horizon Forbidden West", "Steam", ["Horizon Forbidden West"], "AppData\\Roaming"),
    ("对马岛之魂 Ghost of Tsushima", "Steam", ["Ghost of Tsushima Director's Cut"], "AppData\\Roaming"),
    ("战神4 God of War", "Steam", ["God of War"], "AppData\\Roaming\\Sony Interactive Entertainment"),
    ("蜘蛛侠 Marvel's Spider-Man", "Steam", ["Marvel's Spider-Man"], "AppData\\Roaming\\Sony"),
    ("最后生还者 The Last of Us Part I", "Steam", ["The Last of Us Part I"], "AppData\\Roaming\\Naughty Dog"),
    ("师父 Sifu", "Steam", ["Sifu"], "AppData\\Local"),
    ("师父 云存档", "Steam", ["Sloclap\\Sifu"], "AppData\\Local"),
    ("潜水员戴夫 Dave the Diver", "Steam", ["Dave the Diver"], "AppData\\LocalLow\\Mintrocket"),
    ("潜水员戴夫 本地", "Steam", ["Mintrocket\\Dave the Diver"], "AppData\\LocalLow"),
    ("鹅鸭杀 Goose Goose Duck", "Steam", ["Goose Goose Duck"], "AppData\\LocalLow\\Gaggle"),
    ("致命公司 Lethal Company", "Steam", ["Lethal Company"], "AppData\\LocalLow\\Zeekerss"),
    ("内容警告 Content Warning", "Steam", ["Content Warning"], "AppData\\LocalLow\\Landfall"),
    ("全面战争模拟器 Totally Accurate Battle Simulator", "Steam", ["Totally Accurate Battle Simulator"], "AppData\\LocalLow\\Landfall"),
    ("森林 The Forest", "Steam", ["The Forest"], "AppData\\LocalLow\\Endnight"),
    ("森林之子 Sons of the Forest", "Steam", ["Sons Of The Forest"], "AppData\\LocalLow\\Endnight"),
    ("绿色地狱 Green Hell", "Steam", ["Green Hell"], "AppData\\LocalLow\\Creepy Jar"),
    ("Raft 木筏求生", "Steam", ["Raft"], "AppData\\LocalLow\\Redbeet"),
    ("阴森之子", "Steam", ["Sons Of The Forest"], "AppData\\LocalLow\\Endnight"),
    ("禁闭求生 Grounded", "Steam", ["Grounded"], "AppData\\LocalLow\\Obsidian"),
    ("亡灵巫师2", "Steam", ["Lichdom II"], "AppData\\Roaming"),
    ("雾锁王国 Enshrouded", "Steam", ["Enshrouded"], "AppData\\LocalLow\\Keen"),
    ("夜族崛起 V Rising", "Steam", ["V Rising"], "AppData\\LocalLow\\Stunlock Studios"),
    ("幸福工厂 Satisfactory", "Steam/Epic", ["FactoryGame"], "AppData\\Local"),
    ("异星工厂 Factorio", "Steam", ["Factorio"], "AppData\\Roaming"),
    ("机械纪元 Automata", "Steam", ["NieRAutomata"], "AppData\\Roaming"),
    ("暗黑破坏神4 Diablo IV", "战网", ["Diablo IV"], "Documents"),
    ("星际争霸2", "战网", ["StarCraft II"], "Documents"),
    ("魔兽世界 World of Warcraft", "战网", ["World of Warcraft\\_retail_\\WTF"], "AppData\\Roaming"),
    ("城市：天际线 Cities: Skylines", "Steam", ["Colossal Order\\Cities_Skylines"], "AppData\\Local"),
    ("城市：天际线2 Cities: Skylines II", "Steam", ["Colossal Order\\Cities Skylines II"], "AppData\\Local"),
    ("欧卡2 Euro Truck Simulator 2", "Steam", ["Euro Truck Simulator 2"], "Documents\\Euro Truck Simulator 2"),
    ("美卡 American Truck Simulator", "Steam", ["American Truck Simulator"], "Documents\\American Truck Simulator"),
    ("微软飞行模拟 MSFS", "Steam/Xbox", ["Flight Simulator"], "AppData\\Local\\Packages"),
    # 注：Rockstar 的 "Social Club" 目录只是账号/配置，不是存档，
    # 早先把它当「荒野大镖客2 云」报出来属于误报，已删除。
    ("方舟：生存进化 Ark: Survival Evolved", "Steam", ["ARK"], "AppData\\Local"),
    ("英灵神殿 Valheim 世界", "Steam", ["Valheim\\worlds"], "AppData\\LocalLow\\IronGate"),
    ("噬血代码 Code Vein", "Steam", ["CodeVein"], "AppData\\Roaming\\Bandai Namco Entertainment"),
    ("噬神者3 God Eater 3", "Steam", ["GodEater3"], "AppData\\Roaming\\Bandai Namco"),
    ("火影忍者：终极风暴", "Steam", ["BANDAI NAMCO Entertainment"], "AppData\\Roaming"),
    ("JOJO的奇妙冒险 群星之战", "Steam", ["JOJO's Bizarre Adventure"], "AppData\\Roaming"),
    ("灵魂能力6 SoulCalibur VI", "Steam", ["SoulCaliburVI"], "AppData\\Roaming\\Bandai Namco"),
    ("铁拳7 Tekken 7", "Steam", ["TEKKEN 7"], "AppData\\Roaming\\Bandai Namco Entertainment"),
    ("街霸5 Street Fighter V", "Steam", ["StreetFighterV"], "AppData\\Roaming\\Capcom"),
    ("怪物猎人 stories2", "Steam", ["MonsterHunterStories2"], "AppData\\Roaming"),
    ("勇气默示录2", "Steam", ["Bravely Default II"], "AppData\\Roaming\\SquareEnix"),
    ("时空勇士 Live A Live", "Steam", ["LIVE A LIVE"], "AppData\\Roaming\\SquareEnix"),
    ("歧路旅人 Octopath Traveler", "Steam", ["OCTOPATH TRAVELER"], "AppData\\Roaming\\SquareEnix"),
    ("三角战略 Triangle Strategy", "Steam", ["TRIANGLE STRATEGY"], "AppData\\Roaming\\SquareEnix"),
    ("北欧女神 极乐世界", "Steam", ["VALKYRIE ELYSIUM"], "AppData\\Roaming\\SquareEnix"),
    ("神领编年史 The DioField Chronicle", "Steam", ["The DioField Chronicle"], "AppData\\Roaming\\SquareEnix"),
    ("最终幻想起源 Stranger of Paradise", "Steam", ["STRANGER OF PARADISE"], "AppData\\Roaming\\SquareEnix"),
    ("forspoken", "Steam", ["Forspoken"], "AppData\\Roaming\\Square Enix"),
    ("漫威银河护卫队 Guardians", "Steam", ["Guardians of the Galaxy"], "AppData\\Roaming\\Square Enix"),
    (" Outriders 先遣战士", "Steam", ["Outriders"], "AppData\\Local\\Madness"),
    ("遗迹：灰烬重生 Remnant", "Steam", ["Remnant"], "AppData\\Local\\Perfect World"),
    ("尘埃异变2GR", "Steam", ["2GR"], "AppData\\LocalLow"),
    ("霓虹白 Neon White", "Steam", ["Neon White"], "AppData\\LocalLow\\Annapurna"),
    ("Stray 迷失", "Steam", ["Stray"], "AppData\\LocalLow\\BlueTwelve"),
    ("needy streamer overload", "Steam", ["needy streamer overload"], "AppData\\LocalLow\\WSS"),
    ("主播女孩重度依赖 中文", "Steam", ["needy girl overdose"], "AppData\\LocalLow\\WSS"),
    ("9-nine-", "Steam", ["9-nine-"], "AppData\\LocalLow\\PALETTE"),
    ("千恋万花 Senren Banka", "Steam", ["Senren*Banka"], "AppData\\LocalLow\\Yuzusoft"),
    ("星光咖啡馆与死神之蝶", "Steam", ["Cafe Stella"], "AppData\\LocalLow\\Yuzusoft"),
    ("天使嚣嚣 RE BOOT", "Steam", ["Angelibots"], "AppData\\LocalLow\\Yuzusoft"),
    ("RIDDLE JOKER", "Steam", ["RIDDLE JOKER"], "AppData\\LocalLow\\Yuzusoft"),
    ("永乐店 2025", "独立", ["永"], "AppData\\Roaming"),
    ("山河旅探", "独立", ["山河旅探"], "AppData\\LocalLow"),
    ("山河旅探 存档", "独立", ["MysticalLane"], "AppData\\LocalLow"),
    ("大多数", "独立", ["Most"], "AppData\\LocalLow"),
    ("大多数 存档", "独立", ["U.Game"], "AppData\\LocalLow"),
    ("大多数 本地", "独立", ["大多数"], "Documents"),
    ("完蛋！我被美女包围了", "独立", ["完蛋！我被美女包围了！"], "AppData\\LocalLow"),
    ("烟火 Firework", "独立", ["Firework"], "AppData\\LocalLow"),
    ("三伏", "独立", ["三伏"], "AppData\\LocalLow"),
    ("黑棉花：悟能传奇", "独立", ["黑棉花：悟能传奇"], "AppData\\LocalLow"),
    ("明末：渊虚之羽", "独立", ["WUCHANG"], "AppData\\Local"),
    ("明末 云虚之羽", "独立", ["WUCHANG: Fallen Feathers"], "AppData\\Local"),
    ("昭和米国物语", "独立", ["Showa American Story"], "AppData\\LocalLow"),
    ("墨影侠踪", "独立", ["墨影侠踪"], "AppData\\Roaming"),
    ("大侠立志传", "独立", ["大侠立志传"], "AppData\\LocalLow"),
    ("江湖十一", "独立", ["江湖十一"], "AppData\\LocalLow"),
    ("鬼谷八荒", "Steam", ["鬼谷八荒"], "AppData\\LocalLow\\Easy Game"),
    ("鬼谷八荒 存档", "Steam", ["Easy Game\\鬼谷八荒"], "AppData\\LocalLow"),
    ("太吾绘卷", "Steam", ["太吾绘卷"], "AppData\\LocalLow\\ConchShip"),
    ("了不起的修仙模拟器", "Steam", ["Amazing Cultivation Simulator"], "AppData\\LocalLow\\GSQ"),
    ("部落与弯刀", "Steam", ["部落与弯刀"], "AppData\\LocalLow\\汉家松鼠"),
    ("汉家江湖", "Steam", ["汉家江湖"], "AppData\\LocalLow"),
    ("金庸群侠传", "Steam", ["金庸群侠传"], "AppData\\Roaming"),
    ("河洛群侠传", "Steam", ["河洛群侠传"], "AppData\\Roaming\\Heluo"),
    ("侠客风云传", "Steam", ["Tale of Wuxia"], "AppData\\Roaming\\Heluo"),
    ("侠客风云传 前传", "Steam", ["Tale of Wuxia:The Pre-Sequel"], "AppData\\Roaming\\Heluo"),
    ("武林群侠传", "Steam", ["Heroes of Wuxia"], "AppData\\Roaming\\Heluo"),
    ("三国志14", "Steam", ["三国志14"], "Documents\\KoeiTecmo\\RomanceOfThreeKingdoms"),
    ("仁王2 Nioh 2", "Steam", ["Nioh2"], "AppData\\Roaming\\KoeiTecmo"),
    ("卧龙：苍天陨落 Wo Long", "Steam", ["Wo Long"], "AppData\\Roaming\\KoeiTecmo"),
    ("狂野之心 Wild Hearts", "Steam", ["Wild Hearts"], "AppData\\Roaming"),
    ("碧蓝幻想：Relink", "Steam", ["Granblue Fantasy Relink"], "AppData\\Roaming\\Cygames"),
    ("破晓传说 Tales of Arise", "Steam", ["Tales of Arise"], "AppData\\Roaming\\Bandai Namco"),
    ("情热传说", "Steam", ["Tales of Zestiria"], "AppData\\Roaming\\Bandai Namco"),
    ("狂战传说", "Steam", ["Tales of Berseria"], "AppData\\Roaming\\Bandai Namco"),
    ("绯夜传说", "Steam", ["Tales of Berseria"], "AppData\\Roaming\\Bandai Namco"),
    ("薄暮传说", "Steam", ["Tales of Vesperia"], "AppData\\Roaming\\Bandai Namco"),
    ("无尽传说", "Steam", ["Tales of Xillia"], "AppData\\Roaming\\Bandai Namco"),
    ("圣恩传说", "Steam", ["Tales of Graces"], "AppData\\Roaming\\Bandai Namco"),
    ("仙乐传说", "Steam", ["Tales of Symphonia"], "AppData\\Roaming\\Bandai Namco"),
    ("永恒传说", "Steam", ["Tales of Eternia"], "AppData\\Roaming\\Bandai Namco"),
    ("宿命传说", "Steam", ["Tales of Destiny"], "AppData\\Roaming\\Bandai Namco"),
    ("幻想传说", "Steam", ["Tales of Phantasia"], "AppData\\Roaming\\Bandai Namco"),
    ("心灵传说", "Steam", ["Tales of Hearts"], "AppData\\Roaming\\Bandai Namco"),
    ("美德传说", "Steam", ["Tales of Innocence"], "AppData\\Roaming\\Bandai Namco"),
    ("世界传说", "Steam", ["Tales of the World"], "AppData\\Roaming\\Bandai Namco"),
    (" rescue", "独立", ["res"], "AppData\\Roaming"),
]

# 存档特征：文件名或扩展名命中这些，就认为该目录可能含存档。
# 只收「几乎只有游戏才会用」的强特征 —— .txt/.json/.ini/.xml/.cfg/.bin 这类
# 通用配置文件一律不算，否则剪映、BitComet、AMD 驱动全都会被误判成游戏存档。
SAVE_HINTS = (
    "save", "saves", "slot", "profile", "profiles", "progress", "checkpoint",
    "userdata", "gamedata", "worlds", "characters", "campaign",
    ".sav", ".save", ".savegame", ".rpgsave", ".sl2", ".sps", ".ibg",
    ".es3", ".run", ".dat.sav", ".sav.dat",
)
# 明确的噪声目录，命中即跳过
NOISE = (
    "cache", "caches", "log", "logs", "crash", "crashes", "crashreportclient",
    "temp", "tmp", "shader", "shadercache", "gpucache", "code cache",
    "thumbcache", "webcache", "gpucrash", "diagnostics", "analytics",
    "telemetry", "metrics", "reports", "dumps", "minidump", "backup",
    "node_modules", "packages", "update", "updater", "installer", "uninstall",
    "microsoft", "windows", "common files", "internet explorer",
    # 已知非游戏软件厂商 / 工具，命中目录名直接跳过
    "amd", "asus", "intel", "nvidia", "google", "mozilla", "adobe",
    "bitcomet", "idm", "thunder", "xunlei", "baidunetdisk", "123pan",
    "aliyundrive", "clouddrive", "pan",
    "jianyingpro", "capcut", "ffmpeg", "obs ", "vlc", "potplayer", "mpv",
    "code", "codebuddy", "cursor", "jetbrains", "pycharm", "visual studio",
    "nodejs", "python", "pip", "npm", "yarn", "git", "github", "docker", "wsl",
    "qq", "wechat", "wecom", "dingtalk", "feishu", "lark", "tencent",
    "netease", "bilibili", "douyin", "kuaishou",
    "openai", "anthropic", "claude", "chatgpt", "copilot",
    "pc manager", "360", "huorong", "defender", "malwarebytes", "ccleaner",
    "everything", "listary", "powertoys", "snipaste", "sharex", "dism",
    "bandizip", "7-zip", "winrar", "notepad++", "sublime", "typora",
    "obsidian", "zotero", "calibre", "foxit", "pdf", "wps", "office",
    "onedrive", "dropbox", "baidu", "sogou", "ime", "input",
    "netframework", ".net", "vcredist", "dotnet", "crashpad", "squirreltemp",
    "inno setup", "installshield", "wise", "iobit", "driver booster",
    "geforce", "ryzen", "vortex", "afterburner", "hwinfo", "cpu-z", "gpu-z",
    "aida64", "crystaldisk", "fastcopy", "teracopy", "freefilesync",
    "goodsync", "beyond compare", "winscp", "filezilla", "xshell",
    "securecrt", "mobaXterm", "putty", "vmware", "virtualbox", "hyper-v",
    "sandboxie", "magpie", "lossless", "wallpaper", "rainmeter",
    "translucent", "fences", "rocketdock", "startallback", "explorerpatcher",
    "chameleon", "bypass", "aclog", "acsdkinslog", "nrc", "camera bits",
    "playerkit", "fabplugins", "gameviewer", "gamepp", "gamesave", "farm",
    "programs", "publishers", "edge", "iexplore", "outlook", "teams",
    "skype", "zoom", "meeting", "voov", "xsplit", "streamlabs",
    # 开发/工具类，容易混进启发式结果
    "godot", "unity", "unreal", "blender", "krita", "gimp", "inkscape",
    "clash", "verge", "gamesir", "078", "backlight", "com.example",
    "io.github", "zen", "electron", "tauri", "cef", "chromium",
)


def _dir_file_count(path: str, max_files: int = 2000) -> tuple:
    """统计目录下普通文件数与总字节（不做存档特征过滤）。查表时用。"""
    n = 0
    total = 0
    try:
        for r, dirs, fs in os.walk(path):
            dirs[:] = [d for d in dirs if d.lower() not in NOISE]
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(r, f))
                    n += 1
                except OSError:
                    pass
                if n > max_files:
                    return n, total
    except OSError:
        return 0, 0
    return n, total


def _looks_like_save_dir(path: str, max_files: int = 400) -> tuple:
    """返回 (文件数, 总字节)。只统计命中强存档特征的文件，纯噪声目录返回 0。"""
    n = 0
    total = 0
    try:
        for r, dirs, fs in os.walk(path):
            dirs[:] = [d for d in dirs if d.lower() not in NOISE]
            for f in fs:
                low = f.lower()
                if not any(h in low for h in SAVE_HINTS):
                    continue
                try:
                    total += os.path.getsize(os.path.join(r, f))
                    n += 1
                except OSError:
                    pass
                if n > max_files:
                    return n, total
    except OSError:
        return 0, 0
    return n, total


class FoundGame:
    def __init__(self, name, platform, paths, files, size, source):
        self.name = name
        self.platform = platform
        self.paths = paths
        self.files = files
        self.size = size
        self.source = source      # known / heuristic

    def __repr__(self):
        return "<FoundGame %s %s %d files>" % (self.name, self.paths, self.files)


def scan(known_paths=None, heuristic=True, max_depth=3) -> list:
    """扫描本机可备份的存档目录。

    known_paths: 已添加的游戏路径，用于去重，不重复报告。
    heuristic:   是否启用通用启发式扫描（查表之外的游戏）。
    """
    known_paths = {os.path.normpath(p).lower() for p in (known_paths or [])}
    found = {}
    order = []

    def add(name, platform, paths, files, size, source):
        key = tuple(sorted(os.path.normpath(p).lower() for p in paths))
        if key in found:
            return
        if any(os.path.normpath(p).lower() in known_paths for p in paths):
            return
        if not files:
            return
        found[key] = FoundGame(name, platform, paths, files, size, source)
        order.append(key)

    # ---------- 1. 查表：路径已知，宽松判定 ----------
    for name, platform, rels, anchor in KNOWN_GAMES:
        for root, tag, _heur in _roots():
            base = _resolve_anchor(root, anchor)
            if not base:
                continue
            for rel in rels:
                p = os.path.join(base, rel)
                if os.path.isdir(p):
                    files, size = _dir_file_count(p)
                    if files:
                        add(name, platform, [p], files, size, "known")
                elif os.path.isfile(p):
                    try:
                        add(name, platform, [p], 1, os.path.getsize(p), "known")
                    except OSError:
                        pass

    # ---------- 2. 启发式：只用强存档特征，且只扫游戏专属根 ----------
    if heuristic:
        for root, tag, heur in _roots():
            if not heur:
                continue
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                continue
            for name in entries:
                low = name.lower()
                if low in NOISE or any(low == n or low.startswith(n) for n in NOISE):
                    continue
                p = os.path.join(root, name)
                if not os.path.isdir(p):
                    continue
                # LocalLow 的布局是 <工作室>\<游戏>。工作室那一层不是游戏名 ——
                # 拿「AIHASTO」「House House」去 Steam 搜一条都搜不到，
                # 实际游戏分别是 MiSide、Big Walk。所以先下钻一层找真正的
                # 游戏目录，找到就不再把这个工作室名当游戏。
                # 只对 LocalLow 下钻：AppData 下面同样是「厂商\产品」结构，
                # 但那里的厂商目录里塞的是各种软件，下钻只会扫出更多噪音。
                if _is_locallow(root):
                    got_sub = False
                    try:
                        subs = sorted(os.listdir(p))
                    except OSError:
                        subs = []
                    for sub in subs:
                        if sub.lower() in NOISE:
                            continue
                        sp = os.path.join(p, sub)
                        if not os.path.isdir(sp):
                            continue
                        f2, s2 = _looks_like_save_dir(sp, max_files=60)
                        if f2:
                            add(sub, tag, [sp], f2, s2, "heuristic")
                            got_sub = True
                    if got_sub:
                        continue
                files, size = _looks_like_save_dir(p, max_files=60)
                if files:
                    add(name, tag, [p], files, size, "heuristic")

    return [found[k] for k in order]


def _is_locallow(path: str) -> bool:
    """这个根是不是 LocalLow（Unity 游戏专用的 <工作室>\\<游戏> 布局）。"""
    return "locallow" in (path or "").replace("\\", "/").lower()


def _resolve_anchor(root: str, anchor: str):
    """把 'AppData\\Roaming' / 'Documents' 之类的锚点解析成真实目录。"""
    a = anchor.strip()
    if not a or a == ".":
        return root
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        if a.startswith("AppData\\Roaming"):
            return os.path.join(os.environ.get("APPDATA", ""),
                                a[len("AppData\\Roaming"):].lstrip("\\"))
        if a.startswith("AppData\\Local"):
            return os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                a[len("AppData\\Local"):].lstrip("\\"))
        if a.startswith("AppData\\LocalLow"):
            low = os.path.normpath(os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                                "..", "LocalLow"))
            return os.path.join(low, a[len("AppData\\LocalLow"):].lstrip("\\"))
        if a.startswith("Documents"):
            return os.path.join(home, "Documents",
                                a[len("Documents"):].lstrip("\\"))
        if a.startswith("ProgramData"):
            return os.path.join(os.environ.get("PROGRAMDATA", ""),
                                a[len("ProgramData"):].lstrip("\\"))
    return os.path.join(root, a)
