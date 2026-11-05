import { useMemo, useRef, useEffect, useState, useCallback } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
// 1. Force Dynamics Ontology Configuration (shared module)
import { FORCE_ONTOLOGY, RELATION_COLORS } from '../ontology'

const LABEL_FONT = 'Inter, "Segoe UI", system-ui, sans-serif'
const getId = (n) => (typeof n === 'object' && n !== null ? n.id : n)
const prettify = (id) => String(id).replace(/_/g, ' ')
const colorOf = (relation) => RELATION_COLORS[relation] || RELATION_COLORS.DEFAULT

const withAlpha = (hex, alpha) => {
  const v = parseInt(hex.slice(1), 16)
  return `rgba(${(v >> 16) & 255}, ${(v >> 8) & 255}, ${v & 255}, ${alpha})`
}

// Geometric hit-testing helpers. force-graph's own picking reads a shadow canvas that is
// repainted at most every 800ms and goes stale when devicePixelRatio changes, so we
// hit-test against live node positions instead.
const LINK_SAMPLES = 16

function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax
  const dy = by - ay
  const len2 = dx * dx + dy * dy
  const t = len2 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy))
}

// Reuses the curve geometry force-graph stores on each link (__controlPoints):
// null → straight, [cx, cy] → quadratic bezier, [c1x, c1y, c2x, c2y] → cubic (self-loops)
function distToLink(px, py, link) {
  const s = link.source
  const t = link.target
  if (!Number.isFinite(s?.x) || !Number.isFinite(t?.x)) return Infinity
  const cp = link.__controlPoints
  if (!cp) return distToSegment(px, py, s.x, s.y, t.x, t.y)

  const point = (u) => {
    const v = 1 - u
    if (cp.length === 2) {
      return [v * v * s.x + 2 * v * u * cp[0] + u * u * t.x, v * v * s.y + 2 * v * u * cp[1] + u * u * t.y]
    }
    return [
      v * v * v * s.x + 3 * v * v * u * cp[0] + 3 * v * u * u * cp[2] + u * u * u * t.x,
      v * v * v * s.y + 3 * v * v * u * cp[1] + 3 * v * u * u * cp[3] + u * u * u * t.y
    ]
  }
  let min = Infinity
  let prev = [s.x, s.y]
  for (let i = 1; i <= LINK_SAMPLES; i++) {
    const cur = point(i / LINK_SAMPLES)
    min = Math.min(min, distToSegment(px, py, prev[0], prev[1], cur[0], cur[1]))
    prev = cur
  }
  return min
}

// 2. Custom d3 forces (kept dependency-free; n is small so O(n²) is fine)
// Collision: keeps node bodies + label room from overlapping
function collideForce(padding) {
  let nodes = []
  const force = () => {
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i]
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j]
        let dx = b.x - a.x
        let dy = b.y - a.y
        let dist = Math.hypot(dx, dy)
        const min = a.radius + b.radius + padding
        if (dist >= min) continue
        if (dist === 0) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; dist = Math.hypot(dx, dy) }
        const push = ((min - dist) / dist) * 0.5
        dx *= push; dy *= push
        if (a.fx == null) { a.vx -= dx; a.vy -= dy }
        if (b.fx == null) { b.vx += dx; b.vy += dy }
      }
    }
  }
  force.initialize = (n) => { nodes = n }
  return force
}

// Gravity: weak pull to the origin so disjoint components don't drift away
function gravityForce(strength) {
  let nodes = []
  const force = (alpha) => {
    for (const n of nodes) {
      if (!Number.isFinite(n.x) || !Number.isFinite(n.y)) continue
      n.vx -= n.x * strength * alpha
      n.vy -= n.y * strength * alpha
    }
  }
  force.initialize = (n) => { nodes = n }
  return force
}

// focusRelations: relation types the question asks about (backend intent detection); drawn heavier
export default function CausalGraph({ edges = [], focusRelations = [] }) {
  const focusSet = useMemo(() => new Set(focusRelations), [focusRelations])
  const fgRef = useRef()
  const containerRef = useRef() // Native DOM reference for dimensions
  const fittedRef = useRef(null) // graphData instance we've already auto-fit
  const toolbarRef = useRef()
  const legendRef = useRef()
  const inspectorRef = useRef()
  const tooltipRef = useRef()
  const dragRef = useRef(null) // { node, offsetX, offsetY, startX, startY, moved, lastReheat }
  const pressRef = useRef(null) // { hit, startX, startY } for click detection
  const hoverRef = useRef(null) // last hit object, to avoid re-rendering on every move

  const [dimensions, setDimensions] = useState({ width: 0, height: 0 })
  const [hoveredRel, setHoveredRel] = useState(null)
  const [isLegendOpen, setIsLegendOpen] = useState(false)
  const [hoverNode, setHoverNode] = useState(null)
  const [hoverLink, setHoverLink] = useState(null)
  const [selection, setSelection] = useState({ graph: null, id: null })
  const [pinnedCount, setPinnedCount] = useState(0)
  const [canvasKey, setCanvasKey] = useState(0)

  // Remount the canvas when devicePixelRatio changes (browser zoom, monitor move):
  // force-graph sizes its canvases for the DPR at mount time only
  useEffect(() => {
    let mql
    const onChange = () => {
      fittedRef.current = null
      setCanvasKey((k) => k + 1)
      listen()
    }
    const listen = () => {
      mql?.removeEventListener('change', onChange)
      mql = window.matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`)
      mql.addEventListener('change', onChange)
    }
    listen()
    return () => mql?.removeEventListener('change', onChange)
  }, [])

  // 1. Native Bounding Box Observer (Replaces AutoSizer)
  useEffect(() => {
    if (!containerRef.current) return

    const resizeObserver = new ResizeObserver((entries) => {
      for (let entry of entries) {
        setDimensions({
          width: Math.floor(entry.contentRect.width),
          height: Math.floor(entry.contentRect.height)
        })
      }
    })

    resizeObserver.observe(containerRef.current)
    return () => resizeObserver.disconnect() // Cleanup on unmount
  }, [])

  const graphData = useMemo(() => {
    if (!edges || edges.length === 0) return { nodes: [], links: [] }

    const nodeSet = new Set()
    const edgeMap = new Map()

    edges.forEach((edge) => {
      if (edge.source && edge.target) {
        nodeSet.add(edge.source)
        nodeSet.add(edge.target)

        const uniqueKey = `${edge.source}|${edge.relation}|${edge.target}`
        if (!edgeMap.has(uniqueKey)) {
          edgeMap.set(uniqueKey, {
            source: edge.source,
            target: edge.target,
            relation: edge.relation || 'DEFAULT',
            detail: edge.detail || 'No detail.'
          })
        }
      }
    })

    const links = Array.from(edgeMap.values())
    const pairCounts = {}

    links.forEach((link) => {
      const pairId = [link.source, link.target].sort().join('|')
      pairCounts[pairId] = (pairCounts[pairId] || 0) + 1
      link.pairId = pairId
    })

    const pairCounters = {}
    links.forEach((link) => {
      const pid = link.pairId
      if (link.source === link.target) {
        link.curvature = 0.6 // self-loops need curvature to be visible
      } else if (pairCounts[pid] === 1) {
        link.curvature = 0
      } else {
        pairCounters[pid] = (pairCounters[pid] || 0) + 1
        const index = pairCounters[pid]
        const step = Math.ceil(index / 2) * 0.2
        const sign = index % 2 === 0 ? 1 : -1
        const parity = link.source < link.target ? 1 : -1
        link.curvature = sign * step * parity
      }
    })

    // Adjacency for hover/selection highlighting
    const neighbors = new Map()
    const nodeLinks = new Map()
    nodeSet.forEach((id) => { neighbors.set(id, new Set()); nodeLinks.set(id, []) })
    links.forEach((link) => {
      neighbors.get(link.source).add(link.target)
      neighbors.get(link.target).add(link.source)
      nodeLinks.get(link.source).push(link)
      if (link.source !== link.target) nodeLinks.get(link.target).push(link)
    })

    const nodes = Array.from(nodeSet).map((id) => {
      const degree = nodeLinks.get(id).length
      return { id, degree, radius: 3.5 + 2.2 * Math.sqrt(degree) }
    })

    const relationCounts = {}
    links.forEach((l) => { relationCounts[l.relation] = (relationCounts[l.relation] || 0) + 1 })

    return { nodes, links, neighbors, nodeLinks, relationCounts }
  }, [edges])

  const isReady = dimensions.width > 0 && dimensions.height > 0
  const selectedId = selection.graph === graphData ? selection.id : null

  // 3. PHYSICS ENGINE — re-applied once the canvas actually mounts (isReady)
  useEffect(() => {
    const fg = fgRef.current
    if (!fg) return
    // Bounding the electrostatic charge radius prevents subgraphs from flying off-screen
    fg.d3Force('charge').strength((n) => -160 - n.degree * 10).distanceMax(400)
    fg.d3Force('link').distance((l) => 36 + 7 * Math.sqrt(Math.max(l.source.degree || 1, l.target.degree || 1)))
    fg.d3Force('collide', collideForce(14))
    fg.d3Force('gravity', gravityForce(0.05))
    fg.d3ReheatSimulation()
  }, [graphData, isReady])

  // 4. Focus model: hovered/dragged node > selected node > legend relation > hovered link
  const focusId = hoverNode?.id ?? selectedId
  const { activeNodes, activeLinks } = useMemo(() => {
    if (focusId != null && graphData.neighbors?.has(focusId)) {
      return {
        activeNodes: new Set([focusId, ...graphData.neighbors.get(focusId)]),
        activeLinks: new Set(graphData.nodeLinks.get(focusId))
      }
    }
    if (hoveredRel) {
      const ls = graphData.links.filter((l) => l.relation === hoveredRel)
      return {
        activeNodes: new Set(ls.flatMap((l) => [getId(l.source), getId(l.target)])),
        activeLinks: new Set(ls)
      }
    }
    if (hoverLink) {
      return {
        activeNodes: new Set([getId(hoverLink.source), getId(hoverLink.target)]),
        activeLinks: new Set([hoverLink])
      }
    }
    return { activeNodes: null, activeLinks: null }
  }, [focusId, hoveredRel, hoverLink, graphData])

  // Fit the graph into the canvas area NOT covered by overlays (toolbar, legend, inspector),
  // so no node starts underneath DOM panels that would swallow its pointer events
  const fitToSafeArea = useCallback((ms = 500) => {
    const fg = fgRef.current
    const container = containerRef.current
    if (!fg || !container || graphData.nodes.length === 0) return
    const bbox = fg.getGraphBbox()
    if (!bbox) return

    const c = container.getBoundingClientRect()
    let safe = { left: 0, top: 0, right: c.width, bottom: c.height }
    const area = (s) => Math.max(0, s.right - s.left) * Math.max(0, s.bottom - s.top)

    ;[toolbarRef, legendRef, inspectorRef].forEach((ref) => {
      if (!ref.current) return
      const r = ref.current.getBoundingClientRect()
      const o = { left: r.left - c.left - 8, top: r.top - c.top - 8, right: r.right - c.left + 8, bottom: r.bottom - c.top + 8 }
      // Skip overlays that don't intersect the current safe rect
      if (o.right <= safe.left || o.left >= safe.right || o.bottom <= safe.top || o.top >= safe.bottom) return
      // Cut away whichever side of the safe rect loses the least area
      const candidates = [
        { ...safe, top: Math.max(safe.top, o.bottom) },
        { ...safe, bottom: Math.min(safe.bottom, o.top) },
        { ...safe, left: Math.max(safe.left, o.right) },
        { ...safe, right: Math.min(safe.right, o.left) }
      ]
      safe = candidates.reduce((best, s) => (area(s) > area(best) ? s : best))
    })

    const pad = 24
    const safeW = Math.max(safe.right - safe.left - 2 * pad, 50)
    const safeH = Math.max(safe.bottom - safe.top - 2 * pad, 50)
    const bw = Math.max(bbox.x[1] - bbox.x[0], 1)
    const bh = Math.max(bbox.y[1] - bbox.y[0], 1)
    const k = Math.min(Math.max(Math.min(safeW / bw, safeH / bh), 0.3), 4)

    const bcx = (bbox.x[0] + bbox.x[1]) / 2
    const bcy = (bbox.y[0] + bbox.y[1]) / 2
    const safeCx = (safe.left + safe.right) / 2
    const safeCy = (safe.top + safe.bottom) / 2
    fg.zoom(k, ms)
    fg.centerAt(bcx - (safeCx - c.width / 2) / k, bcy - (safeCy - c.height / 2) / k, ms)
  }, [graphData])

  const countPins = useCallback(() => {
    setPinnedCount(graphData.nodes.filter((n) => n.fx != null).length)
  }, [graphData])

  const releaseAll = () => {
    graphData.nodes.forEach((n) => { n.fx = undefined; n.fy = undefined })
    setPinnedCount(0)
    fgRef.current?.d3ReheatSimulation()
  }

  const nodeCanvasObject = useCallback((node, ctx, scale) => {
    if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) return
    const r = node.radius
    const isFocus = node.id === focusId
    const isActive = !activeNodes || activeNodes.has(node.id)
    const isHub = node.degree >= 5

    ctx.globalAlpha = isActive ? 1 : 0.15

    if (isFocus) {
      ctx.beginPath()
      ctx.arc(node.x, node.y, r + 6 / scale + 2, 0, 2 * Math.PI)
      ctx.fillStyle = 'rgba(56, 189, 248, 0.18)'
      ctx.fill()
    }

    ctx.beginPath()
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI)
    ctx.fillStyle = isFocus ? '#38bdf8' : activeNodes && isActive ? '#e2e8f0' : isHub ? '#a5b4fc' : '#94a3b8'
    ctx.fill()
    ctx.lineWidth = (node.fx != null ? 2 : 1.25) / scale
    ctx.strokeStyle = node.fx != null ? '#f59e0b' : 'rgba(6, 9, 14, 0.9)'
    ctx.stroke()

    // Labels: always for hubs/focus/neighbourhood, everything else once zoomed in
    const showLabel = isActive && (isFocus || (activeNodes && isActive) || node.degree >= 4 || scale >= 1.4)
    if (showLabel) {
      const fontSize = (isFocus ? 13 : 11.5) / scale
      ctx.font = `${isFocus || isHub ? 600 : 400} ${fontSize}px ${LABEL_FONT}`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'
      const label = prettify(node.id)
      const y = node.y + r + 3 / scale
      ctx.lineWidth = 3 / scale
      ctx.lineJoin = 'round'
      ctx.strokeStyle = 'rgba(6, 9, 14, 0.9)'
      ctx.strokeText(label, node.x, y)
      ctx.fillStyle = isFocus || (activeNodes && isActive) ? '#f1f5f9' : '#cbd5e1'
      ctx.fillText(label, node.x, y)
    }
    ctx.globalAlpha = 1
  }, [focusId, activeNodes])

  const linkColor = useCallback((link) => {
    const c = colorOf(link.relation)
    if (!activeLinks) return withAlpha(c, focusSet.has(link.relation) ? 0.95 : 0.6)
    return activeLinks.has(link) ? c : withAlpha(c, 0.06)
  }, [activeLinks, focusSet])

  const linkWidth = useCallback((link) => (activeLinks?.has(link) ? 2.4 : focusSet.has(link.relation) ? 2.2 : 1.1), [activeLinks, focusSet])
  const linkParticles = useCallback((link) => (activeLinks?.has(link) ? 2 : !activeLinks && focusSet.has(link.relation) ? 1 : 0), [activeLinks, focusSet])

  // 5. Pointer layer: live geometric hit-testing (replaces force-graph's shadow-canvas picking)
  const toGraph = (clientX, clientY) => {
    const fg = fgRef.current
    const el = containerRef.current
    if (!fg || !el) return null
    const r = el.getBoundingClientRect()
    const px = clientX - r.left
    const py = clientY - r.top
    const g = fg.screen2GraphCoords(px, py)
    const g1 = fg.screen2GraphCoords(px + 1, py)
    return { x: g.x, y: g.y, k: 1 / (g1.x - g.x || 1), px, py, width: r.width, height: r.height }
  }

  const hitTest = (p) => {
    if (!p) return null
    let best = null
    let bestDist = Infinity
    for (const n of graphData.nodes) {
      if (!Number.isFinite(n.x)) continue
      const d = Math.hypot(n.x - p.x, n.y - p.y)
      if (d <= Math.max(n.radius + 2, 6 / p.k) && d < bestDist) { best = n; bestDist = d }
    }
    if (best) return { type: 'node', obj: best }

    bestDist = Infinity
    for (const l of graphData.links) {
      const d = distToLink(p.x, p.y, l)
      if (d <= (linkWidth(l) / 2 + 4) / p.k && d < bestDist) { best = l; bestDist = d }
    }
    return best ? { type: 'link', obj: best } : null
  }

  const updateHover = (hit) => {
    const prev = hoverRef.current
    if (prev?.obj === hit?.obj) return
    hoverRef.current = hit
    setHoverNode(hit?.type === 'node' ? hit.obj : null)
    setHoverLink(hit?.type === 'link' ? hit.obj : null)
  }

  const placeTooltip = (p) => {
    const tip = tooltipRef.current
    if (!tip || !p) return
    const w = tip.offsetWidth
    const h = tip.offsetHeight
    const x = p.px + 14 + w > p.width ? p.px - 14 - w : p.px + 14
    const y = p.py + 14 + h > p.height ? p.py - 14 - h : p.py + 14
    tip.style.transform = `translate(${Math.max(4, x)}px, ${Math.max(4, y)}px)`
  }

  // Selected-node inspector rows
  const selectedRelations = useMemo(() => {
    if (selectedId == null || !graphData.nodeLinks?.has(selectedId)) return null
    return graphData.nodeLinks.get(selectedId).map((l) => {
      const outgoing = getId(l.source) === selectedId
      return { link: l, outgoing, other: outgoing ? getId(l.target) : getId(l.source) }
    })
  }, [selectedId, graphData])

  const selectNode = (id) => setSelection({ graph: graphData, id })

  const setCursor = (c) => { if (containerRef.current) containerRef.current.style.cursor = c }

  const handlePointerDown = (e) => {
    if (e.button !== 0) return
    const p = toGraph(e.clientX, e.clientY)
    const hit = hitTest(p)
    pressRef.current = { hit, startX: e.clientX, startY: e.clientY }
    if (hit?.type !== 'node') return // empty canvas / link: let d3-zoom pan

    // Cancelling pointerdown suppresses the compatibility mousedown, so d3-zoom won't pan
    e.preventDefault()
    e.currentTarget.setPointerCapture(e.pointerId)
    const node = hit.obj
    dragRef.current = {
      node,
      offsetX: node.x - p.x,
      offsetY: node.y - p.y,
      prevFx: node.fx,
      prevFy: node.fy,
      moved: false,
      lastReheat: 0
    }
    node.fx = node.x
    node.fy = node.y
    setCursor('grabbing')
  }

  const handlePointerMove = (e) => {
    const p = toGraph(e.clientX, e.clientY)
    if (!p) return
    const drag = dragRef.current

    if (drag) {
      if (!drag.moved && Math.hypot(e.clientX - pressRef.current.startX, e.clientY - pressRef.current.startY) < 4) return
      drag.moved = true
      const { node } = drag
      node.fx = node.x = p.x + drag.offsetX
      node.fy = node.y = p.y + drag.offsetY
      // Neighbours follow through the link force; keep the engine alive during long drags
      const now = performance.now()
      if (now - drag.lastReheat > 1500) {
        fgRef.current?.d3ReheatSimulation()
        drag.lastReheat = now
      }
      placeTooltip(p)
      return
    }

    const hit = hitTest(p)
    updateHover(hit)
    placeTooltip(p)
    setCursor(hit?.type === 'node' ? 'grab' : hit ? 'pointer' : '')
  }

  const handlePointerUp = (e) => {
    const drag = dragRef.current
    const press = pressRef.current
    dragRef.current = null
    pressRef.current = null

    if (drag) {
      if (e.currentTarget.hasPointerCapture?.(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId)
      setCursor('grab')
      if (drag.moved) {
        countPins() // stays pinned where dropped
      } else {
        // Plain click: restore previous pin state and toggle selection
        drag.node.fx = drag.prevFx
        drag.node.fy = drag.prevFy
        selectNode(selectedId === drag.node.id ? null : drag.node.id)
      }
      return
    }

    if (!press || e.type === 'pointercancel') return
    if (Math.hypot(e.clientX - press.startX, e.clientY - press.startY) >= 4) return // was a pan

    if (press.hit?.type === 'link') {
      // Interaction Stub for Supabase JSONB Mutation
      const link = press.hit.obj
      console.log(`[Interaction] Captured Edge for DB Mutation:`, {
        source: getId(link.source),
        relation: link.relation,
        target: getId(link.target)
      })
    } else {
      selectNode(null)
    }
  }

  const handleContextMenu = (e) => {
    const hit = hitTest(toGraph(e.clientX, e.clientY))
    if (hit?.type !== 'node') return
    e.preventDefault()
    hit.obj.fx = undefined
    hit.obj.fy = undefined
    countPins()
    fgRef.current?.d3ReheatSimulation()
  }

  const handlePointerLeave = () => {
    if (dragRef.current) return
    updateHover(null)
    setCursor('')
  }

  return (
    <div className="graph-root">

      {/* TOOLBAR */}
      <div className="graph-toolbar" ref={toolbarRef}>
        <button onClick={() => fitToSafeArea(500)} title="Fit graph to the uncovered area">Fit view</button>
        <button onClick={releaseAll} disabled={pinnedCount === 0} title="Release all pinned nodes">
          Release pins{pinnedCount > 0 ? ` (${pinnedCount})` : ''}
        </button>
        <span className="graph-stats">{graphData.nodes.length} nodes · {graphData.links.length} edges</span>
        {focusRelations.length > 0 && (
          <span className="focus-chip" title="Relation types this question asks about (detected from the question); their edges are emphasised">
            Focus
            {focusRelations.map((r) => (
              <span key={r} className="focus-rel" style={{ color: colorOf(r), borderColor: withAlpha(colorOf(r), 0.6), background: withAlpha(colorOf(r), 0.12) }}>{r}</span>
            ))}
          </span>
        )}
        <div className="graph-hint">Drag to pin · Right-click to release · Click to inspect</div>
      </div>

      {/* MINIMIZABLE FORCE DYNAMICS OVERLAY */}
      <div className="ontology-overlay" style={{ zIndex: 10 }} ref={legendRef}>
        <h4
          className="ontology-header"
          style={{ display: 'flex', justifyContent: 'space-between', cursor: 'pointer', userSelect: 'none' }}
          onClick={() => setIsLegendOpen(!isLegendOpen)}
        >
          <span>ONTOLOGICAL BASIS VECTORS</span>
          <span style={{ color: '#38bdf8', fontSize: '1.2rem', lineHeight: '0.8' }}>
            {isLegendOpen ? '−' : '+'}
          </span>
        </h4>

        {isLegendOpen && (
          <div className="ontology-content">
            {Object.entries(FORCE_ONTOLOGY).map(([catKey, category]) => (
              <div key={catKey} className="ontology-group">
                <div className="ontology-group-title">{category.title}</div>
                {Object.entries(category.relations).map(([relKey, data]) => {
                  const count = graphData.relationCounts?.[relKey] || 0
                  return (
                    <div
                      key={relKey}
                      className={`ontology-item${count === 0 ? ' is-empty' : ''}`}
                      onMouseEnter={() => setHoveredRel(relKey)}
                      onMouseLeave={() => setHoveredRel(null)}
                    >
                      <div className="ontology-pill">
                        <div className="ontology-color-bar" style={{ backgroundColor: data.color }}></div>
                        <span className="ontology-rel-name">{relKey}</span>
                        <span className="ontology-count">{count}</span>
                      </div>

                      {hoveredRel === relKey && (
                        <div className="ontology-tooltip">
                          <div className="ontology-math">{data.math}</div>
                          <div className="ontology-desc">{data.desc}</div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* NODE INSPECTOR */}
      {selectedRelations && (
        <div className="node-inspector" ref={inspectorRef}>
          <div className="node-inspector-header">
            <span className="node-inspector-title">{prettify(selectedId)}</span>
            <button className="node-inspector-close" onClick={() => selectNode(null)} title="Close">×</button>
          </div>
          <div className="node-inspector-sub">{selectedRelations.length} relation{selectedRelations.length === 1 ? '' : 's'}</div>
          <div className="node-inspector-list">
            {selectedRelations.map(({ link, outgoing, other }, i) => (
              <div key={i} className="node-rel-row">
                <div className="node-rel-bar" style={{ backgroundColor: colorOf(link.relation) }} />
                <div className="node-rel-body">
                  <div className="node-rel-head">
                    <span className="node-rel-dir">{outgoing ? '→' : '←'}</span>
                    <span className="node-rel-type" style={{ color: colorOf(link.relation) }}>{link.relation}</span>
                    <button className="node-rel-target" onClick={() => selectNode(other)}>{prettify(other)}</button>
                  </div>
                  <div className="node-rel-detail">{link.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* STRICTLY BOUNDED PHYSICS CANVAS */}
      <div
        ref={containerRef}
        className="graph-canvas"
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={handlePointerUp}
        onPointerLeave={handlePointerLeave}
        onContextMenu={handleContextMenu}
      >
        {isReady && graphData.nodes.length === 0 && (
          <div className="empty-state graph-empty">
            <p>No graph relationships to display.</p>
          </div>
        )}
        {/* Never start the force layout with zero nodes */}
        {isReady && graphData.nodes.length > 0 && (
          <ForceGraph2D
            key={canvasKey}
            width={dimensions.width}
            height={dimensions.height}
            ref={fgRef}
            graphData={graphData}
            backgroundColor="rgba(0,0,0,0)"

            // Smooth, Obsidian-like settling
            d3AlphaDecay={0.025}
            d3VelocityDecay={0.32}
            cooldownTime={6000}
            // Auto-fit only once per new graph — not after every drag
            onEngineStop={() => {
              if (fittedRef.current !== graphData) {
                fittedRef.current = graphData
                fitToSafeArea(600)
              }
            }}

            nodeRelSize={1}
            nodeVal={(n) => n.radius * n.radius} // keeps arrowheads on the node rim
            nodeCanvasObject={nodeCanvasObject}

            // Pointer picking/drag handled by our geometric layer on the container (see section 5);
            // force-graph keeps only zoom/pan
            enablePointerInteraction={false}
            enableNodeDrag={false}

            linkColor={linkColor}
            linkWidth={linkWidth}
            linkCurvature="curvature"

            linkDirectionalArrowLength={5}
            linkDirectionalArrowRelPos={1}
            linkDirectionalParticles={linkParticles}
            linkDirectionalParticleSpeed={0.006}
            linkDirectionalParticleWidth={2.5}
          />
        )}
      </div>

      {/* HOVER TOOLTIP (positioned imperatively in handlePointerMove) */}
      <div
        ref={tooltipRef}
        className="graph-tooltip graph-tooltip-float"
        style={{ display: hoverNode || hoverLink ? 'block' : 'none' }}
      >
        {hoverNode ? (
          <>
            <div className="graph-tooltip-path">{prettify(hoverNode.id)}</div>
            <div className="graph-tooltip-detail">
              {hoverNode.degree} connection{hoverNode.degree === 1 ? '' : 's'}{hoverNode.fx != null ? ' · pinned' : ''}
            </div>
          </>
        ) : hoverLink ? (
          <>
            <div className="graph-tooltip-rel" style={{ color: colorOf(hoverLink.relation) }}>{hoverLink.relation}</div>
            <div className="graph-tooltip-path">
              {prettify(getId(hoverLink.source))} → {prettify(getId(hoverLink.target))}
            </div>
            <div className="graph-tooltip-detail">{hoverLink.detail}</div>
          </>
        ) : null}
      </div>
    </div>
  )
}
