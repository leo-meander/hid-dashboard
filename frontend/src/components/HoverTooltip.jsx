import { useLayoutEffect, useRef, useState } from "react";

/**
 * Hover tooltip for showing a number's working.
 *
 * Fixed positioning on purpose: the KPI grid and the forecast tables both sit
 * inside `overflow-x-auto` wrappers, and an absolutely-positioned panel is
 * clipped by them. It measures the trigger on enter and pins itself above it.
 *
 * Lifted out of Home.jsx so the forecast cards on Fill Pace can show their
 * arithmetic the same way the KPI forecast already does — one tooltip style
 * across the app, one place to change it.
 *
 * Centring on the trigger runs the panel off the screen when the trigger is a
 * right-aligned table cell, which is where most of these live — so after it
 * renders, it is nudged back inside the viewport.
 */
export default function HoverTooltip({ children, content, className = "", width = "w-80" }) {
  const [show, setShow] = useState(false);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const tip = useRef(null);

  useLayoutEffect(() => {
    if (!show || !tip.current) return;
    const r = tip.current.getBoundingClientRect();
    const pad = 8;
    const dx = r.left < pad
      ? pad - r.left
      : r.right > window.innerWidth - pad
        ? window.innerWidth - pad - r.right
        : 0;
    if (dx) setPos((p) => ({ ...p, x: p.x + dx }));
  }, [show]);
  const onEnter = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setPos({ x: r.left + r.width / 2, y: r.top });
    setShow(true);
  };
  const onLeave = () => setShow(false);
  return (
    <span
      className={"cursor-help " + className}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      {children}
      {show && (
        <div
          ref={tip}
          className={`fixed z-50 ${width} p-3 bg-gray-900 text-white text-[11px] leading-relaxed rounded-lg shadow-xl pointer-events-none text-left`}
          style={{ left: pos.x, top: pos.y - 10, transform: "translate(-50%, -100%)" }}
        >
          {content}
        </div>
      )}
    </span>
  );
}
