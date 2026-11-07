import React, { useId } from "react"
import { SelectTrigger } from "@/components/ui/select"

/** A select trigger whose name is its label followed by the visible value (WCAG 2.5.3). */
export function NamedSelectTrigger({ label, id, children, ...props }: { label: string } & React.ComponentProps<typeof SelectTrigger>) {
  const own = useId(), triggerId = id ?? own
  return <>
    <span id={`${triggerId}-label`} hidden>{label}</span>
    <SelectTrigger id={triggerId} aria-labelledby={`${triggerId}-label ${triggerId}`} {...props}>{children}</SelectTrigger>
  </>
}
