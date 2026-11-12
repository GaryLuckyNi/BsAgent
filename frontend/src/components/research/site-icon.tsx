import { Globe2 } from 'lucide-react'

export function SiteIcon({ origin }: { origin: string }) {
  void origin
  return <span className="citation-source-icon" aria-hidden="true"><Globe2 size={16} /></span>
}
