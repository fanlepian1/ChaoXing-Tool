"""守护测试: 注解里引用的名字必须真的存在。

Python 3.14 起注解默认惰性求值, 漏 import 也不会当场报错; 3.13 及更早会在定义时求值并直接 NameError
(CI 就是因为这个挂的: api/answer.py 用了 Any 却没导入)。这里对模块里所有函数/方法/类主动求一次注解,
让这类问题在本地、在任何 Python 版本上都能暴露。
"""
import importlib
import sys
import typing
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

MODULES = [
    "api.answer",
    "api.answer_check",
    "api.base",
    "api.exceptions",
    "api.logger",
    "api.tiku_bank",
    "main",
]


def _iter_targets(module, module_name):
    for attr_name, obj in list(vars(module).items()):
        if getattr(obj, "__module__", None) != module_name:
            continue
        yield attr_name, obj
        if isinstance(obj, type):
            for member_name, member in list(vars(obj).items()):
                if not member_name.startswith("__"):
                    yield f"{attr_name}.{member_name}", member


class TestAnnotations(unittest.TestCase):
    def test_every_module_imports(self):
        for name in MODULES:
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_annotations_resolve(self):
        """注解里的每个名字都要能求值(漏 import / 拼错类型名都会在这里炸)。"""
        for name in MODULES:
            module = importlib.import_module(name)
            for qualname, target in _iter_targets(module, name):
                if not callable(target):
                    continue
                with self.subTest(target=f"{name}.{qualname}"):
                    try:
                        typing.get_type_hints(target)
                    except Exception as e:          # noqa: BLE001 - 这里就是要抓一切异常
                        self.fail(f"{name}.{qualname} 的注解无法求值: {e!r}")


if __name__ == "__main__":
    unittest.main()
