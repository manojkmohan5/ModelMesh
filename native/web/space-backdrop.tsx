import React, { memo, useEffect, useRef } from "react"
import { reducedMotionQuery } from "./motion"
import "./space-backdrop.css"

type MeshNode = { x: number; y: number; vx: number; vy: number; r: number }
type Signal = { from: number; to: number; progress: number; duration: number }

const LINK = 150 // Nodes closer than this many pixels are joined by an edge.
const FRAME = 1000 / 30 // A calm 30 fps is plenty for a backdrop.

function palette() {
  const style = getComputedStyle(document.documentElement)
  const read = (name: string, fallback: string) => style.getPropertyValue(name).trim() || fallback
  return { node: read("--mm-node", "196, 181, 253"), edge: read("--mm-edge", "167, 139, 250"), pulse: read("--mm-pulse", "240, 171, 252") }
}

/** Model nodes drifting in a mesh, with signals routed along its edges. Decorative only. */
function NeuralMesh() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = canvasRef.current, context = canvas?.getContext("2d")
    if (!canvas || !context) return
    const motion = reducedMotionQuery()
    let colors = palette(), nodes: MeshNode[] = [], signals: Signal[] = []
    let width = 0, height = 0, raf = 0, last = 0, nextSignal = 0, onScreen = true

    const draw = () => {
      context.clearRect(0, 0, width, height)
      context.lineWidth = 1
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j], distance = Math.hypot(a.x - b.x, a.y - b.y)
        if (distance >= LINK) continue
        context.strokeStyle = `rgba(${colors.edge}, ${((1 - distance / LINK) * .3).toFixed(3)})`
        context.beginPath(); context.moveTo(a.x, a.y); context.lineTo(b.x, b.y); context.stroke()
      }
      context.fillStyle = `rgba(${colors.node}, .75)`
      for (const node of nodes) { context.beginPath(); context.arc(node.x, node.y, node.r, 0, Math.PI * 2); context.fill() }
      for (const signal of signals) {
        const a = nodes[signal.from], b = nodes[signal.to], t = signal.progress
        const x = a.x + (b.x - a.x) * t, y = a.y + (b.y - a.y) * t, tail = Math.max(0, t - .2)
        const fade = Math.sin(Math.PI * t)
        context.strokeStyle = `rgba(${colors.pulse}, ${(.75 * fade).toFixed(3)})`
        context.lineWidth = 1.4
        context.beginPath(); context.moveTo(a.x + (b.x - a.x) * tail, a.y + (b.y - a.y) * tail); context.lineTo(x, y); context.stroke()
        const glow = context.createRadialGradient(x, y, 0, x, y, 9)
        glow.addColorStop(0, `rgba(${colors.pulse}, ${fade.toFixed(3)})`)
        glow.addColorStop(1, `rgba(${colors.pulse}, 0)`)
        context.fillStyle = glow
        context.beginPath(); context.arc(x, y, 9, 0, Math.PI * 2); context.fill()
      }
      context.lineWidth = 1
    }

    const spawn = () => {
      for (let attempt = 0; attempt < 8; attempt++) {
        const from = Math.floor(Math.random() * nodes.length), origin = nodes[from]
        const near = nodes.flatMap((node, index) => index !== from && Math.hypot(node.x - origin.x, node.y - origin.y) < LINK * .9 ? [index] : [])
        if (near.length) {
          signals.push({ from, to: near[Math.floor(Math.random() * near.length)], progress: 0, duration: 1100 + Math.random() * 900 })
          return
        }
      }
    }

    const step = (elapsed: number) => {
      for (const node of nodes) {
        node.x += node.vx * elapsed / 16; node.y += node.vy * elapsed / 16
        if (node.x < 0 || node.x > width) { node.vx *= -1; node.x = Math.min(Math.max(node.x, 0), width) }
        if (node.y < 0 || node.y > height) { node.vy *= -1; node.y = Math.min(Math.max(node.y, 0), height) }
      }
      signals = signals.filter(signal => (signal.progress += elapsed / signal.duration) < 1)
      nextSignal -= elapsed
      if (nextSignal <= 0 && signals.length < 6 && nodes.length) { spawn(); nextSignal = 420 + Math.random() * 700 }
    }

    const loop = (now: number) => {
      raf = requestAnimationFrame(loop)
      const elapsed = now - last
      if (elapsed < FRAME) return
      last = now
      step(Math.min(elapsed, 100))
      draw()
    }
    const stop = () => { cancelAnimationFrame(raf); raf = 0 }
    const start = () => {
      if (raf || !onScreen || document.hidden || motion.matches || !width) return
      last = performance.now()
      raf = requestAnimationFrame(loop)
    }
    // Always paint a still frame; animate only when visible and motion is allowed.
    const sync = () => { stop(); draw(); start() }

    const resize = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      width = canvas.clientWidth; height = canvas.clientHeight
      if (!width || !height) return
      canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio)
      context.setTransform(ratio, 0, 0, ratio, 0, 0)
      const count = Math.max(14, Math.min(54, Math.round(width * height / 26000)))
      nodes = Array.from({ length: count }, () => ({
        x: Math.random() * width, y: Math.random() * height,
        vx: (Math.random() - .5) * .22, vy: (Math.random() - .5) * .22, r: 1 + Math.random() * 1.3,
      }))
      signals = []
      sync()
    }

    const resizer = new ResizeObserver(resize)
    const viewer = new IntersectionObserver(([entry]) => { onScreen = entry?.isIntersecting ?? true; if (onScreen) start(); else stop() })
    const theme = new MutationObserver(() => { colors = palette(); if (!raf) draw() })
    const visibility = () => { if (document.hidden) stop(); else start() }
    resizer.observe(canvas)
    viewer.observe(canvas)
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] })
    document.addEventListener("visibilitychange", visibility)
    motion.addEventListener("change", sync)
    return () => {
      stop()
      resizer.disconnect(); viewer.disconnect(); theme.disconnect()
      document.removeEventListener("visibilitychange", visibility)
      motion.removeEventListener("change", sync)
    }
  }, [])
  return <canvas ref={canvasRef} className="neural-mesh" />
}

export const SpaceBackdrop = memo(function SpaceBackdrop({ variant = "home" }: { variant?: "home" | "chat" }) {
  return <div className={`space-backdrop ${variant}-space`} aria-hidden="true">
    {variant === "home" && <><div className="space-aurora" /><NeuralMesh /></>}
  </div>
})
