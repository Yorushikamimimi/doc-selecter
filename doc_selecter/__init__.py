"""下载文件夹分类助手（doc-selecter）。

最小闭环：生成预览 → 用户确认 → 执行移动 → 可撤销。

安全要旨（详见 docs/MVP方案.md v3.1）：
- 移动只经 Darwin ``renamex_np`` / ``renameatx_np`` + ``RENAME_EXCL``，原子不覆盖；
- 所有真实运行必须显式注入沙盒路径（``DOCSELECTER_TEST_ROOT`` 或 config.json），
  缺少配置即拒绝运行，绝不回退到真实 Downloads。
"""

__version__ = "0.1.0"
RULES_VERSION = "milestone5-empty-dir-archive-r1"

__all__ = ["__version__", "RULES_VERSION"]
