# Windows 内测构建与验收

本文件描述 Windows 分支的使用、构建和验收约束；项目当前进度仍以 `CURRENT_PROGRESS.md` 为唯一入口。
源码实现和工作流不等于已在 Windows 上运行通过。没有通过 Windows 原生检查前，不提供可用性承诺。

## 支持范围

- Windows 11、原生 x64、本地 NTFS、普通用户权限。ARM64 仿真、网络盘、其他文件系统不支持。
- 先选择来源，只读预览，明确确认后移动；同名跳过、不覆盖、不自动改名、不复制后删除；按日志条件撤销。
- 只读取名称和元数据，文件正文不用于分类。能力探测和测试可以读写**自身创建的人工材料**。
- 首版禁用空目录归档和剩余项归档，界面禁用且后端拒绝伪造请求。旧 macOS 计划不能用于 Windows 移动、撤销或恢复。
- 拒绝所有重解析点，包括 junction、符号链接和云占位对象；跳过 `.lnk/.url`，不解析其目标。
- 跳过 Windows 隐藏、系统、离线及召回属性文件；拒绝大小写敏感目录、8.3 短目录别名、SUBST 映射。
- 路径必须为盘符绝对路径；原始路径不能包含 `.`、`..`、空段、设备命名空间或 ADS。保留设备名、尾点、尾空格及非 NFC 名称不支持，不做自动改名。
- 完整路径限制为少于 248 个 UTF-16 单元，包含分类后的目标路径；过长目标会使计划被拒绝，不截断名称。
- 已知 OneDrive、Dropbox、Google Drive、iCloudDrive、Box 路径被保守拒绝。其他同步软件或自定义同步目录不能可靠识别；本版不支持在同步目录使用。整理时暂停其他写入。
- 文件占用导致无法获得安全移动句柄时跳过；撤销遇占用、身份变化或原位冲突会保留双方并要求检查，不强制解锁。
- 不承诺断电、内核崩溃或存储设备故障下的目录持久性。文件日志刷盘失败立即停止；Windows 不把目录 `fsync` 伪装为成功。

## 安全实现边界

`doc_selecter/windows/` 是独立后端。共享模块只在 `os.name == 'nt'` 时使用它，不替换全局 `os`，Darwin 仍使用原有库与原语。

- `NtCreateFile` 使用父句柄＋单段名称，配合 `OBJ_DONT_REPARSE`、`FILE_OPEN_REPARSE_POINT`；持有祖先目录句柄且不共享 DELETE 权限。
- `SetFileInformationByHandle(FileRenameInfo)` 对已核验的源句柄移动，目标是已持有的目录句柄；覆盖标志为 false。最终调用前重核源的卷序列号、128 位 File ID、大小、修改时间和类型。
- `windows.io` 仅适配现有引擎使用的文件系统操作。目录对象不是 CRT 文件描述符；文件描述符仍是真实 CRT fd，并持有父链。
- 不读取用户文件正文获得身份。128 位 File ID 保留为 Python 整数；JS 不负责计算身份或计划哈希。
- Windows 计划的 `platform=windows-ntfs-v1` 参与哈希；原 macOS 计划投影不变。计划、确认、intent/result、撤销和恢复沿用现有引擎。
- `LockFileEx` 提供独占非阻塞锁，应用锁和操作锁分开。状态目录拒绝对其他普通用户开放写入的 ACL；不提升权限或修改系统 ACL。
- 新目标目录仍记录创建 intent、身份和 result；Windows 不执行未证明的目录刷盘。目录归档功能因此不启用。

## 界面与分发

网页界面使用 WebView2；检查 Evergreen Runtime 与 .NET Framework，缺失时提供微软下载地址并询问是否改用 Tk。程序不下载安装运行时。冻结包排除 MSHTML 后端，避免静默切到旧 IE 引擎。

成品为完整目录 ZIP：解压后运行 `FileOrganizer.exe`，不要只复制 exe。自带固定 Python、Tcl/Tk 和 Python 依赖，仍需要系统的 WebView2/.NET 才能使用网页界面；`FileOrganizer.exe --tk` 可明确选择备用界面。无签名、无安装器、不自动替换旧应用。

默认记录位于 Windows 提供的 LocalAppData 下的 `文件整理助手/state`。测试必须设置 `DOCSELECTER_TEST_ROOT`，优先于默认状态和配置。记录是恢复依据，不作为缓存清理。首次打开不选择真实 Downloads。

SmartScreen、Defender 或组织策略可能阻止运行，不能承诺“无提示”。不要关闭保护、加排除或导入自签名根证书。公开发布和签名另行决定。

## 手动 GitHub Actions

工作流：`.github/workflows/windows-preview.yml`，仅 `workflow_dispatch`，仓库权限 `contents: read`，不保存 checkout 凭证、不使用 secrets、不发布 Release。

1. 提交、推送分别授权并完成后，在 Actions 手动运行 **Windows preview (manual)**。
2. 在全新目录准备 SHA256 锁定的 PBS CPython 3.12.15 和哈希锁定依赖；不修改全局 Python。
3. 运行专用 Windows 后台入口。macOS 旧测试不在 Windows 上强行发现或标记 skip；两套结果分开报告。
4. 原生检查成功且记录的源码 hash 与当前包一致，才允许构建；冻结 exe 再做一次 100 文件后台往返和内置模块来源检查。
5. 审查白名单、PE 架构、许可材料、禁止文件和本机构建路径，再生成 ZIP、脱敏构建清单与 SHA256SUMS。只上传这三类文件，保留 7 天；测试日志、状态、文件清单和截图不上传。

`windows-2025` 是 Windows Server runner。仅在人工沙盒中允许接口回归，不据此宣称 Windows 11 已验收。网络下载后的安全提示、真实窗口、用户缩放和另一台机器均需另测。

## 在 Windows 本机构建

需要已有的 Python 3.12 或更新版本作为准备入口。以下命令会下载固定构建依赖并只安装进指定新目录；先阅读脚本，不覆盖已有候选。

```powershell
python -B scripts/prepare_windows_build.py --output-dir build/windows-env
& build/windows-env/venv/Scripts/python.exe -B scripts/test_windows_background.py --receipt build/windows-checks.json
& build/windows-env/venv/Scripts/python.exe -B scripts/build_windows_distributable.py --output-dir build/windows-candidate --test-receipt build/windows-checks.json
```

失败材料保留，重跑用新的目录／receipt 文件名。测试入口通过 `scripts/windows_sandbox.py` 新建私有人工根，只有当前用户／SYSTEM／管理员可写，不修改已有目录或系统权限。原始诊断保留在此次临时人工沙盒的 `records` 中，不提交或上传。构建记录中的 `source_sha256` 仅含源码，不是用户文件清单。

## 验收清单

| 层次 | 可观察结果 |
|---|---|
| macOS 后台回归 | 原有 520 项与新增策略测试通过，失败／错误／跳过为 0；GUI 未运行单列 |
| Windows 原生后台 | 100 个假文件覆盖七类及嵌套目录；预览不创建目标、不改变源；移动 100、撤销 100，字节和身份全部回位，两日志 healthy |
| 冲突和权限 | 同名竞态、大小写冲突保留双方；源替换拒绝；文件占用不强行关闭；祖先替换被持有句柄阻止；junction 不跟随 |
| 日志和恢复 | 日志写入失败不继续移动；进程在移动前／移动后退出，恢复后位置可判；原位被占用时撤销不覆盖 |
| 生命周期 | 操作锁互斥、应用单实例锁独立；释放后可重获。跨进程崩溃释放由中断恢复测试覆盖操作锁 |
| 冻结后台 | ZIP 中 exe 以空白人工根完成 100 文件往返；应用模块和 Tk 扩展来自包内，不弹窗 |
| 必须单列的未证明项 | Windows 11 原生窗口、WebView2/Tk 实际交互、不同缩放、干净机器、网络下载标记、Defender/SmartScreen、真实双卷测试、断电行为 |

跨卷检查有单元测试验证拒绝发生在源打开前；原生真正双卷场景不伪称通过。后续须在明确授权的两个独立人工卷目录验证，不借用真实用户文件。

## 官方接口依据

- [FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info)
- [NtCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)、[OBJECT_ATTRIBUTES](https://learn.microsoft.com/en-us/windows/win32/api/ntdef/ns-ntdef-_object_attributes)
- [FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)、[LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)
- [WebView2 分发与检测](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)
- [PyInstaller 平台限制](https://pyinstaller.org/en/stable/)
