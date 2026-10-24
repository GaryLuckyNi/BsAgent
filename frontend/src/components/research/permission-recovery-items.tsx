import type { RecoveryItemView } from '@/domain/research'

export function PermissionRecoveryItems({ items }: { items: RecoveryItemView[] }) {
  return <ul aria-label="本次确认涉及的任务" className="permission-recovery-items">
    {items.map((item) => <li key={item.item_id}>
      <p>{item.task_goal}</p>
      <p>原数据源：{item.original_tool}</p>
      <p>{item.reason}</p>
      <p>{item.source_notice}</p>
    </li>)}
  </ul>
}
