# 可重连会话与协作式停止设计

## 已确认的行为

- 刷新或断网只结束订阅，后台继续；重新进入页面恢复部分回答和后续事件。
- 用户停止是显式操作，持久化部分正文，状态与成功完成、等待审批区分；禁止旧任务覆盖新任务。
- 不统一回滚已经提交的副作用；停止阻止新的工具调用，已发出的调用保留实际结果与未知状态。
- 会话终止通过受条件约束的数据库更新完成，不凭 pending 直接清空锁。
- 保留用户设置的 CONVERSATION_HISTORY_LIMIT=0。独立保存停止交接信息，避免恢复全部历史。

## 接口契约

现有 POST /api/sessions/{session_id}/messages/protocol-stream 保持 SDK 兼容，但消费者断开不再关闭生产者。

- GET /api/sessions/{session_id}/runs/active：返回 {run: null} 或 {run: {run_id, status, message_id, content, request_id, last_event_sequence, replay_after}}，仅当前租户和用户可读。replay_after 是审批恢复后的阶段起点，不是快照游标。
- GET /api/sessions/{session_id}/runs/{run_id}/events?after=0：以持久化递增事件序号重放并订阅，返回现有 LangGraph 协议 SSE，id 为序号；结束时关闭。相同身份校验。
- POST /api/sessions/{session_id}/runs/{run_id}/stop：幂等停止请求，返回 {run_id, status}。已完成不能改为取消。
- 协议 custom 事件增加 run_started（run_id、message_id）与 run_status（status）；保留现有 messages/progress/final/approval/input/error 协议。

## 状态和执行

独立保存运行控制和输出事件。执行使用独立数据库会话；订阅不拥有执行生命周期。控制状态包括 running、stop_requested、cancelled、completed、failed、interrupted。业务消息取消状态与等待审批区分。停止先禁止后续调度，在安全边界保存并结束。外部调用结果保留执行记录，不因停止丢弃；未知写操作禁止盲重试。执行者所有权、条件更新和心跳用于防止重复执行与迟到覆盖。

前端恢复以持久化消息 ID 为准；空的运行、取消、等待输入消息可显示状态。重连从当前执行阶段的事件起点重建当前回答，避免快照与增量重复以及重放旧的等待输入终态。停止先请求后端，再断开本地订阅；新问题需要等待停止确认，不创建新 session 绕过。历史快照与 active 查询之间完成时，利用历史 run_id 补订阅其终态。

交接上下文最多 4000 字符，只针对紧邻的已取消 turn；不提升为 system 指令。未知工具结果优先于普通回执，预算不足时保留完整 JSON 和省略计数。工具网关另外独立查询所有未知写操作回执，不依赖模型从摘要中自行判断安全。

本地线程与生产 Celery 独立 turn-worker 共用执行入口。当前恢复仅接受同主机 PID 已死亡的肯定证据并更换执行令牌；不以超时心跳强行接管。跨主机或容器消失的运行需外部协调确认，未实现自动通用回滚和跨系统 exactly-once。

## 验收

断开消费者后生产继续；刷新补齐输出且不重复；停止幂等；停止后保存部分回答并可新建 turn；正常完成与停止竞争不改写成功；旧回调不释放新 run；跨租户不可订阅或停止；审批恢复原有行为保持；历史上限保持 0；新表迁移可升级及回退。
