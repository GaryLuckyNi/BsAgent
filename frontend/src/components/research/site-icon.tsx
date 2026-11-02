import { useState } from 'react'
import { Globe2 } from 'lucide-react'

export function SiteIcon({ origin }: { origin: string }) {
  const [loaded, setLoaded] = useState(false)
  const [failed, setFailed] = useState(false)
  return <span className="citation-source-icon" aria-hidden="true">
    {!loaded && <Globe2 size={16} />}
    {!failed && <img src={`${origin}/favicon.ico`} alt="" referrerPolicy="no-referrer"
      style={{ display: loaded ? 'block' : 'none' }} onLoad={() => setLoaded(true)} onError={() => { setLoaded(false); setFailed(true) }} />}
  </span>
}
