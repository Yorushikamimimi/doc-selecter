# Windows 内测包的组件与许可来源

应用源码为 MIT；以下第三方材料保留原文，不以项目许可证替代。

- Python 运行时固定为 PBS 20261013／CPython 3.12.15，Windows x86_64 MSVC 构建，包含 Tcl/Tk 8.6。资产来源与 SHA256 见 `runtime-windows-x86_64.json`。
- Windows 完整运行时发行资产的许可文本按 SHA256 核对后原样保留；列出许可不表示其中每个组件都进入最终包，实际二进制以构建清单为准。
- PyInstaller 6.22.3 的 COPYING（含冻结产物例外）、pywebview 6.2.1 及其余有附带许可的 Python wheel，按发行包元数据收集许可证／NOTICE。构建依赖不整体复制进应用。
- pythonnet 3.0.5、clr-loader 0.2.7.post0 的 wheel 未包含许可证；从各自同版本、经 PyPI SHA256 验证的源码发行包提取 LICENSE，分别保留为 `LICENSE.pythonnet.txt`、`LICENSE.clr-loader.txt`。
- WebView2 SDK 组件版本为 1.0.3856.49（pywebview 锁定 wheel 内 DLL 的 FileVersion）；对应微软许可原文取自同版本 pywebview 源码包中的 `Microsoft.Web.WebView2.LICENSE.md`，保留为 `LICENSE.WebView2.md`。Evergreen WebView2 Runtime 本身不随包分发，不自动安装。
- proxy_tools 0.1.0 继续使用已核对的上游原文 `LICENSE.proxy_tools.txt`；其 PyPI 元数据的 MIT 标签不能替代上游原文中的 BSD 条款。
- 只保留 x64 WebView2 loader 和互操作组件；Microsoft／pythonnet 的 AnyCPU 托管程序集可保留，不能把 PE 的 x86 标记单独当成原生 x86 组件。

补充许可的发行来源、归档 SHA256、成员路径与原文文件 SHA256 均记录在 Windows 运行时锁中。构建时核验这些文件，再放入 ZIP 的 `licenses` 目录；不从用户机器的应用或系统目录搜集许可。

出处：[PBS 官方发行](https://github.com/astral-sh/python-build-standalone/releases/tag/20261013)、[pythonnet 3.0.5](https://pypi.org/project/pythonnet/3.0.5/)、[clr-loader 0.2.7.post0](https://pypi.org/project/clr-loader/0.2.7.post0/)、[pywebview 6.2.1](https://pypi.org/project/pywebview/6.2.1/)、[PyInstaller 许可](https://pyinstaller.org/en/stable/license.html)。
