# BsAgent MCP Server

## 目标

该目录提供独立运行的真实 MCP Streamable HTTP Server。Agent 在启动期通过 `list_tools` 动态发现工具，运行期经权限、风险、参数 Schema、超时与错误归一化后调用，不使用 HTTP mock 接口。

当前工具：

- `echo`
- `stock.resolve_company`
- `stock.company_profile`
- `stock.market_snapshot`
- `stock.price_trend`
- `stock.financial_metrics`
- `stock.announcement_search`

MCP 是工具能力提供方，不是顶层 intent。Intent Router 判断用户目标，Planner 根据动态工具 Schema 与 Skill 证据契约决定是否调用 MCP。

## 启动

本地独立启动：

```bash
cd "mcp server"
/opt/anaconda3/bin/python3 -m app.main
```

默认 MCP Endpoint 为 `http://127.0.0.1:8011/mcp`。这是 MCP Streamable HTTP 协议端点，不是普通 REST API；客户端使用官方 MCP SDK 或 `langchain-mcp-adapters` 连接。

可通过以下环境变量调整监听地址：

```bash
export MCP_SERVER_HOST=127.0.0.1
export MCP_SERVER_PORT=8011
export MCP_SERVER_PATH=/mcp
```

## 市场数据源状态

`stock.*` 目前只支持真实 Tushare 数据，生产代码没有 fixture provider。服务启动和工具发现不依赖数据源可用，但工具执行采用失败关闭：

- `configured`：配置、token 引用和 `enabled_api_names` 均有效，可以调用声明范围内的 API。
- `not_configured`：没有激活数据源，调用返回 `market_data_source_not_configured`。
- `configuration_unavailable`：Admin 不可达或配置非法，调用返回对应配置错误。

任何状态都不会返回伪造行情、固定股票资料或空业务对象充当成功结果。

## 通过本地环境配置

该服务只作为学习和本地联调示例，不接入 Admin 的官方 Tushare MCP 配置。显式启动时使用本地环境变量：

```bash
export MCP_MARKET_DATA_PROVIDER=tushare
export MCP_TUSHARE_TOKEN_ENV_VAR=TUSHARE_TOKEN
export TUSHARE_TOKEN=你的真实TushareToken
export MCP_TUSHARE_ENABLED_API_NAMES=stock_basic,daily,daily_basic
export MCP_TUSHARE_BASE_URL=https://api.tushare.pro
export MCP_TUSHARE_TIMEOUT_SECONDS=10
export MCP_TUSHARE_STOCK_BASIC_CACHE_TTL_SECONDS=3600
```

缺少 provider、token 或非空 API 声明时均返回配置错误，不自动降级。

## 字段与 API 契约

工具输入必须声明 `required_fields` 和 `optional_fields`。用户明确询问的字段应进入 `required_fields`；增强信息才进入 `optional_fields`。

当前映射：

- `stock_basic`：公司解析、名称、行业和板块基础字段。
- `stock_company`：主营业务与注册地址。
- `daily`：价格、涨跌幅、成交额，以及 20/60 日收益率、均线、波动率和区间最大回撤。
- `daily_basic`：换手率、PE、PB 和市值。
- `income`、`cashflow`、`fina_indicator`：核心财务指标及营收、归母净利润、经营现金流同比变化。
- `anns_d`：公告元数据。

未指定报告期时，`stock.financial_metrics` 查询数据源可获得的最新报告期，不再固定到某个年份。

执行前会先根据 `enabled_api_names` 计算字段覆盖：

- 必需字段权限不足：零上游调用，返回 `required_api_permission_missing`。
- 可选字段权限不足：仅调用相关且已授权 API，返回 `partial` 并列出缺失字段。
- 成功响应包含 `coverage.requested_fields`、`provided_fields`、`missing_optional_fields` 和 `source_apis`。

上游认证、权限、限流、超时、网络和响应格式错误会归一化为稳定错误码；原始响应文本和 token 不进入 Agent State 或 trace。

## 测试

常规测试完全离线：

```bash
cd "mcp server"
/opt/anaconda3/bin/python3 -m pytest tests -q
```

真实 Tushare smoke 默认跳过。显式开启后只调用管理员声明的 `stock_basic`，不会探测其他权限：

```bash
cd "mcp server"
export RUN_LIVE_TUSHARE_SMOKE=1
export TUSHARE_TOKEN=你的真实TushareToken
export TUSHARE_SMOKE_SYMBOL=600519.SH
/opt/anaconda3/bin/python3 -m pytest tests/test_live_tushare_smoke.py -q
```

## Agent 调用链路

```text
MCP Streamable HTTP Server 独立启动
-> langchain-mcp-adapters MultiServerMCPClient.get_tools
-> ToolRegistry 快照与 catalog digest
-> Intent Router / Planner / Skill evidence contract
-> ToolExecutionGateway 权限、审批、参数与风险校验
-> LangChain MCP Tool.ainvoke
-> 业务 envelope 与错误归一化
-> Analysis 字段级 ToolFact / coverage
-> Synthesizer claims
-> Verifier 回查 tool_call_id + JSON path + value
-> 最终回答与脱敏 trace summary
```
