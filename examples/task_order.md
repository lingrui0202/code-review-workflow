# 需求：订单计价模块

写一个可直接运行的 Python 模块 `order_price.py`，提供三个能力：

1. `calc_total(items, discounts=None)`：按 `price * qty` 累加各条目金额；支持两类折扣规则
   （`{"type": "percent", "value": 10}` 表示打九折，`{"type": "minus", "value": 20}` 表示立减 20），
   结果四舍五入到两位小数。`category` 缺失时按默认分类处理。
2. `load_items(path)`：从 JSON 文件读入商品列表；文件不存在或格式不对时返回空列表。
3. `save_report(total, path)`：把总价写入文本文件。

约束：只用标准库；不要写死任何密钥；公开函数要有 docstring。
