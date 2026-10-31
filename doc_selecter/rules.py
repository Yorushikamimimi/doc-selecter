"""分类规则与统一私密名称筛查（正式基线与已有额外保护的并集）。

只依据条目的**名称、扩展名、条目类型**判定，绝不读取文件正文。

判定优先级（自上而下，命中即停，保证每个条目恰好进入一个结果）::

    疑似敏感 → 元数据读取失败 → 符号链接 → 隐藏项 → 文件夹
      → 未完成下载或临时项 → 其他特殊类型
      → 安装包 → 文档资料 → 图片截图 → 音视频 → 压缩包 → 代码与数据 → 待确认
"""

from __future__ import annotations

import os
import re
import stat as statmod
from urllib.parse import unquote
from dataclasses import dataclass

# --- 七类（顺序固定，参与 plan_hash 的 category_selection 排序） ---------------
CATEGORIES: tuple[str, ...] = (
    "安装包",
    "文档资料",
    "图片截图",
    "音视频",
    "压缩包",
    "代码与数据",
    "待确认",
)
UNCLASSIFIED = "待确认"

# --- 扩展名规则表（固定裁定见 docs/MVP方案.md §3） ----------------------------
# 安装用途明确的封装格式归「安装包」
INSTALLER_EXTS = frozenset(
    """
    .dmg .pkg .mpkg .exe .msi .msix .msixbundle .appx .appxbundle
    .deb .rpm .apk .apks .xapk .whl .gem .nupkg .vsix .crx .xpi
    """.split()
)
DOCUMENT_EXTS = frozenset(
    """
    .pdf .doc .docx .docm .odt .rtf .txt .md .markdown .ppt .pptx .pptm .odp
    .xls .xlsx .xlsm .ods .epub .mobi .azw .azw3 .pages .numbers .keynote .tex .rst
    """.split()
)

# 仅用于本次所选来源顶层的零散文档；递归目录中的文档保持原相对层级。
DOCUMENT_SUBCATEGORY_EXTS: dict[str, frozenset[str]] = {
    "Excel": frozenset({".xls", ".xlsx", ".xlsm"}),
    "PDF": frozenset({".pdf"}),
    "Word": frozenset({".doc", ".docx", ".docm"}),
    "PPT": frozenset({".ppt", ".pptx", ".pptm"}),
    "Markdown": frozenset({".md", ".markdown"}),
}
DOCUMENT_SUBCATEGORIES: tuple[str, ...] = (
    "Excel", "PDF", "Word", "PPT", "Markdown", "其他文档",
)
IMAGE_EXTS = frozenset(
    """
    .jpg .jpeg .png .gif .webp .heic .heif .bmp .tif .tiff .svg .ico .avif
    .psd .ai .eps .raw .dng .cr2 .nef .arw
    """.split()
)
MEDIA_EXTS = frozenset(
    """
    .mp3 .wav .flac .aac .m4a .ogg .opus .aiff .alac .wma
    .mp4 .mkv .mov .avi .webm .m4v .wmv .flv .mpeg .mpg .m2ts .mts .3gp
    """.split()
)
ARCHIVE_EXTS = frozenset(
    """
    .tar.gz .tar.bz2 .tar.xz .tar.zst .tar.lz .tar.lzma .tar
    .zip .7z .rar .gz .bz2 .xz .zst .tgz .tbz .tbz2 .txz .lz .lzma .cab
    """.split()
)
CODE_DATA_EXTS = frozenset(
    """
    .py .js .mjs .cjs .jsx .tsx .java .c .h .cpp .hpp .cs .go .rs .rb .php
    .swift .kt .kts .sh .bash .zsh .ps1 .bat .cmd .ipynb
    .json .jsonl .ndjson .yaml .yml .toml .xml .csv .tsv
    .sql .sqlite .sqlite3 .db .parquet .arrow .feather .npy .npz
    .r .rdata .log .html .htm .css .scss .less .vue .svelte
    .lock .ini .cfg .conf .properties
    """.split()
)

# 已知歧义扩展名：固定归「待确认」，不猜用途
AMBIGUOUS_EXTS = frozenset({".ts", ".jar"})

EXTENSION_RULES: dict[str, str] = {}
for _exts, _cat in (
    (INSTALLER_EXTS, "安装包"),
    (DOCUMENT_EXTS, "文档资料"),
    (IMAGE_EXTS, "图片截图"),
    (MEDIA_EXTS, "音视频"),
    (ARCHIVE_EXTS, "压缩包"),
    (CODE_DATA_EXTS, "代码与数据"),
):
    for _e in _exts:
        if _e in EXTENSION_RULES:
            raise AssertionError(f"扩展名规则重复: {_e}")
        EXTENSION_RULES[_e] = _cat
for _e in AMBIGUOUS_EXTS:
    EXTENSION_RULES[_e] = UNCLASSIFIED

# --- 排除规则 -----------------------------------------------------------------
SENSITIVE_SUFFIXES = frozenset(
    """
    .pem .key .p12 .pfx .keystore .jks .ppk .asc .gpg .kdbx .keychain
    .mobileconfig .mobileprovision .ovpn .kdb .age .netrc .1password .cer .crt
    """.split()
)
SENSITIVE_TERMS = (
    "password", "passwd", "passphrase", "credential", "secret", "token",
    "apikey", "api-key", "api_key", "privatekey", "private-key",
    "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa",
    "seedphrase", "seed-phrase", "mnemonic", "wallet", "backupcode",
    "recoverycode", "htpasswd", "auth.json", "cookies", "cookie",
    "private", "confidential", "authorized_keys", "known_hosts", "keychain",
    "recovery code", "recovery-code", "recovery_code", "backup code", "backup-code",
    "seed phrase", "resume", "curriculum vitae", "passport", "identity card",
    "medical", "payroll", "salary", "bankstatement",
    "简历", "履历", "身份证", "护照", "户口", "病历", "体检", "医疗",
    "工资", "薪资", "薪酬", "银行卡", "银行流水", "账单", "社保", "公积金",
    "个税", "纳税", "征信", "签名", "签章", "合同", "协议", "保密", "私密",
    "隐私", "凭证", "密钥", "秘钥", "密码", "口令", "令牌", "助记词", "恢复码",
)

# 附加模式是本轮明确的新边界，历史元数据只记录其语义。
_PRIVATE_WORD = re.compile(
    r"(?<![a-z0-9])(?:cv|ssn|apikey|api_key|access_key|auth|oauth|personal)(?![a-z0-9])",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.IGNORECASE)
# 连续 11 位数字（手机号样式）、18 位身份证样式、连续至少 12 位数字。
# 无需判断号码是否真实，也不校验真实身份；只作文件名保守筛查。
_PRIVATE_NUMBER = re.compile(r"(?<![0-9])(?:[0-9]{11}|[0-9]{17}[0-9xX]|[0-9]{12,})(?![0-9])")
# 微信保存的图片名带的是保存时间（14 位日期时间／13 位毫秒时间戳），不是号码。
# 新规则只对这两个精确前缀免长数字检查；其余敏感词、邮箱等检查照旧。
_WECHAT_TIMESTAMP = re.compile(r"^(?:微信图片_20[0-9]{12}|mmexport1[0-9]{12})(?![0-9])")
UNFINISHED_SUFFIXES = frozenset(
    ".crdownload .part .partial .download .filepart .opdownload .tmp".split()
)
SYSTEM_METADATA_NAMES = frozenset(
    {".DS_Store", ".localized", ".Spotlight-V100", ".fseventsd"}
)

# --- 递归整理：目录级剔除规则 ------------------------------------------------
#: 包目录后缀（macOS 包/插件包等，整体跳过，绝不进入）
PACKAGE_DIR_SUFFIXES = frozenset(
    ".app .framework .bundle .appex .xpc .kext .plugin .prefpane .qlgenerator "
    ".mdimporter .saver .slideSaver .wdgt .pkg .mpkg .rtfd .imovielibrary".split()
)

#: 代码项目标记（**只看目录内是否存在这些名字**，绝不读取其内容）
CODE_PROJECT_MARKERS = frozenset(
    {
        ".git", ".hg", ".svn", "package.json", "pyproject.toml", "setup.py",
        "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
        "CMakeLists.txt", "Makefile", "composer.json", "Gemfile", "Podfile",
        "Package.swift", "*.xcodeproj", "*.xcworkspace", "requirements.txt",
        "tsconfig.json", "Dockerfile", "docker-compose.yml",
    }
)

#: 代码项目文件后缀（同级已出现两个及以上时判定为项目目录）
_CODE_PROJECT_SUFFIXES = (".xcodeproj", ".xcworkspace", ".sln", ".csproj")

#: 疑似敏感**目录**名（保守筛查；命中即整目录跳过且只计数量、不记录名称）
SENSITIVE_DIR_TERMS = (
    "password", "passwd", "credential", "secret", "token", "apikey", "api-key",
    "api_key", "private", "id_rsa", "id_ed25519", "id_dsa", "id_ecdsa",
    "seedphrase", "seed-phrase", "mnemonic", "wallet", "backupcode",
    "recoverycode", "htpasswd", "cookies", "keychain", "keystore", "ssh",
    "gpg", "vault", "银行卡", "身份证", "证件", "密钥", "密码", "私密",
)

#: 临时/未完成下载后缀（递归与其他扫描都要排除；自动发现也用它判断「未稳定」）
TEMP_DOWNLOAD_SUFFIXES = frozenset(
    ".crdownload .part .partial .download .filepart .opdownload .tmp .temp "
    ".aria2 .!ut .bc! .xltd .hevc.part".split()
)

# --- 判定结果 -----------------------------------------------------------------
EXCLUDED_SENSITIVE = "EXCLUDED_SENSITIVE"
EXCLUDED_HIDDEN = "EXCLUDED_HIDDEN"
EXCLUDED_DIR = "EXCLUDED_DIR"
EXCLUDED_SYMLINK = "EXCLUDED_SYMLINK"
EXCLUDED_UNFINISHED = "EXCLUDED_UNFINISHED"
EXCLUDED_OTHER = "EXCLUDED_OTHER"
#: 递归时新增的目录级剔除原因
EXCLUDED_LEAF_DIR = "EXCLUDED_LEAF_DIR"
EXCLUDED_PACKAGE_DIR = "EXCLUDED_PACKAGE_DIR"
EXCLUDED_CODE_PROJECT = "EXCLUDED_CODE_PROJECT"
EXCLUDED_SENSITIVE_DIR = "EXCLUDED_SENSITIVE_DIR"

#: 排除原因的互斥优先级（与基线一致；目录级规则在递归进入**之前**生效）
EXCLUSION_PRIORITY: tuple[str, ...] = (
    EXCLUDED_SENSITIVE,
    EXCLUDED_SENSITIVE_DIR,
    EXCLUDED_SYMLINK,
    EXCLUDED_HIDDEN,
    EXCLUDED_LEAF_DIR,
    EXCLUDED_PACKAGE_DIR,
    EXCLUDED_CODE_PROJECT,
    EXCLUDED_DIR,
    EXCLUDED_UNFINISHED,
    EXCLUDED_OTHER,
)

EXCLUSION_LABELS = {
    EXCLUDED_SENSITIVE: "疑似敏感项",
    EXCLUDED_SENSITIVE_DIR: "疑似敏感目录（整体跳过）",
    EXCLUDED_SYMLINK: "符号链接",
    EXCLUDED_HIDDEN: "隐藏项",
    EXCLUDED_LEAF_DIR: "空文件夹（无文件，整体跳过）",
    EXCLUDED_PACKAGE_DIR: "包目录（整体跳过）",
    EXCLUDED_CODE_PROJECT: "代码项目目录（整体跳过）",
    EXCLUDED_DIR: "文件夹",
    EXCLUDED_UNFINISHED: "未完成下载或临时项",
    EXCLUDED_OTHER: "其他特殊类型",
}

#: 递归扫描中「不进入」的排除原因（命中的目录整体跳过，不读取其内部）
PRUNE_CODES = frozenset(
    {
        EXCLUDED_SENSITIVE_DIR,
        EXCLUDED_HIDDEN,
        EXCLUDED_PACKAGE_DIR,
        EXCLUDED_CODE_PROJECT,
        EXCLUDED_LEAF_DIR,
    }
)


def is_package_dir(name: str) -> bool:
    """包目录（``.app``、``.framework`` 等）→ 整体跳过，绝不进入。"""
    low = name.lower()
    return any(low.endswith(suffix) for suffix in PACKAGE_DIR_SUFFIXES)


def is_code_project_dir(names: frozenset[str]) -> tuple[bool, str]:
    """按**目录内条目名**判断是否为代码项目目录（不读取任何文件内容）。

    ``names`` 是调用方用 ``scandir`` 得到的**直接子项名字集合**。
    返回 ``(是否项目目录, 命中的标记说明)``。
    """
    hits = sorted(n for n in names if n in CODE_PROJECT_MARKERS)
    if hits:
        return True, "命中标记：" + ", ".join(hits[:4])
    suffix_hits = sorted(
        n for n in names if any(n.lower().endswith(sfx) for sfx in _CODE_PROJECT_SUFFIXES)
    )
    if suffix_hits:
        return True, "命中标记：" + ", ".join(suffix_hits[:4])
    return False, ""


def is_sensitive_dir(name: str, *, legacy: bool = False) -> bool:
    """疑似敏感**目录**名（保守筛查，只看名字）。

    与文件规则一致：命中即整目录跳过，且只在内存里计数，不落盘、不展示名称。
    """
    low = name.lower()
    if not legacy and decoded_filename(name) != name and is_sensitive_dir(decoded_filename(name), legacy=True):
        return True
    if is_sensitive_name(name, legacy=legacy):
        return True
    if low in (".ssh", ".gnupg", ".aws", ".kube"):
        return True
    return any(term in low for term in SENSITIVE_DIR_TERMS)


def is_temp_download(name: str) -> bool:
    """临时/未完成下载后缀（自动发现判「尚未稳定」也用它）。"""
    return normalize_extension(name) in TEMP_DOWNLOAD_SUFFIXES or name.endswith(".crdownload")


def is_leaf_dir(names: frozenset[str]) -> bool:
    """目录内**没有任何普通文件**（可能为空或只有子目录）→ 整体跳过。

    判定只用名字与类型，不读取内容；空目录不会被移动也不会被删除。
    """
    return not names


@dataclass(frozen=True)
class Classification:
    """单个顶层条目的判定结果。"""

    name: str
    status: str  # "included" | "excluded"
    category: str | None
    extension: str
    basis: str
    exclusion_code: str | None = None

    @property
    def included(self) -> bool:
        return self.status == "included"


def normalize_extension(name: str, is_dir: bool = False) -> str:
    """返回用于规则匹配的小写扩展名（可为多段，如 ``.tar.gz``；无扩展名返回 ""）。

    对目录返回 ""（目录一律排除，不参与扩展名分类）。
    """
    if is_dir:
        return ""
    base = os.path.basename(name)
    # 真正无扩展名（或以点开头的隐藏名）视为 ""
    if base.startswith("."):
        return ""
    idx = base.rfind(".")
    if idx <= 0:
        return ""
    return base[idx:].lower()


def match_extension(ext: str) -> tuple[str | None, str]:
    """按「最长扩展名优先」匹配规则表。

    返回 ``(category, matched_ext)``；未命中返回 ``(None, "")``。
    """
    if not ext:
        return None, ""
    candidate = ext
    while candidate:
        if candidate in EXTENSION_RULES:
            return EXTENSION_RULES[candidate], candidate
        idx = candidate.find(".", 1)
        if idx == -1:
            break
        candidate = candidate[idx:]
    return None, ""


def document_subcategory(ext: str) -> str | None:
    """返回文档子类；非文档扩展名返回 ``None``。"""
    ext = (ext or "").lower()
    category, _matched = match_extension(ext)
    if category != "文档资料":
        return None
    for name, extensions in DOCUMENT_SUBCATEGORY_EXTS.items():
        if ext in extensions:
            return name
    return "其他文档"


# Filename-only refinements. Keep the historic document format helper unchanged.
PHONE_NAME = re.compile(r"IMG_E?[0-9]+(?: \([0-9]+\))?$", re.IGNORECASE)
COMPUTER_SCREENSHOT = re.compile(
    r"^(?:截屏|屏幕快照|Screenshot|Screen Shot)\s*[0-9]{4}[-年.][0-9]{1,2}[-月.][0-9]{1,2}(?:日|\b|[_ ])", re.IGNORECASE)
COURSE_TERMS = ("大作业", "实验报告", "课程设计", "作业", "课设", "课程", "期末", "期中",
                "复习", "习题", "试卷", "考试", "课件", "讲义", "教案", "开题", "答辩", "论文", "选课", "课表")
AUDIO_EXTS = frozenset(".mp3 .wav .flac .aac .m4a .ogg .opus .aiff .alac .wma".split())
CATEGORY_SUBFOLDERS = {
    "图片截图": ("手机照片", "手机截屏", "电脑截屏", "微信图片", "其他图片"),
    "音视频": ("手机视频", "视频", "音频"),
    "文档资料": ("学校课程", "PDF", "Word", "Excel", "PPT", "Markdown", "其他文档"),
    "安装包": ("Mac 安装包（dmg pkg）", "Windows 安装包（exe msi）", "手机安装包（apk）", "其他安装包"),
    "压缩包": ("ZIP", "RAR 和 7Z", "其他压缩包"),
    "代码与数据": (), "待确认": (),
}


def decoded_filename(name: str) -> str:
    """Display/keyword candidate only; never use it as a filesystem name."""
    if not re.search(r"%[0-9a-fA-F]{2}", name):
        return name
    try:
        return unquote(name, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return name


def file_subfolder(name: str, category: str) -> str | None:
    """Caller limits this filename rule to top-level files."""
    ext = normalize_extension(name)
    stem = os.path.splitext(name)[0]
    phone = bool(PHONE_NAME.fullmatch(stem))
    if category == "图片截图":
        if phone and ext in {".heic", ".heif", ".jpg", ".jpeg", ".dng"}: return "手机照片"
        if phone and ext == ".png": return "手机截屏"
        if COMPUTER_SCREENSHOT.match(stem): return "电脑截屏"
        if stem.lower().startswith(("微信图片_", "mmexport")): return "微信图片"
        return "其他图片"
    if category == "音视频":
        if phone and ext in {".mov", ".mp4"}: return "手机视频"
        return "音频" if ext in AUDIO_EXTS else "视频"
    if category == "文档资料":
        if any(term in os.path.splitext(decoded_filename(name))[0] or term in stem for term in COURSE_TERMS):
            return "学校课程"
        return document_subcategory(ext)
    if category == "安装包":
        if ext in {".dmg", ".pkg", ".mpkg"}: return "Mac 安装包（dmg pkg）"
        if ext in {".exe", ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle"}: return "Windows 安装包（exe msi）"
        if ext in {".apk", ".apks", ".xapk"}: return "手机安装包（apk）"
        return "其他安装包"
    if category == "压缩包":
        return "ZIP" if ext == ".zip" else "RAR 和 7Z" if ext in {".rar", ".7z"} else "其他压缩包"
    return None


def classification_basis(name: str, category: str, ext: str) -> str:
    sub = file_subfolder(name, category)
    if sub == "学校课程":
        term = next(t for t in COURSE_TERMS if t in os.path.splitext(name)[0] or t in os.path.splitext(decoded_filename(name))[0])
        return f"名字里有「{term}」→ 学校课程"
    if sub in {"手机照片", "手机截屏", "手机视频"}:
        return f"名字是 IMG_ + 数字，格式 {ext.lstrip('.').upper()} → {sub}"
    if sub == "电脑截屏": return "名字是截屏前缀加日期 → 电脑截屏"
    if sub == "微信图片": return "名字以 微信图片_ 或 mmexport 开头 → 微信图片"
    return f"扩展名 {ext} → {sub or category}"


def is_sensitive_name(name: str, *, legacy: bool = False) -> bool:
    """保守的敏感性名称筛查（只看名称/扩展名，绝不读取内容）。"""
    decoded = decoded_filename(name) if not legacy else name
    if decoded != name and is_sensitive_name(decoded, legacy=True):
        return True
    low = name.lower()
    if any(low.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
        return True
    if re.search(r"(?:^|\.)env(?:$|[._-])", low):
        return True
    return bool(any(term in low for term in SENSITIVE_TERMS)
                or _PRIVATE_WORD.search(name) or _EMAIL.search(name)
                or _PRIVATE_NUMBER.search(name if legacy else _WECHAT_TIMESTAMP.sub("", name, count=1)))


def is_sensitive_relative_path(rel_path: str, *, legacy: bool = False) -> bool:
    """只筛查相对文件名和父目录段；不检查下载根之外的系统路径。"""
    return any(is_sensitive_dir(part, legacy=legacy) for part in rel_path.split("/") if part)


def redact_private_text(text: str, *, roots: tuple[str, ...] = ()) -> str:
    """用户可见文本的保守遮蔽，不改历史计划、日志或身份基准。

    根路径只作信任锚点，保护 /private/tmp 等系统前缀不被当成文件名。
    命中时隐藏整行，避免带空格的文件名被切碎后漏出。
    """
    lines = []
    for line in str(text).split("\n"):
        probe = line
        for root in sorted(roots, key=len, reverse=True):
            if root:
                # 只豁免完整锚点；同名前缀的兄弟路径不属于可信根。
                probe = re.sub(re.escape(root) + r"(?=$|[/\s'\"`<>{}\[\](),:;!?])",
                               "[ROOT]", probe)
        # OS 错误常把路径包在引号/括号中；不能让这些标点掩盖 .cer/.env 等后缀。
        parts = re.split(r"[/\s'\"`<>{}\[\](),:;!?]+", probe)
        private = is_sensitive_name(probe) or any(is_sensitive_dir(p) for p in parts)
        lines.append("（疑似敏感信息已隐藏）" if private else line)
    return "\n".join(lines)


def is_unfinished(name: str) -> bool:
    if name in SYSTEM_METADATA_NAMES:
        return True
    return normalize_extension(name) in UNFINISHED_SUFFIXES


def classify_entry(name: str, st_mode: int, *, excluded_names: frozenset[str] = frozenset(), legacy: bool = False) -> Classification:
    """按互斥优先级判定一个顶层条目。

    ``st_mode`` 来自 ``lstat``（不跟随符号链接）。
    """
    is_link = statmod.S_ISLNK(st_mode)
    is_dir = statmod.S_ISDIR(st_mode)
    is_regular = statmod.S_ISREG(st_mode)
    is_hidden = name.startswith(".")
    ext = normalize_extension(name, is_dir=is_dir or is_link)

    def excluded(code: str, basis: str) -> Classification:
        return Classification(
            name=name, status="excluded", category=None, extension=ext,
            basis=basis, exclusion_code=code,
        )

    # 1) 疑似敏感（最高优先级；只记数量，不保留名称）
    if is_sensitive_name(name, legacy=legacy):
        return excluded(EXCLUDED_SENSITIVE, "名称或扩展名命中疑似敏感规则（保守筛查，未读取内容）")
    # 2) 符号链接
    if is_link:
        return excluded(EXCLUDED_SYMLINK, "符号链接：不跟随、不移动")
    # 3) 隐藏项
    if is_hidden:
        return excluded(EXCLUDED_HIDDEN, "隐藏项（名称以 . 开头）：不纳入分类")
    # 4) 文件夹
    if is_dir:
        return excluded(EXCLUDED_DIR, "文件夹：仅计数，不递归、不统计内部大小")
    # 5) 未完成下载或临时项
    if is_unfinished(name):
        return excluded(EXCLUDED_UNFINISHED, "疑似临时/未完成项：不纳入分类")
    # 6) 其他特殊类型
    if not is_regular:
        return excluded(EXCLUDED_OTHER, "特殊文件类型（非普通文件）：不纳入分类")

    # 7) 七类归档
    category, matched = match_extension(ext)
    if category is None:
        return Classification(
            name=name, status="included", category=UNCLASSIFIED, extension=ext,
            basis="扩展名不在已知规则内，归入「待确认」",
        )
    return Classification(
        name=name, status="included", category=category, extension=ext,
        basis=(f"扩展名 {matched} 命中「{category}」规则" if legacy else classification_basis(name, category, matched)),
    )


def is_known_category(name: str) -> bool:
    return name in CATEGORIES


def rules_table() -> dict[str, list[str]]:
    """返回 ``{类别: [扩展名...]}``（用于报告与自检）。"""
    table: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for ext, cat in EXTENSION_RULES.items():
        table[cat].append(ext)
    for cat in table:
        table[cat].sort()
    return table
