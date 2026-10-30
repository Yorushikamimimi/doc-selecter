# 随包运行组件与许可

本项目源码为MIT。候选冻结产物自带CPython 3.12.15及按导入分析收集的标准库／扩展、Tcl/Tk9.0运行库和脚本数据。运行时来自Astral Python Build Standalone 20261013的官方arm64 stripped资产；来源和SHA256固定在`runtime-macos-arm64.json`。完整发行资产的许可文本也按固定SHA256核对，收录于`licenses/`。

- CPython：PSF／CNRI及历史许可，见LICENSE.cpython.txt。
- Tcl/Tk：BSD式Tcl许可，见LICENSE.tcl.txt。
- 标准库底层扩展／运行库可能含OpenSSL、libffi、expat、bzip2、SQLite、mpdecimal、zlib、liblzma等；对应许可文本从同批官方发行资产原样保留。包含发行版附带许可文本不表示全部平台依赖都装入应用，实际运行范围以候选App文件／Mach-O清单为准；系统库不随包重分发。
- PyInstaller 6.22.3：GPL-2.0附冻结产物例外及部分文件Apache-2.0，原COPYING见LICENSE.PyInstaller.txt。例外允许生成物以依赖所允许的许可发布；未修改PyInstaller工具／bootloader。
- pyinstaller-hooks-contrib、altgraph、macholib、packaging、setuptools仅为隔离构建依赖，版本及wheel哈希锁定；不作为用户运行时整体复制。

官方资料：[PyInstaller许可](https://pyinstaller.org/en/stable/license.html)、[macOS冻结／架构及签名](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support)、[PBS运行与许可元数据](https://github.com/astral-sh/python-build-standalone/blob/main/docs/running.rst)、[CPython许可](https://docs.python.org/3.12/license.html)、[Tcl官方许可说明](https://www.tcl-lang.org/about/support.html)。

## 网页界面运行依赖（路线 B）

以下组件仅装入隔离构建环境，由 PyInstaller 按实际导入收集；网页本身不引用 CDN。版本和选定发行文件的 SHA256 固定在 `requirements-macos-build.txt`，哈希逐项与 PyPI 对应版本 JSON 元数据核对。

| 组件 | 版本 | 随包许可原文 |
| --- | --- | --- |
| pywebview | 6.2.1 | BSD-3-Clause，LICENSE.pywebview.txt |
| pyobjc-core、pyobjc-framework-Cocoa、Quartz、Security、UniformTypeIdentifiers、WebKit | 12.2.2 | MIT，各 LICENSE.pyobjc-*.txt |
| bottle | 0.13.4 | MIT，LICENSE.bottle.txt |
| proxy_tools | 0.1.0 | 发行元数据标为 MIT，但上游许可原文为 BSD；随包保留上游原文 LICENSE.proxy_tools.txt，不把元数据标签替代原文 |
| typing_extensions | 4.16.0 | PSF-2.0，LICENSE.typing_extensions.txt |

许可优先从对应 wheel 原样提取；pyobjc-core、Security、UniformTypeIdentifiers 的 wheel 未带许可，取自同版本、经 PyPI SHA256 校验的源码发行包。proxy_tools 0.1.0 源码包未带许可，取自[上游固定提交 db43f1e 的 LICENSE.txt](https://github.com/jtushman/proxy_tools/blob/db43f1e35d4f90a65c5a4d56d9e9af88212ec6e6/LICENSE.txt)。其源码包 SHA256 同样锁定；安装前先安装已锁定的 setuptools，关闭构建隔离，避免临时下载未锁定构建依赖。

系统 WebKit、Cocoa 等 macOS 框架不随包复制。pywebview 桥接所需的动态函数创建在本地 CSP 中仅开放 `unsafe-eval`；资源、连接和嵌入内容仍限制为本地或禁止。没有开启外部下载。默认网页入口，`--tk` 或 `DOCSELECTER_UI=tk` 可切回保留的 Tk 界面。
