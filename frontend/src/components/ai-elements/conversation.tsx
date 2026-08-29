import type { HTMLAttributes, ReactNode } from 'react'

import { cn } from '../../lib/cn'

export function Conversation({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('conversation', className)} {...props} />
}

export function ConversationContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('conversation-content', className)} {...props} />
}

export function ConversationEmptyState({ icon, title, description }: {
  icon?: ReactNode
  title: string
  description: string
}) {
  return <div className="conversation-empty">{icon}<h3>{title}</h3><p>{description}</p></div>
}
