# 文件整理助手（doc-selecter）

macOS 本地文件分类工具。选择文件夹、预览整理结果、确认移动，必要时撤销。使用 Python 3 + Tkinter，仅依赖标准库。

## 功能

- 按类型分为安装包、文档资料、图片截图、音视频、压缩包、代码与数据、待确认；顶层文档再按格式细分。
- 选择来源和子目录范围，支持受控递归与子目录全选。
- 提示新文件，不自动整理或扩大已确认范围。
- 可选归档本批整理后变空的目录；另有独立的剩余项归档入口。
- 当前会话内按逆序撤销多批操作。

## 使用

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

## 构建 macOS App

```bash
python3 scripts/build_macos_app.py
# 或明确指定支持 Tkinter 的 Python
python3 scripts/build_macos_app.py --python /absolute/path/to/python3
```

需要已有的 Apple Command Line Tools / Clang / SDK。每次构建输出独立的 `build/macos/<时间-随机组>/文件整理助手.app`；默认绑定执行构建脚本的 Python。

App 不读取项目配置，首次启动后由用户选择来源。**App 依赖构建时指定的本机 Python / Tk，未嵌入运行时；不是跨 Mac 自包含安装包。** 没有 Developer ID 签名或公证，不提供预构建 App 下载。

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
