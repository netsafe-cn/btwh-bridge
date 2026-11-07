# -*- coding: utf-8 -*-
"""run_tests.py —— 回归测试入口

一条命令跑全部单元测试（全离线，不碰硬件、不连网络）：
    python run_tests.py           # 简洁汇总
    python run_tests.py -v        # 逐条列出

约定：
  * 所有 test_*.py 自动纳入回归，新增测试无需改本文件；
  * 连真实硬件的探针脚本不得用 test_ 前缀（本项目为 probe_login_matrix.py）；
  * 测试必须能在无 WinRT / 无蓝牙的 CI（Linux / macOS）上跑通或显式 skip。

任何改动（代码 / 文档 / 配置）之后都应先跑这个，全绿再提交。
"""
import pathlib
import sys
import unittest


HERE = pathlib.Path(__file__).resolve().parent


def _walk(suite):
    """展开嵌套 TestSuite，取出最底层用例。"""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _walk(item)
        else:
            yield item


def main():
    verbosity = 2 if "-v" in sys.argv else 1
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(HERE), pattern="test_*.py",
                            top_level_dir=str(HERE))

    # 空跑保护：目录搬错 / pattern 写错时不能"假通过"
    if suite.countTestCases() == 0:
        print("未发现任何测试用例，请检查目录与 pattern")
        return 1

    # 分模块统计，CI 里一眼看出哪个模块挂了
    mods = {}
    for case in _walk(suite):
        m = case.__class__.__module__
        mods[m] = mods.get(m, 0) + 1
    print("纳入回归：%s" %
          ", ".join("%s(%d)" % (k, v) for k, v in sorted(mods.items())))
    print()

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    total = result.testsRun
    skipped = len(result.skipped)
    bad = len(result.failures) + len(result.errors)
    print()
    print("=" * 56)
    if bad == 0:
        print("回归通过：%d 项全部成功（跳过 %d 项平台相关）" % (total, skipped))
    else:
        print("回归失败：%d 项中 %d 项异常" % (total, bad))
        for case, _ in result.failures + result.errors:
            print("  - %s" % case)
    print("=" * 56)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
