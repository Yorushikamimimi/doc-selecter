# 随包运行组件与许可

本项目源码为MIT。候选冻结产物自带CPython 3.12.15及按导入分析收集的标准库／扩展、Tcl/Tk9.0运行库和脚本数据。运行时来自Astral Python Build Standalone 20261013的官方arm64 stripped资产；来源和SHA256固定在`runtime-macos-arm64.json`。完整发行资产的许可文本也按固定SHA256核对，收录于`licenses/`。

- CPython：PSF／CNRI及历史许可，见LICENSE.cpython.txt。
- Tcl/Tk：BSD式Tcl许可，见LICENSE.tcl.txt。
- 标准库底层扩展／运行库可能含OpenSSL、libffi、expat、bzip2、SQLite、mpdecimal、zlib、liblzma等；对应许可文本从同批官方发行资产原样保留。包含发行版附带许可文本不表示全部平台依赖都装入应用，实际运行范围以候选App文件／Mach-O清单为准；系统库不随包重分发。
- PyInstaller 6.22.3：GPL-2.0附冻结产物例外及部分文件Apache-2.0，原COPYING见LICENSE.PyInstaller.txt。例外允许生成物以依赖所允许的许可发布；未修改PyInstaller工具／bootloader。
- pyinstaller-hooks-contrib、altgraph、macholib、packaging、setuptools仅为隔离构建依赖，版本及wheel哈希锁定；不作为用户运行时整体复制。

官方资料：[PyInstaller许可](https://pyinstaller.org/en/stable/license.html)、[macOS冻结／架构及签名](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support)、[PBS运行与许可元数据](https://github.com/astral-sh/python-build-standalone/blob/main/docs/running.rst)、[CPython许可](https://docs.python.org/3.12/license.html)、[Tcl官方许可说明](https://www.tcl-lang.org/about/support.html)。
