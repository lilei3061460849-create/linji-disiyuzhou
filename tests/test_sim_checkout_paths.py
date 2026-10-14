"""A23 的技术前置：模拟脚本可从重命名/搬动后的 checkout 定位根目录。"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["b19_consumable_shadow.py", "b19_duel_lab.py", "elite_diffusion_experiment.py"])
def test_script_root_is_derived_from_its_file_not_old_checkout(name, tmp_path, monkeypatch):
    source = (ROOT / "sim" / name).read_text()
    tree = ast.parse(source)
    root_expression = next(node.value.args[1] for node in tree.body
                           if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                           and isinstance(node.value.func, ast.Attribute)
                           and node.value.func.attr == "insert")
    renamed_root = tmp_path / "renamed checkout"
    script = renamed_root / "sim" / name
    script.parent.mkdir(parents=True)
    script.touch()
    monkeypatch.chdir(tmp_path)
    # 只执行真实定位表达式，不 import 会修改学习器全局设置的实验模块。
    expression = ast.Expression(root_expression)
    result = eval(compile(expression, str(script), "eval"), {"Path": Path, "__file__": str(script)})
    assert Path(result) == renamed_root.resolve()
    assert '"/home/user/linji-disiyuzhou"' not in source
