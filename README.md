# 文件整理助手（doc-selecter）

macOS 本地文件分类工具。选择文件夹、预览整理结果、确认移动，必要时撤销。应用使用 Python 3 + Tkinter 标准库；自包含候选App自带运行时，源码开发与打包另需构建环境。

## 功能

- 按类型分为安装包、文档资料、图片截图、音视频、压缩包、代码与数据、待确认；顶层文档再按格式细分。
- 选择来源和子目录范围，支持受控递归与子目录全选。
- 提示新文件，不自动整理或扩大已确认范围。
- 可选归档本批整理后变空的目录；另有独立的剩余项归档入口。
- 当前会话内按逆序撤销多批操作。

## 下载（待正式发布）

自包含候选版本为 **0.1.0 / Apple Silicon arm64**，用户无需另装Python。候选待主控独立核验，**当前没有正式Release或有效的二进制下载附件**；核验通过后将在 [GitHub Releases](https://github.com/Yorushikamimimi/doc-selecter/releases) 提供DMG和SHA256SUMS，再更新此处下载入口。

候选构建最低系统声明为macOS 11.0，实际只在macOS 27.0（26A428）arm64验证；未在其他macOS版本或另一台干净Mac验证，不称universal。只有本地ad-hoc签名，无Developer ID或公证；网络下载的Gatekeeper体验尚未验证，系统可能拦截，不应关闭全局保护或自动移除隔离属性。候选未安装，也未替换已有应用。

## 从源码运行

需要 macOS、Python 3.12 和可用的 Tkinter 环境（当前验证组合）。系统自带的旧版 Python 可能不支持源码语法。其他平台拒绝文件移动。

```bash
git clone https://github.com/Yorushikamimimi/doc-selecter.git
cd doc-selecter
cp config.json.example config.json
```

编辑本机 `config.json`：设置明确的来源文件夹 `downloads_dir` 和来源／项目之外的状态目录 `state_dir`。配置只留本地。

```bash
python3 -m doc_selecter.gui
```

界面操作：选择来源 → 生成预览 → 勾选范围 → 确认整理。请先用人工文件夹试用，再处理自己的文件。

命令行提供 `plan`、`confirm`、`execute`、`undo`、`recover`、`status`、`remaining-plan`：

```bash
python3 -m doc_selecter --help
```

## 构建自包含App与DMG

采用PyInstaller **6.22.3 onedir/windowed**，使用成熟的Python／Tk收集与macOS动态库重定位，不手写运行时加载器。运行时固定为Astral Python Build Standalone **20261013／CPython3.12.15／Tcl9.0.4与Tk9.0.4／arm64**，构建依赖版本与wheel SHA256锁定；正式图标母版保持不变，构建时生成真实ICNS。

开发者需已有Python3.12及macOS的sips、iconutil、hdiutil、codesign。以下示例输出目录必须尚不存在；再次构建请选择新目录，失败材料不会覆盖或删除：

```bash
python3 -B scripts/prepare_macos_build.py --output-dir build/runtime-0.1.0
build/runtime-0.1.0/venv/bin/python3 -B scripts/build_distributable.py \
  --output-dir build/release/candidate-0.1.0
```

准备入口只在指定新目录下载／校验固定运行时，创建隔离venv并安装哈希锁定wheel，不修改全局Python。可通过`--archive`复用已下载的运行时，但仍核对SHA256。构建入口仅接受固定Python／构建依赖及arm64环境，显式复制22个当前模块和正式图标／许可资源进行冻结，不复制整个venv、项目树或home；旧测试、兼容夹具、config、凭证、状态和用户材料均不打包。PyInstaller收集必要标准库、扩展、Tcl/Tk脚本数据及动态库，不盲目剥除运行组件。

输出包含`dist/文件整理助手.app`、`doc-selecter-0.1.0-macos-arm64.dmg`、`SHA256SUMS`、逐文件构建清单和**拟**Release说明；所有产物在Git忽略的build中。DMG只提供应用、Applications拖放入口及说明，不运行安装器或自动替换旧App。App从自身包定位运行时，不依赖源码目录、cwd、PATH或用户site；首次来源未选，正式状态仍在既有应用支持位置，验证时DOCSELECTER_TEST_ROOT优先。

可复核候选的架构、动态依赖、内部链接、资源权限及最低系统声明：

```bash
python3 -B scripts/audit_macos_bundle.py "build/release/candidate-0.1.0/dist/文件整理助手.app"
```

该审查只读候选包，接受已核对的包内链接和系统库，不把构建声明当成旧系统实测。App为PyInstaller本地ad-hoc签名且资源封印通过校验；没有Apple分发身份或公证。固定依赖／来源可追溯，但不同构建的压缩归档和签名可能不同，不承诺逐字节确定性；以每份候选的校验和为准。

许可及官方选型出处见 [packaging/THIRD_PARTY.md](packaging/THIRD_PARTY.md)：CPython PSF／历史许可、Tcl/Tk BSD式许可、标准库底层依赖许可随包保留；PyInstaller生成物例外允许按依赖许可分发。构建依赖仅用于构建，不整体复制为运行时。

### 保留的本机依赖模式

```bash
python3 scripts/build_macos_app.py
# 或指定已有、支持Tkinter的解释器
python3 scripts/build_macos_app.py --python /absolute/path/to/python3
```

该旧模式需要已有Clang／SDK，绑定外部Python／Tk，**不能用于无需Python的用户分发**。保留其资源、防覆盖与失败诊断契约及原测试；与新的自包含构建入口明确分开。

## 安全与限制

- 同名目标跳过，不覆盖、不自动改名；跨卷跳过，不复制后删除。
- 移动使用 macOS 原子不覆盖原语，检查路径、父目录及文件身份。
- 计划、确认、执行与撤销日志保存在本机；它们是恢复所需状态，不能当普通缓存删除。
- 不跟随链接；按名称和项目标记保护部分敏感项，不读取正文判断隐私。名称规则会漏判或误判，不保证识别所有私人文件。
- 无已知标记的成套资源可能被拆分，预览时应保留这些目录。
- 外部改动、原位置占用、状态丢失可能阻止完整撤销。操作前应暂停下载或其他写入，检查和移动之间仍存在外部竞态。
- 条件空目录归档默认关闭，仅处理本批符合条件的目录。最终空检查后仍可能出现新内容，异常时停止并保留现场，不自动搬回未知内容。
- 不自动删除、去重或清理预先为空的目录；不承诺断电／内核崩溃下的目录持久性。

技术边界见 [docs/MVP方案.md](docs/MVP方案.md)，当前验证状态见 [CURRENT_PROGRESS.md](CURRENT_PROGRESS.md)。

## 开发验证

```bash
python3 -B scripts/test_background.py
```

该入口建立独立人工沙盒，只运行后台子集，不打开可见窗口，不使用本机配置或真实下载目录。结果保存于临时沙盒，不进入仓库。包含 GUI 的完整套件需要单独安排，不能把后台子集通过称为全套通过。

## 许可证

[MIT](LICENSE)。
