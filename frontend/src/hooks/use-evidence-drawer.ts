import { useCallback, useState } from 'react'

export function useEvidenceDrawer() {
  const [isOpen, setIsOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState<number | null>(null)

  const openEvidence = useCallback((index: number) => {
    setActiveIndex(index)
    setIsOpen(true)
  }, [])

  const closeEvidence = useCallback(() => setIsOpen(false), [])

  return {
    isOpen,
    activeIndex,
    openEvidence,
    closeEvidence
  }
}
