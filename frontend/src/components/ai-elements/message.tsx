import type { HTMLAttributes, ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { cn } from '../../lib/cn'

export function Message({ from, className, ...props }: HTMLAttributes<HTMLDivElement> & { from: 'user' | 'assistant' }) {
  return <article className={cn('message', `message--${from}`, className)} {...props} />
}

export function MessageContent({ children, className }: { children: string; className?: string }) {
  return <div className={cn('message-content', className)}><ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown></div>
}

export function MessageActions({ children }: { children: ReactNode }) {
  return <div className="message-actions">{children}</div>
}
