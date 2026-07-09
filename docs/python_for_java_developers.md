# 代码阅读笔记

这个项目整体可以按“门面对象 + pandas 表格数据”来理解。
如果你主要写 Java，下面这些对应关系会更好读：

- `adata.stock`、`adata.stock.market`、`adata.stock.info` 是模块级门面实例，类似已经装配好的 service/facade 对象。
- `pandas.DataFrame` 是项目里最常见的表格返回类型，可以理解成内存里的二维表，接近 `ResultSet` 或 `List<Map<String, Object>>`。
- `df.empty` 表示 DataFrame 没有数据行，很多方法会用它判断是否需要切换到备用数据源。
- `with open(...) as f` 是 Python 的资源管理写法，作用接近 Java 的 `try-with-resources`。
- `SomeClass(**kwargs)` 会把字典展开成命名参数传给构造方法，类似从一个 Map 中按字段名给对象赋值。
- 列表、字典、集合推导式是 Python 的紧凑循环写法，理解上接近 Java Stream 的 `map/filter/collect`。

建议先从这些文件看起：

- `adata/__init__.py`：包级入口函数，例如 `version()` 和 `proxy(...)`。
- `adata/stock/__init__.py`：股票模块门面。
- `adata/stock/market/stock_market/stock_market.py`：股票行情门面，以及数据源兜底逻辑。
- `adata/stock/info/stock_code.py`：全市场股票代码加载和 pandas 清洗流程。
- `adata/common/utils/sunrequests.py`：统一 HTTP 请求封装，包含重试、超时和代理处理。
