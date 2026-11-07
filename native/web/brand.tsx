import React, { useId } from "react"
import { PanelLeftClose, PanelLeftOpen } from "lucide-react"
import { Button } from "@/components/ui/button"
import "./brand.css"

/** The ModelMesh mark: a router node meshed with three model nodes. Decorative; always pair it with text. */
export function BrandMark({ size = 20, className = "" }: { size?: number; className?: string }) {
  const gradient = `mm-mark-${useId().replace(/[^A-Za-z0-9_-]/g, "")}`
  const paint = `url(#${gradient})`
  return <svg className={`brand-mark ${className}`.trim()} width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false">
    <defs>
      <linearGradient id={gradient} x1="3" y1="2" x2="21" y2="22" gradientUnits="userSpaceOnUse">
        <stop offset="0" stopColor="#8b5cf6" /><stop offset=".55" stopColor="#d946ef" /><stop offset="1" stopColor="#22d3ee" />
      </linearGradient>
    </defs>
    <g stroke={paint} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3.6 20.4 18.2H3.6Z" strokeOpacity=".6" />
      <path d="M12 12.6V3.6M12 12.6l8.4 5.6M12 12.6l-8.4 5.6" />
    </g>
    <g fill={paint}>
      <circle className="brand-node" cx="12" cy="3.6" r="2.2" />
      <circle className="brand-node" cx="20.4" cy="18.2" r="2.2" />
      <circle className="brand-node" cx="3.6" cy="18.2" r="2.2" />
    </g>
    <circle className="brand-core" cx="12" cy="12.6" r="2.8" fill={paint} />
  </svg>
}

export function Brand({ onClick }: { onClick?: React.MouseEventHandler<HTMLAnchorElement> }) {
  return <a href="#" className="brand" onClick={onClick}>
    <BrandMark />
    <span className="brand-name">ModelMesh</span>
  </a>
}

export function SidebarToggle({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const label = collapsed ? "Open sidebar" : "Close sidebar"
  return <Button className="sidebar-toggle" variant="ghost" size="icon-sm" aria-label={label} title={label} aria-expanded={!collapsed} onClick={onToggle}>
    {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
  </Button>
}
