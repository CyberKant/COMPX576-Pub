import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

const MARGIN = 8 // min distance to the viewport edge
const GAP = 8 // distance between icon and bubble
const GRACE_MS = 120 // lets the pointer travel from the icon onto the bubble

// "i" icon with an accessible tooltip. The bubble is portalled to <body> with fixed positioning,
// so it is never clipped by the scrolling left pane and floats over the pane divider and the
// right pane; its position is clamped to the viewport.
export default function InfoTip({ text }) {
  const id = useId()
  const iconRef = useRef(null)
  const bubbleRef = useRef(null)
  const closeTimer = useRef(null)
  const [open, setOpen] = useState(false)
  const [pos, setPos] = useState(null) // null while the bubble is being measured

  const close = () => { setOpen(false); setPos(null) }
  const show = () => { clearTimeout(closeTimer.current); setOpen(true) }
  const hideSoon = () => { clearTimeout(closeTimer.current); closeTimer.current = setTimeout(close, GRACE_MS) }
  const hideNow = () => { clearTimeout(closeTimer.current); close() }

  // Measure after the (hidden) bubble has rendered, then place and clamp it
  useLayoutEffect(() => {
    if (!open) return
    const icon = iconRef.current
    const bubble = bubbleRef.current
    if (!icon || !bubble) return
    const ir = icon.getBoundingClientRect()
    const bw = bubble.offsetWidth
    const bh = bubble.offsetHeight
    const vw = document.documentElement.clientWidth
    const vh = document.documentElement.clientHeight
    const left = Math.max(MARGIN, Math.min(ir.left + ir.width / 2 - bw / 2, vw - bw - MARGIN))
    let top = ir.top - bh - GAP // above the icon…
    if (top < MARGIN) top = ir.bottom + GAP // …or below when there is no room
    top = Math.max(MARGIN, Math.min(top, vh - bh - MARGIN))
    setPos({ left, top })
  }, [open, text])

  useEffect(() => {
    if (!open) return
    const dismiss = () => { setOpen(false); setPos(null) }
    const onKey = (e) => { if (e.key === 'Escape') dismiss() }
    window.addEventListener('scroll', dismiss, true) // capture: scrolling inside any container
    window.addEventListener('resize', dismiss)
    document.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('scroll', dismiss, true)
      window.removeEventListener('resize', dismiss)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  useEffect(() => () => clearTimeout(closeTimer.current), [])

  return (
    <span className="info-tip">
      <button
        ref={iconRef}
        type="button"
        className="info-tip-icon"
        aria-describedby={open ? id : undefined}
        aria-label="More information"
        onMouseEnter={show}
        onMouseLeave={hideSoon}
        onFocus={show}
        onBlur={hideNow}
      >
        i
      </button>
      {open && createPortal(
        <span
          ref={bubbleRef}
          role="tooltip"
          id={id}
          className="info-tip-bubble"
          style={pos ? { left: pos.left, top: pos.top } : { left: 0, top: 0, visibility: 'hidden' }}
          onMouseEnter={show}
          onMouseLeave={hideSoon}
        >
          {text}
        </span>,
        document.body
      )}
    </span>
  )
}
