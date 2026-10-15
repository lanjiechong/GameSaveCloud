# 第三方组件与资源声明

GameSaveCloud 本体以 [MIT 许可证](LICENSE)发布（Copyright (c) 2026 lanjiechong）。
本文件列出分发物中包含、引用或运行时访问的第三方组件及其许可证。

---

## 1. PySide6 / Qt —— GNU LGPL v3

- 用途：图形界面框架（`ui/` 目录下的全部界面代码）
- 版权：The Qt Company Ltd. 及 Qt 贡献者
- 许可证：GNU Lesser General Public License v3（LGPL-3.0）
- 许可证全文：<https://www.gnu.org/licenses/lgpl-3.0.txt>
- 源码获取：<https://download.qt.io/official_releases/QtForPython/>
- PyPI：<https://pypi.org/project/PySide6/>

### LGPL 合规说明

本项目以**动态链接**方式使用 PySide6/Qt，未修改 Qt 库本身，也没有把
Qt 代码静态合并进应用。按照 LGPL-3.0 第 4 节的要求：

1. 分发物中显著标明使用了 Qt，并保留其版权与许可证声明（即本文件）；
2. 用户有权**用自行编译或替换的 PySide6/Qt 版本运行本程序**：
   - 本项目完整源码在 <https://github.com/lanjiechong/GameSaveCloud> 公开；
   - 安装同版本 PySide6（`pip install -r requirements.txt`）后
     `python run.py` 即可用替换后的 Qt 运行；
   - 打包者可用 PyInstaller 重新构建以替换其中的 Qt 二进制
     （`pyinstaller GameSaveCloud.spec --noconfirm --clean`）。
3. 本程序不阻止对其进行逆向调试，以便用户修改 Qt 后调试效果。

## 2. zstandard —— BSD 3-Clause

- 用途：chunk 的 zstd 压缩加速（可选依赖；缺失时自动降级到标准库 zlib）
- 版权：Gregory Szorc 及其他贡献者（Meta 平台 zstd 格式的 Python 绑定）
- 许可证：BSD 3-Clause License
- 主页：<https://github.com/indygreg/python-zstandard>

## 3. Python 运行时 —— PSF License

- 用途：打包后的 exe 内嵌 Python 解释器（标准库）
- 许可证：Python Software Foundation License（兼容 GPL，BSD 式宽松许可）
- 许可证全文：<https://docs.python.org/3/license.html>

本程序用到的其他标准库（sqlite3、urllib、hashlib、ctypes、zlib、
winreg 等）均为 Python 标准库的一部分，适用同一许可证。

## 4. 游戏图标（商标/版权归各游戏厂商所有）

本项目**不再在仓库和安装包中内置、再分发任何第三方游戏 logo**。
界面中的游戏图标全部在**运行时**按下列方式获得并缓存在用户本机：

- 来源：Steam 商店公开搜索 API 与 Steam 公共 CDN
  （`store.steampowered.com/api/storesearch`、
  `cdn.akamai.steamstatic.com/steam/apps/<appid>/`）；
- 用途：仅用于在界面中**标识对应的游戏**（指示性使用，nominative use），
  不与任何游戏构成赞助、授权或关联关系；
- 各游戏名称、logo、商标的版权归其各自所有者所有（Valve、Rockstar、
  FromSoftware 等）；
- 离线或无法取得真实图标时，显示的是本程序自行绘制的纯文字色块
  （配色 + 游戏名缩写，不含任何第三方素材）。

构建工具 `scripts/fetch_top_logos.py` 仅供开发者在**本机**预取图标，
其产物目录 `ui/assets/logos/` 已被 `.gitignore` 排除、也不进入发布打包。

## 5. 许可证清单自查

```powershell
# 列出当前环境全部直接/间接依赖的名称、版本与许可证
python scripts/check_licenses.py
```

---

如认为本项目中有任何素材侵犯了您的权利，请在
<https://github.com/lanjiechong/GameSaveCloud/issues> 提交 issue，
核实后会立即移除。
