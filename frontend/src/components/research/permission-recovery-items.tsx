import type { RecoveryItemView } from '@/domain/research'

export function PermissionRecoveryItems({ items }: { items: RecoveryItemView[] }) {
  const reasons = [...new Set(items.map((item) => {
    // 仅转换已知的服务端原因；未知原因不猜测，完整原文仍可在详情中查看。
    if (item.reason === '历史工具执行尚未核实，本次原接口未派发。') {
      return '原数据源暂不可用：先前查询的状态尚未确认。'
    }
    if (item.reason === '数据源账号权限不足，原接口未执行。') {
      return '当前账号没有该数据源的访问权限。'
    }
    return '原数据源暂不可用，未取得所需数据。'
  }))]

  return <div className="permission-recovery">
    {reasons.map((reason) => <p className="permission-recovery-reason" key={reason}>{reason}</p>)}
    <details className="permission-recovery-details">
      <summary>查看详情（{items.length}项任务）</summary>
      <ul aria-label="本次确认涉及的任务" className="permission-recovery-items">
        {items.map((item) => <li key={item.item_id}>
          <p>{item.task_goal}</p>
          <p>原数据源：{item.original_tool}</p>
          <p>{item.reason}</p>
          <p>{item.source_notice}</p>
        </li>)}
      </ul>
    </details>
  </div>
}
