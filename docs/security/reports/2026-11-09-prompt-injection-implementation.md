# 按原计划收口：提示注入必要防护验收

日期：2026-11-09。依据[原范围](../README.md)、[统一内容策略计划](../../superpowers/plans/2026-11-02-tool-content-policy.md)和[本轮缺口清单](../../superpowers/plans/2026-11-09-prompt-injection-gap.md)。本轮仅在独立安全模块和公共适配层补齐常见提示注入处置；未新增业务Agent安全调用、数据库表或服务。全局并发提案停止推进，未完成记忆版本原型已[撤回归档](../deferred/README.md)，此前已验证修复保留。

## 最终行为

| 接入位置 | 当前行为 |
| --- | --- |
| 公共工具网关与结果投影 | 运行配置默认`withhold`；命中高风险内容或扫描限额时扣留正文，不把工具成功/已完成改成未执行；成功结果、失败message/details、artifact、回执共用原内容策略 |
| 常见风险检测 | 中英文指令覆盖、角色/管理员伪造、秘密索取、审批绕过、记忆污染、任务与工具劫持；有限Unicode、零宽字符、HTML、URL及Base64变体；超长候选和解码次数耗尽不静默放行 |
| 历史工具消息 | 进入公共模型适配时重投影正文与artifact，保持ToolMessage角色和调用ID；外部自报可信或已审批不产生授权 |
| OCR与记忆/摘要 | 显式扣留命中内容；OCR受限结果以固定空文本/受限标记返回，现有协调器不再向后续Vision转发该原图；记忆/摘要内容检查在模型或持久化边界拒绝 |
| 工具描述与schema文本 | 在绑定模型前检查，命中拒绝绑定；原schema/凭证校验保留 |
| 公共模型适配 | 宿主System规则明确外部资料只能作为证据，不能授权、覆盖规则、扩大工具范围或跳过审批；重复保护不重复添加；普通用户询问安全概念保持可用 |

策略版本为`tool-content-v5`。`Settings.tool_content_injection_action`默认`withhold`；纯`ToolContentPolicy`构造器默认仍为`flag`，避免将受信系统提示、用户安全研究问题等用途混同外部工具内容。部署如显式配置`flag`仍需调整。业务字段规则默认空，仍不推断业务数据分类。

## 验证与证据

- [专项源码](../../../backend/app/tests/unit/test_prompt_injection_security.py)、[专项日志](2026-11-09-prompt-injection-unit-tests.log)、[JUnit](2026-11-09-prompt-injection-unit-tests.xml)：**41项通过**。包括19个攻击样本、正常投研/错误/工具描述、防御性文字、普通采购审批表述、安全概念用户问题、`trusted:false`、短编码公开数据、扫描限额、检查器故障以及真实网关成功/失败对照。攻击内容仅为合成字符串，未执行载荷。
- [118文件清单](2026-11-09-prompt-injection-test-files.txt)、[联合日志](2026-11-09-prompt-injection-tests.log)、[JUnit](2026-11-09-prompt-injection-tests.xml)：**2309项通过、14项跳过、0失败、0错误**，耗时146.23秒。包含原117文件基线与新增专项，复核同步/异步/流式、审批/恢复、权限、回执及正常业务路径；统计与专项重叠，不能相加。
- 跳过项为13项未配置隔离PostgreSQL地址的迁移/Store测试，以及1项需显式启用真实DeepSeek的压缩评估。保留既有`postgres_store`插件导入顺序的1条`PytestAssertRewriteWarning`。本轮通过`app.tests.support.no_httpx_network`阻断默认HTTPX网络，使用测试传输和临时资源，不访问真实模型、RAG或业务服务。
- 初始新增攻击样本的[红测](2026-11-09-prompt-injection-red.log)、[扫描限额红测](2026-11-09-prompt-decode-red.log)、复核发现超长Base64候选被跳过的[红测](2026-11-09-prompt-long-base64-red.log)保留。最终检测器对超过4096字符的候选直接发出`injection_scan_limit`，不继续解码；超过16个候选同样扣留。
- [首次联合失败记录](2026-11-09-prompt-injection-before-expectations-tests.log)及同名XML保留：6项失败来自宿主System前缀带来的消息位置/字符统计变化，以及原压缩样本现应在模型调用前拒绝。已保留原安全断言并更新位置/统计预期；原恶意压缩样本另断言拒绝且模型调用为零。普通摘要质量对照仍保留。
- [复核修复前联合记录](2026-11-09-prompt-injection-before-review-tests.log)及同名XML为2306项通过、14项跳过；后续补`trusted:false`、超长Base64和短编码正常对照三项，最终以上述2309项结果为准。最终相关安全模块、公共模型适配和专项的Ruff检查、`git diff --check`均通过。

联合回归可在仓库根目录复现：

```sh
PYTHONPATH=backend .venv/bin/python -m pytest \
  -p app.tests.support.no_httpx_network \
  $(cat docs/security/reports/2026-11-09-prompt-injection-test-files.txt) \
  -q -o faulthandler_timeout=60
```

## 验收边界与留存TODO

本轮证明已列样本的检测/扣留和公共适配路径回归，不证明识别所有自然语言、任意混淆、跨字段拼接、多轮或图片像素中的注入。规则和宿主提示都可能误报、漏报；工具权限、当前身份、参数审批和副作用管理仍是执行硬边界。

真实模型的多轮/多模态诱导效果、完整32类红队案例、真实PostgreSQL多进程、生产OIDC/Admin配置及RAG `localhost:8011`的上游认证/文档ACL仍未验收。完整来源追踪、未知业务分类、历史污染清理和记忆版本顺序继续留在[TODO](../TODO.md)。没有新增并发额度、记忆版本表或费用/审计基础设施，也未执行部署、真实迁移或修改外部RAG工程。总体安全TODO保持按证据逐项验收，不能由本轮通过推定全部完成。
