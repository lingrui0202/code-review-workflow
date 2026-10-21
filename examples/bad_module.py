"""一个"看起来能跑、其实有坑"的模块，用于 ``--code`` 演示。

它跟内置模板不是同一份代码：这份更接近真实业务代码里的问题——
吞掉异常后返回 None、用 eval 处理表达式、字典当默认参数、魔法数字。
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

CACHE = {}


def parse_amount(text):
    pattern = re.compile(r"-?\d+(?:\.\d+)?")
    match = pattern.search(text or "")
    if match:
        return float(match.group())
    return 0.0


def apply_rules(amount, rules=[]):
    for rule in rules:
        kind = rule.get("kind")
        if kind == "rate":
            amount = amount * (1 - rule["value"])
        elif kind == "cap":
            if amount > rule["value"]:
                amount = rule["value"]
        else:
            if kind == None:
                continue
    return amount


def read_config(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None


def evaluate(expr):
    try:
        return eval(expr)
    except:
        return 0


def build_key(name, salt="", prefix="ord"):
    return f"{prefix}:{name}:{salt}:{os.getpid()}"


def retry(times, fn):
    last = None
    for index in range(times):
        try:
            return fn()
        except Exception as exc:
            last = exc
            logger.debug("第 %s 次重试失败：%s", index, exc)
    raise last
