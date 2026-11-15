# Tushare 真实数据与字段级证据契约设计

## 背景

当前 MCP Server 已接入 Tushare Pro HTTP API，但生产代码仍保留 `FixtureMarketDataProvider` 和一批伪造的股票、行情、财务与风险数据。`fixture` 同时还是数据源的默认值，Admin 也允许将其激活。这会导致真实数据源未配置或不可用时，系统仍可能返回看似合理的伪造金融事实。

另一个风险是权限与证据完备性没有进入 Agent 执行契约。一个业务工具可能依赖多个 Tushare 接口，但当前 Provider 会固定调用整组接口；某个接口未授权时，系统无法根据用户本次问题判断该字段是必需还是可选。Synthesizer 直接消费返回字典，Verifier 也没有校验金融结论与工具字段的引用关系。

## 目标

1. 彻底删除生产运行时的伪造市场数据和 `fixture` Provider。
2. 未配置真实数据源时，MCP 仍可启动和发现工具，但股票工具必须返回可识别的未配置错误。
3. Admin 显式管理 Token 已开通的 Tushare 接口，不由模型猜测账号权限。
4. Planner 和 Skill 为每次请求声明必需字段与可选字段。
5. MCP 仅调用获取本次所需字段的 Tushare 接口，并返回结构化覆盖率。
6. 金融事实结论必须引用真实工具字段或 RAG 证据，缺失的必需字段不得由 LLM 补全。

## 非目标

- 不向模型暴露通用 `tushare.query(api_name, params, fields)` 工具。
- 不为 Tushare 每一个原始接口创建一个 MCP 工具。
- 不在启动时逐一调用 Tushare 接口探测权限，避免消耗积分和触发限流。
- 不伪造 `stock.risk_events` 的真实数据映射；在找到真实、稳定的数据来源前，不将其作为默认 Skill 依赖。
- 不把 Tushare Token 明文保存到 Admin 数据库。

## 方案选择

采用“业务级 MCP 工具 + 字段级证据契约”。

不拆分成大量指标级工具，以免工具目录膨胀与模型选择错误；也不暴露 Tushare 原始查询入口，以免模型越过业务契约、权限治理和审计边界。

## 总体架构

```text
Admin 数据源配置
  ├─ token_env_var
  └─ enabled_api_names
          |
          v
Planner / Skill evidence_contract
  ├─ required_fields
  └─ optional_fields
          |
          v
MCP 业务工具
  字段校验 -> 字段/API 依赖解析 -> 权限校验
          |
          v
Tushare Provider
  仅调用已授权且本次所需的 api_name
          |
          v
结构化工具结果
  data + coverage + source_apis + warnings
          |
          v
Analysis Agent -> claim + tool field reference
          |
          v
Synthesizer -> 受限渲染
          |
          v
Verifier -> 字段覆盖、引用路径、数值一致性校验
```

## 生产 Provider 边界

`mcp server/app/market_data/provider.py` 不再包含任何股票样例、别名、行情、财务、公告或风险数据。该模块仅保留 Provider 协议、数据源运行状态和领域错误。

生产运行时只支持 Tushare。未配置时使用无数据 Provider，它不返回任何数据，只生成 `market_data_source_not_configured` 结构化错误。MCP Server 仍可正常启动，`list_tools` 仍可发现股票业务工具。

确定性数据只能存在测试目录中，通过 `FakeTushareClient` 等测试替身注入，不能成为可选生产 Provider。

## Admin 数据源配置

Admin 的 Provider 类型只保留 `tushare`，配置示例：

```json
{
  "provider": "tushare",
  "settings": {
    "token_env_var": "TUSHARE_TOKEN",
    "base_url": "https://api.tushare.pro",
    "timeout_seconds": 10,
    "stock_basic_cache_ttl_seconds": 3600,
    "enabled_api_names": [
      "stock_basic",
      "daily",
      "daily_basic",
      "income"
    ]
  }
}
```

`enabled_api_names` 是管理员根据 Tushare 账号实际权限声明的原始接口名列表。Admin 校验其为去重的非空字符串列表，MCP 运行时再与当前 Adapter 支持的接口取交集。未识别接口保留在配置中并进入诊断信息，但不参与调用。

Admin 仅保存密钥环境变量名，真实 Token 继续由容器密钥、K8s Secret 或云 Secret Manager 注入。

## 配置加载语义

数据源有三种状态：

- `configured`：已获取 Tushare 配置、Token 与权限声明。
- `not_configured`：没有 Admin 激活配置，也没有显式环境变量配置。
- `configuration_unavailable`：已声明 Admin 配置地址，但 Admin 请求超时、连接失败或返回非法响应。

当 `MCP_MARKET_DATA_ADMIN_BASE_URL` 已配置时，Admin 不可用必须闭锁失败，不能静默回退到环境变量或样例数据。只有未配置 Admin 地址时，才允许通过环境变量显式配置 Tushare。

## 字段级数据契约

需要按字段获取数据的工具输入增加：

```json
{
  "symbol": "300085.SZ",
  "required_fields": ["price"],
  "optional_fields": ["pe_ttm", "pb"]
}
```

字段名由各业务工具的 JSON Schema 枚举限制。同一字段不能同时出现在必需和可选集合中。工具不接受 `api_name`、Token 或任意 `fields` 透传。

字段与 Tushare API 的依赖关系属于 Adapter 契约，不属于 LLM 推理内容。第一版包括：

| 业务工具 | 业务字段 | Tushare API |
| --- | --- | --- |
| `stock.resolve_company` | `symbol`, `name`, `exchange` | `stock_basic` |
| `stock.company_profile` | `name`, `industry`, `board` | `stock_basic` |
| `stock.company_profile` | `main_business`, `registered_address` | `stock_company` |
| `stock.market_snapshot` | `price`, `pct_change`, `turnover` | `daily` |
| `stock.market_snapshot` | `market_cap`, `pe_ttm`, `pb` | `daily_basic` |
| `stock.financial_metrics` | `revenue`, `net_profit_parent` | `income` |
| `stock.financial_metrics` | `operating_cash_flow` | `cashflow` |
| `stock.financial_metrics` | `gross_margin`, `roe`, `debt_to_asset` | `fina_indicator` |
| `stock.announcement_search` | `announcements` | `anns_d` |

MCP 先计算本次必需和可选字段所需的 API，再与 `enabled_api_names` 比对。缺少必需 API 时不发起 Tushare 请求；仅缺少可选 API 时，调用已授权接口并明确返回缺失覆盖。

## 工具输出契约

市场数据工具返回统一结构化信封。成功或部分成功示例：

```json
{
  "status": "partial",
  "data": {
    "symbol": "300085.SZ",
    "price": 18.23
  },
  "coverage": {
    "requested_fields": ["price", "pe_ttm", "pb"],
    "required_fields": ["price"],
    "optional_fields": ["pe_ttm", "pb"],
    "provided_fields": ["price"],
    "missing_required_fields": [],
    "missing_optional_fields": ["pe_ttm", "pb"],
    "source_apis": ["daily"]
  },
  "warnings": ["optional_api_permission_missing:daily_basic"]
}
```

必需数据不可用时返回失败信封，动态 MCP Adapter 必须将其转换为失败的 `ToolCallResult`，不能作为普通成功数据进入 Agent State。

失败信封示例：

```json
{
  "status": "failed",
  "error": {
    "code": "required_api_permission_missing",
    "message": "当前数据源权限无法提供必需字段。",
    "details": {
      "missing_fields": ["pe_ttm"],
      "missing_api_permissions": ["daily_basic"]
    },
    "retryable": false
  }
}
```

Agent 侧 `ToolError` 同步增加受限的 `details` 字典与 `retryable` 标记。Tool Agent 和 Synthesizer 通过结构化字段生成用户提示，不得通过解析错误字符串恢复缺失字段或权限信息。`details` 只允许进入经过白名单和脱敏的诊断数据，不能包含 Token、上游完整响应或连接凭证。

## 错误分层

| 错误码 | 语义 | 执行行为 |
| --- | --- | --- |
| `market_data_source_not_configured` | 未配置真实数据源 | 不请求 Tushare，提示联系管理员配置 |
| `market_data_configuration_unavailable` | Admin 配置服务不可用 | 不回退，返回暂时不可用 |
| `required_api_permission_missing` | 权限声明缺少必需 API | 不请求 Tushare，阻断相关结论 |
| `provider_permission_denied` | Tushare 实际拒绝已声明权限 | 阻断结论，提示管理员核对权限 |
| `provider_rate_limited` | Tushare 频率或积分限制 | 返回可重试的上游错误 |
| `provider_timeout` | Tushare 请求超时 | 返回暂时不可用 |
| `required_data_missing` | 接口成功但未返回必需记录或字段 | 不用 LLM 补齐，明确数据缺失 |
| `invalid_field_request` | 请求字段不在工具 Schema | 拒绝执行并记录 Planner/Skill 契约错误 |

HTTP 错误、Tushare 业务错误和响应解析错误必须在 Tushare Client 层转换为可分类异常，不再只抛出通用 `RuntimeError`。错误日志不能包含 Token 或完整请求载荷。

## Skill 证据契约

Admin `skill_profiles` 增加 JSON 字段 `evidence_contract`，通过数据库迁移发布。契约以业务能力名而不是完整 MCP 工具名建模：

```json
{
  "requirements": [
    {
      "capability": "stock.company_profile",
      "required_fields": ["name", "industry"],
      "optional_fields": ["main_business", "registered_address"]
    },
    {
      "capability": "stock.market_snapshot",
      "required_fields": ["price", "pct_change"],
      "optional_fields": ["pe_ttm", "pb", "market_cap"]
    }
  ]
}
```

`ConfiguredSkill` 通过工具名后缀匹配业务能力，将字段契约注入工具参数。这使同一 Skill 可在不同 MCP Server 名称下复用，并清除对 `mcp.mock.stock.*` 的业务判断。

已有 Skill 的空契约不得解释为“所有字段均可用”。对需要字段契约的市场数据工具，空契约应使 Skill 返回配置错误，直到管理员补齐配置。默认投研 Skill 的种子配置将同步增加证据契约，并从风险扫描 Skill 中移除 `stock.risk_events`。

## Planner 契约

对非 Skill 的单次工具请求，Planner 必须根据用户问题和工具 JSON Schema 输出 `required_fields` 与 `optional_fields`。服务端覆盖工具名、版本和风险策略，并通过 Schema 拒绝未知字段、重复字段与空必需集合。

Planner 是字段需求的语义解析者，但不是权限与数据覆盖率的信任边界。MCP 仍须独立校验每一个请求。

## 结构化事实与引用

Analysis Agent 将每个工具输出中的已提供字段转换为结构化事实，并保留工具调用和字段路径：

```json
{
  "claim_id": "claim_market_1",
  "text": "银之杰最新收盘价为 18.23 元",
  "claim_type": "fact",
  "tool_evidence_refs": [
    {
      "tool_call_id": "call_123",
      "field_path": "data.price",
      "value": 18.23
    }
  ]
}
```

`AnswerClaim` 扩展 `tool_evidence_refs`，同时保留现有 `evidence_ids` 以支持 RAG 证据。Synthesizer 先从结构化事实生成 claims，再将 claims 渲染为自然语言，不再将原始 Python 字典直接拼入回答。

LLM 渲染器只接收已通过契约的 claims 和缺失信息。如果渲染结果无法通过后置一致性校验，系统使用确定性模板渲染同一批 claims，不将失败草稿返回给用户。

## Verifier 强制规则

1. 所有 `required_fields` 必须存在于 `provided_fields`。
2. 金融事实 claim 必须至少引用一条 RAG 证据或一个工具字段路径。
3. `tool_call_id` 必须对应本次 Run 中已成功执行的工具调用。
4. `field_path` 必须在工具返回中存在，引用值必须与工具结果一致。
5. 最终回答中的金额、比例、日期和证券代码必须能在 claims 或引用证据中找到。
6. 可选字段缺失时，回答必须显式说明未覆盖的数据维度。
7. 任一规则失败时，原草稿不得返回给用户，必须进入修订或确定性错误模板。

## `stock.risk_events` 处理

当前 Tushare Adapter 没有真实风险事件映射，返回空数组会混淆“没有发现风险”和“没有数据能力”。第一版将其从 MCP 工具目录和默认风险扫描 Skill 中移除。风险扫描暂时使用真实公告与 RAG 证据，后续接入股权质押、减持、处罚、诉讼等真实数据源后再注册新的风险工具。

## 数据库迁移与兼容性

Admin `skill_profiles` 新增 `evidence_contract` JSON 字段，并为默认投研 Skill 写入显式契约。迁移不会删除用户自定义 Skill，但需要市场数据字段的旧 Skill 在补充契约前不能执行相关工具。

`market_data_provider_configs.settings` 仍为 JSON，不需要表结构迁移。已有 Tushare 配置如果没有 `enabled_api_names`，按空权限处理，管理员必须在 Admin 补充真实权限，系统不会自动假设拥有任何接口。

## 可观测性

每次市场数据工具调用应记录以下非敏感字段：

- Provider 名称和配置 ID。
- 业务工具名。
- 请求字段、已提供字段和缺失字段。
- 实际调用的 Tushare `api_name`。
- 耗时、重试次数和结果分类。
- 错误码，但不记录 Token 和完整上游请求体。

这些字段进入现有 trace 事件和 JSON summary，用于排查权限声明与实际权限不一致、字段覆盖不足和上游限流。

## 测试策略

### Admin API

- Provider 类型只接受 `tushare`。
- `enabled_api_names` 必须是去重的非空字符串列表。
- Skill `evidence_contract` 校验能力名、字段列表和必需/可选互斥。
- 旧 Skill 空契约不能获得隐式完整权限。

### MCP 与 Tushare Provider

- 未配置数据源时 MCP 可启动，工具返回 `market_data_source_not_configured`。
- 缺少必需 API 时不发起 Tushare 请求。
- 缺少可选 API 时只请求已授权 API，并返回 `partial` 覆盖。
- Tushare 实际权限拒绝、限流、超时与空数据被分类为不同错误。
- Provider 按请求字段裁剪 API 调用，不固定调用整组接口。

### Agent 节点

- Planner 向直接市场数据请求注入字段需求。
- Configured Skill 从 `evidence_contract` 注入字段需求。
- Analysis Agent 不依赖 `mcp.mock.stock.*` 前缀，并生成工具字段引用。
- Synthesizer 不直接输出原始字典，且只渲染已验证 claims。
- Verifier 拒绝缺少必需字段、引用路径不存在、引用值不一致和无证据数字。

### 集成验收

- 用户询问 PE，但未声明 `daily_basic` 权限时，最终回答明确说明所缺接口或字段，不得出现 PE 数值。
- 用户询问收盘价，且仅有 `daily` 权限时，只调用 `daily` 并正常回答。
- 未配置数据源时，最终回答明确提示联系管理员配置。
- 可选估值字段缺失时，综合分析可继续，但必须显示估值维度未覆盖。
- 生产目录不再存在 `FixtureMarketDataProvider`、伪造行情常量或 `mcp.mock.stock.*` 业务前缀判断。

真实 Tushare smoke test 默认跳过，只在显式开启并注入 Token 时运行，避免 CI 消耗积分或触发频率限制。

## 验收标准

1. 生产代码和 Admin 中不再存在 `fixture` 数据源选项或默认回退。
2. 无真实数据源配置时，MCP 可启动，股票请求不产生任何伪造数据，Agent 返回明确业务错误。
3. 市场数据工具只调用获取请求字段所需的 Tushare API。
4. 缺少必需权限或必需数据时，回答被阻断；缺少可选数据时，回答显式披露覆盖缺口。
5. 每个金融事实 claim 可追溯到本次 Run 中的工具字段或 RAG 证据。
6. 管理员可在 Admin 维护 Tushare 权限声明和 Skill 证据契约。
7. 自动化测试覆盖配置、权限、字段覆盖、错误分类、结论引用和最终回答。
