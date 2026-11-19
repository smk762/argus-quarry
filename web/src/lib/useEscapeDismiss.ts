import { useEffect, useState, type FocusEvent } from "react";

/**
 * WCAG 1.4.13: content shown on hover or focus can be dismissed with Escape without moving the pointer or the focus.
 * Spread `bind` on the element whose hover or focus shows the content, and hide the content while `dismissed`. It can
 * show again once the pointer and the focus have both left.
 *
 * Escape is heard on the document, so it works while only the pointer is over the trigger.
 */
export function useEscapeDismiss() {
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const active = hovered || focused;

  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDismissed(true);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [active]);

  const bind = {
    onMouseEnter: () => setHovered(true),
    onMouseLeave: () => {
      setHovered(false);
      if (!focused) setDismissed(false);
    },
    onFocus: () => setFocused(true),
    onBlur: (event: FocusEvent<HTMLElement>) => {
      if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
      setFocused(false);
      if (!hovered) setDismissed(false);
    },
  };
  return { dismissed, bind };
}
