"""离线模板：一份"新手风格"的订单计价模块。

它本身就是可运行的代码——不是伪代码。缺陷是真实新手代码里最常见的那几类：
裸 except、可变默认参数、``== None``、没用到的 import、吞异常、没有 docstring。

工作流的演示要能证明"发现问题 → 修复 → 复检数量下降"，所以这份模板必须
既**能跑**，又**确实有可判定、可修复的问题**。
"""

from __future__ import annotations

import json
import os


def calc_total(items, discounts=[]):
    total = 0
    for item in items:
        price = item["price"] * item["qty"]
        if item.get("category") == None:
            price = price * 1.0
        total = total + price
    for rule in discounts:
        if rule["type"] == "percent":
            total = total * (1 - rule["value"] / 100)
        elif rule["type"] == "minus":
            total = total - rule["value"]
    return round(total, 2)


def load_items(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except:
        return []


def save_report(total, path):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(total))
    except Exception:
        pass


def group_by_category(items):
    groups = {}
    for item in items:
        category = item.get("category")
        if category is None:
            category = "unknown"
        if category not in groups:
            groups[category] = []
        groups[category].append(item)
    return groups


if __name__ == "__main__":
    sample = [
        {"name": "键盘", "price": 199.0, "qty": 1, "category": "外设"},
        {"name": "鼠标", "price": 89.0, "qty": 2, "category": None},
    ]
    print(calc_total(sample, [{"type": "percent", "value": 10}]))
