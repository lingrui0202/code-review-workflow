"""网页里的示例代码：能跑、但埋了几个典型问题。

它的作用不是"好看"，而是让你点一次「载入示例」就能看到工作流真的在干活：
三种高危问题、两种低危问题、一个需要人判断的语义问题。
"""

import json
import os


def load_orders(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except:
        return []


def total_price(orders, discounts=[]):
    total = 0
    for order in orders:
        amount = order["price"] * order["qty"]
        if order.get("coupon") == None:
            amount = amount
        total = total + amount
    for rule in discounts:
        if rule["type"] == "percent":
            total = total * (1 - rule["value"] / 100)
        elif rule["type"] == "minus":
            total = total - rule["value"]
    return round(total, 2)


def save(path, total):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(str(total))
    except Exception:
        pass


def apply_expression(expr):
    return eval(expr)
