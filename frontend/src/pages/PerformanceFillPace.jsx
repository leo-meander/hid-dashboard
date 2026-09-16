/**
 * Fill Pace — how fast a stay month is filling, and whether that is faster or
 * slower than the same run-up one year earlier.
 *
 * Every other Performance page reads stays that already happened. This one
 * reads the opposite axis: pick a stay month, look back over a booking window,
 * and watch the month fill. The comparison line is last year's countdown to the
 * same month, aligned by distance from the month rather than calendar date —
 * so 60 days before December 2026 sits against 60 days before December 2025.
 *
 * Two words do the work, and the page keeps them apart on purpose:
 *   On the books — room-nights sold for the month as of a date. The position.
 *   Pickup       — room-nights added inside the window. The slope, i.e. speed.
 *
 * A mature month can sit high on the books while barely moving; a new one can
 * be filling fast from nothing. Only the second is pace, which is why the
 * verdict is read off pickup and not off occupancy.
 */
import { useMemo, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import axios from "axios";
import {
  LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import SyncBadge from "../components/SyncBadge";
import { useBranch } from "../context/BranchContext";
import HoverTooltip from "../components/HoverTooltip";

const THIS_YEAR = "#4f46e5";   // indigo-600
const LAST_YEAR = "#f59e0b";   // amber-500

const WINDOWS = [30, 60, 90, 180];
// Mirrors MAX_WINDOW_DAYS on the endpoint. A custom range longer than this is
// clamped server-side, so the page says so rather than showing a range it is
// not actually reading.
const MAX_WINDOW_DAYS = 365;
// Mirrors MAX_STAY_MONTHS on the endpoint, which refuses a longer list.
const MAX_STAY_MONTHS = 12;
// Days in the trailing mean on the speed chart.
const SMOOTHING_DAYS = 7;

// ── dates ────────────────────────────────────────────────────────────────────
// All arithmetic goes through UTC midnight so a range never drifts by a day for
// a user sitting in a timezone behind or ahead of the server.
const isoOf = (d) => d.toISOString().slice(0, 10);

function todayISO() {
  const n = new Date();
  return isoOf(new Date(Date.UTC(n.getFullYear(), n.getMonth(), n.getDate())));
}

function shiftISO(iso, n) {
  const [y, m, d] = iso.split("-").map(Number);
  return isoOf(new Date(Date.UTC(y, m - 1, d + n)));
}

function daysBetweenISO(from, to) {
  const at = (s) => {
    const [y, m, d] = s.split("-").map(Number);
    return Date.UTC(y, m - 1, d);
  };
  return Math.round((at(to) - at(from)) / 86400000);
}

function ymd(iso) {
  if (typeof iso !== "string") return null;
  const parts = iso.split("-").map(Number);
  return parts.length === 3 && !parts.some(Number.isNaN) ? parts : null;
}

function longDate(iso) {
  const parts = ymd(iso);
  if (!parts) return "";
  const [y, m, d] = parts;
  return new Date(y, m - 1, d).toLocaleDateString("en-GB", {
    day: "2-digit", month: "short", year: "numeric",
  });
}

// ── formatting ───────────────────────────────────────────────────────────────
const nf = new Intl.NumberFormat("en-US");

function nights(v) {
  if (v === null || v === undefined) return "—";
  return nf.format(Math.round(v));
}

function occ(v) {
  if (v === null || v === undefined) return "—";
  return `${v.toFixed(1)}%`;
}

/** A signed figure whose unit is carried by its column header, not repeated
 *  on every row — "pts" beside a number reads as "%" to anyone who has not met
 *  the distinction, and the two differ by a factor of twenty-five here. */
function signedNumber(v) {
  if (v === null || v === undefined) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}`;
}

/**
 * The same gap in words. "pts" is the correct unit and an unfamiliar one, and
 * a reader who takes it for "%" is out by a factor of twenty-five here: the gap
 * between 4.1% and 19.2% is 15.1 points of fill, or +368% relative.
 */
function gapLabel(v) {
  if (v === null || v === undefined) return "no year-ago figure to compare";
  const size = Math.abs(v).toFixed(1);
  if (Math.abs(v) < 0.05) return "level with last year";
  return `${size} points of fill ${v > 0 ? "ahead of" : "behind"} last year`;
}

/** Relative change. null means there was no year-ago base to divide by. */
function pctLabel(v) {
  if (v === null || v === undefined) return "no base";
  return `${v > 0 ? "+" : ""}${v.toFixed(0)}%`;
}

function money(v, currency) {
  if (v === null || v === undefined) return "—";
  const n = Math.round(v);
  if (!currency) return nf.format(n);
  if (currency === "VND") return `${nf.format(n)}₫`;
  return `${currency} ${nf.format(n)}`;
}

/**
 * Money at a glance. 77,814,114,009₫ is unreadable in a headline and the
 * digits past the second one are noise at this scale, so the card rounds to a
 * suffix and keeps the exact figure for the line underneath it.
 */
function shortMoney(v, currency) {
  if (v === null || v === undefined) return "—";
  const n = Math.abs(v);
  const sign = v < 0 ? "-" : "";
  const unit = currency === "VND" ? "₫" : currency ? `${currency} ` : "";
  const body = (value, suffix) => {
    const shown = value >= 100 ? Math.round(value) : value.toFixed(1).replace(/\.0$/, "");
    return `${shown}${suffix}`;
  };
  let out;
  if (n >= 1e9) out = body(n / 1e9, "B");
  else if (n >= 1e6) out = body(n / 1e6, "M");
  else if (n >= 1e3) out = body(n / 1e3, "K");
  else out = String(Math.round(n));
  return currency === "VND" ? `${sign}${out}${unit}` : `${sign}${unit}${out}`;
}

function toneFor(v, deadband = 0) {
  if (v === null || v === undefined) return "text-gray-400";
  if (v > deadband) return "text-emerald-600";
  if (v < -deadband) return "text-red-600";
  return "text-gray-500";
}

function monthLabel(ym) {
  if (!ym) return "";
  const [y, m] = ym.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleString("en-GB", { month: "long", year: "numeric" });
}

function shortDate(iso) {
  const parts = ymd(iso);
  if (!parts) return typeof iso === "number" ? String(iso) : "";
  const [, m, d] = iso.split("-");
  return `${d}/${m}`;
}

function shortMonth(ym) {
  if (!ym) return "";
  const [y, m] = ym.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleString("en-GB", { month: "short" });
}

/** Stay months on offer, grouped by year: six back, fourteen forward. Past
 *  months are still worth opening — that is how you check whether a pace read
 *  came true. */
function monthGroups() {
  const now = new Date();
  const byYear = new Map();
  for (let i = -6; i <= 14; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() + i, 1);
    const year = d.getFullYear();
    const ym = `${year}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    if (!byYear.has(year)) byYear.set(year, []);
    byYear.get(year).push({
      value: ym,
      label: monthLabel(ym) + (i < 0 ? " (past)" : ""),
    });
  }
  return [...byYear.entries()].map(([year, options]) => ({
    group: String(year),
    options,
  }));
}

function defaultMonth() {
  const d = new Date();
  const n = new Date(d.getFullYear(), d.getMonth() + 1, 1);
  return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, "0")}`;
}

/**
 * What to call the stay period in prose. One month gets its name; a run of
 * consecutive months gets a range; anything else gets a count, because
 * "Feb, Jun and Nov 2027" in the middle of a sentence reads worse than "3
 * stay months" and the picker already says which.
 */
function monthsLabel(list) {
  if (!list?.length) return "";
  if (list.length === 1) return monthLabel(list[0]);

  const idx = (ym) => {
    const [y, m] = ym.split("-").map(Number);
    return y * 12 + m;
  };
  const consecutive = list.every((ym, i) => i === 0 || idx(ym) === idx(list[i - 1]) + 1);
  if (!consecutive) return `${list.length} stay months`;

  const first = list[0];
  const last = list[list.length - 1];
  const sameYear = first.slice(0, 4) === last.slice(0, 4);
  return sameYear
    ? `${shortMonth(first)}–${shortMonth(last)} ${first.slice(0, 4)}`
    : `${shortMonth(first)} ${first.slice(0, 4)}–${shortMonth(last)} ${last.slice(0, 4)}`;
}

// Built once: the option list is fixed for the session, and a stable array
// identity keeps the picker from re-rendering on every unrelated keystroke.
const MONTH_GROUPS = monthGroups();

// ── verdict ──────────────────────────────────────────────────────────────────
/**
 * The one-line answer, read off pickup — the room-nights the window itself
 * added — not off occupancy, which mostly reflects how long the month has been
 * on sale. A missing pace index means last year booked nothing in this stretch,
 * and there is no speed to be faster than.
 */
function verdict(d) {
  if (!d?.vs_last_year) return null;
  const { pace_index, pickup_room_nights_pct } = d.vs_last_year;
  const picked = d.current?.pickup_room_nights || 0;

  // A verdict needs something settled to judge against. When last year's month
  // is itself still selling, "2.4x the pace" is two moving numbers divided by
  // each other, and stating it in green would be the page's loudest lie.
  const status = d.last_year?.status;
  if (status && status !== "finished") {
    return {
      tone: "neutral",
      headline: "Nothing to judge yet — last year has not settled",
      detail: `${nights(picked)} room-nights booked in this window against ${nights(d.last_year.pickup_room_nights)} in the same countdown last year. But ${monthsLabel(d.last_year.unfinished_stay_months)} is still taking bookings, so that gap is between two unfinished numbers.`,
    };
  }

  if (pace_index === null || pace_index === undefined) {
    return picked > 0
      ? {
          tone: "neutral",
          headline: "No year-ago pace to compare with",
          detail: `${nights(picked)} room-nights picked up this window. The same countdown to ${monthsLabel(d.last_year?.stay_months)} booked nothing, so there is no speed to be faster than.`,
        }
      : {
          tone: "neutral",
          headline: "Nothing picked up in this window",
          detail: `Neither this year nor the same countdown to ${monthsLabel(d.last_year?.stay_months)} booked anything. Widen the window or check a different source.`,
        };
  }

  const pctMore = pickup_room_nights_pct;
  if (pace_index >= 1.05) {
    return {
      tone: "up",
      headline: `Filling faster than last year — ${pace_index.toFixed(2)}× the pace`,
      detail: `${nights(picked)} room-nights booked in this window against ${nights(d.last_year.pickup_room_nights)} in the same countdown last year (${pctLabel(pctMore)}).`,
    };
  }
  if (pace_index <= 0.95) {
    return {
      tone: "down",
      headline: `Filling slower than last year — ${pace_index.toFixed(2)}× the pace`,
      detail: `${nights(picked)} room-nights booked in this window against ${nights(d.last_year.pickup_room_nights)} in the same countdown last year (${pctLabel(pctMore)}).`,
    };
  }
  return {
    tone: "flat",
    headline: "Filling at about last year's pace",
    detail: `${nights(picked)} room-nights booked in this window against ${nights(d.last_year.pickup_room_nights)} in the same countdown last year (${pctLabel(pctMore)}).`,
  };
}

/**
 * The same question asked of the window before this one instead of last year.
 *
 * The raw ratio is never the verdict on its own: a window beats its predecessor
 * whatever anyone does, because bookings crowd towards check-in. Where last
 * year says what that stretch normally runs, the verdict is the raw figure
 * divided by it. Where it does not, the page says the number is unchecked
 * rather than dressing it as a win.
 */
function previousPeriodVerdict(d) {
  const vs = d?.vs_previous_period;
  if (!vs) return null;
  const picked = d.current?.pickup_room_nights || 0;
  const before = d.previous_period?.pickup_room_nights || 0;
  const both = `${nights(picked)} room-nights this window against ${nights(before)} in the ${d.days} days before it`;

  if (!vs.acceleration) {
    return {
      tone: "neutral",
      headline: "Nothing booked in the previous window",
      detail: `${both}. With nothing before it there is no rate to have sped up from.`,
    };
  }
  if (!vs.natural_acceleration) {
    return {
      tone: "neutral",
      headline: `${vs.acceleration.toFixed(2)}× the previous window — unchecked`,
      detail: `${both}. Some of that is simply check-in getting closer, and with no year-ago volume for this month there is nothing to say how much. Treat it as a direction, not a result.`,
    };
  }
  const excess = vs.excess_acceleration;
  const norm = `The same stretch ran ${vs.natural_acceleration.toFixed(2)}× last year, so the part that is not just the calendar is ${excess.toFixed(2)}×.`;
  if (excess >= 1.05) {
    return {
      tone: "up",
      headline: `Ahead of the seasonal norm — ${excess.toFixed(2)}× after allowing for it`,
      detail: `${both}, a raw ${vs.acceleration.toFixed(2)}×. ${norm}`,
    };
  }
  if (excess <= 0.95) {
    return {
      tone: "down",
      headline: `Behind the seasonal norm despite a raw ${vs.acceleration.toFixed(2)}×`,
      detail: `${both}. ${norm}`,
    };
  }
  return {
    tone: "flat",
    headline: "Moving with the seasonal norm, not against it",
    detail: `${both}, a raw ${vs.acceleration.toFixed(2)}×. ${norm}`,
  };
}

const VERDICT_STYLE = {
  up:      "bg-emerald-50 border-emerald-200 text-emerald-900",
  down:    "bg-red-50 border-red-200 text-red-900",
  flat:    "bg-gray-50 border-gray-200 text-gray-800",
  neutral: "bg-amber-50 border-amber-200 text-amber-900",
};

// ── small pieces ─────────────────────────────────────────────────────────────

function Stat({ label, value, sub, subTone = "text-gray-500", hint }) {
  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4">
      <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-bold text-gray-900 mt-1 tabular-nums">{value}</div>
      {sub && <div className={`text-sm mt-0.5 tabular-nums ${subTone}`}>{sub}</div>}
      {hint && <div className="text-xs text-gray-400 mt-1 leading-snug">{hint}</div>}
    </div>
  );
}

/**
 * A labelled control. Deliberately a <div> and not a <label>: the source picker
 * below is a popover whose click-away layer covers the viewport, and inside a
 * label a click on that layer gets forwarded to the first checkbox in the
 * popover instead of closing it — the menu became impossible to dismiss.
 */
function Field({ label, children }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium text-gray-500">{label}</span>
      {children}
    </div>
  );
}

const SELECT_CLS =
  "px-3 py-1.5 rounded-lg text-sm border border-gray-200 bg-white text-gray-700 " +
  "focus:outline-none focus:ring-2 focus:ring-indigo-200";

/**
 * Multi-select over grouped options, used for both stay months and sources.
 *
 * A plain <select> could only hold one of each, and both filters need several:
 * "how is Q4 filling" is one question about three months, and "our own website"
 * is a different question from "everything we booked directly". So every option
 * is its own checkbox, under a group header that ticks all of its options at
 * once — a year, or a source category.
 */
function MultiPicker({ groups, selected, emptyLabel, summarise,
                       onToggle, onToggleGroup, onClear, width = "13rem" }) {
  const [open, setOpen] = useState(false);

  const label = selected.length === 0 ? emptyLabel : summarise(selected);

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className={`${SELECT_CLS} flex items-center justify-between gap-2 text-left`}
        style={{ minWidth: width }}
      >
        <span className={selected.length ? "text-gray-800" : "text-gray-500"}>{label}</span>
        <span className="text-gray-400 text-xs">▾</span>
      </button>

      {open && (
        <>
          {/* Click-away layer, so the popover closes like a native select. */}
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute z-20 mt-1 w-72 max-h-80 overflow-y-auto bg-white border
                          border-gray-200 rounded-lg shadow-lg py-1">
            {onClear && (
              <button
                onClick={onClear}
                className={`w-full text-left px-3 py-1.5 text-sm hover:bg-gray-50 ${
                  selected.length === 0 ? "text-indigo-600 font-medium" : "text-gray-600"
                }`}
              >
                {emptyLabel}
              </button>
            )}

            {groups.map(({ group, options }) => {
              const values = options.map((o) => o.value);
              const allOn = values.every((v) => selected.includes(v));
              return (
                <div key={group} className="border-t border-gray-100 mt-1 pt-1">
                  <button
                    onClick={() => onToggleGroup(values)}
                    className="w-full flex items-center justify-between px-3 py-1
                               text-xs font-semibold uppercase tracking-wide
                               text-gray-400 hover:text-indigo-600"
                  >
                    <span>{group}</span>
                    <span className="normal-case tracking-normal font-medium">
                      {allOn ? "clear" : "all"}
                    </span>
                  </button>
                  {options.map((o) => (
                    <label key={o.value}
                      className="flex items-center gap-2 px-3 py-1.5 text-sm text-gray-700
                                 hover:bg-gray-50 cursor-pointer">
                      <input type="checkbox" className="accent-indigo-600"
                             checked={selected.includes(o.value)}
                             onChange={() => onToggle(o.value)} />
                      <span className="truncate">{o.label}</span>
                    </label>
                  ))}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

/** Shared columns for the source and branch tables — both answer "who is
 *  pacing ahead", so they read identically. */
function PaceTable({ title, subtitle, rows, nameKey, nameLabel, currency, compare }) {
  if (!rows?.length) return null;
  return (
    <div className="bg-white border border-gray-200 rounded-xl overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-100">
        <h2 className="text-sm font-semibold text-gray-800">{title}</h2>
        <p className="text-xs text-gray-500 mt-0.5">{subtitle}</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50 text-gray-500">
            <tr>
              <th className="text-left  font-medium px-4 py-2">{nameLabel}</th>
              <th className="text-right font-medium px-4 py-2">On the books</th>
              <th className="text-right font-medium px-4 py-2">Fill %</th>
              {compare && (
                <th className="text-right font-medium px-4 py-2">
                  vs LY
                  <span className="block text-[10px] font-normal text-gray-400 leading-none">
                    points of fill
                  </span>
                </th>
              )}
              <th className="text-right font-medium px-4 py-2">Pickup</th>
              {compare && <th className="text-right font-medium px-4 py-2">LY pickup</th>}
              {compare && <th className="text-right font-medium px-4 py-2">Pace</th>}
              <th className="text-right font-medium px-4 py-2">Revenue on books</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((r) => {
              const idx = r.vs_last_year?.pace_index;
              // Branch rows carry their own currency; a channel row inherits the
              // scope's, and falls back to VND when the scope mixes several —
              // an unlabelled sum of TWD and VND would be a meaningless number.
              const rowCurrency = r.currency || currency;
              const revenue = rowCurrency
                ? money(r.otb_revenue_native, rowCurrency)
                : money(r.otb_revenue_vnd, "VND");
              return (
                <tr key={r[nameKey]} className="hover:bg-gray-50">
                  <td className="px-4 py-2 text-gray-800">
                    {r[nameKey]}
                    {r.category && r.category !== r[nameKey] && (
                      <span className="ml-2 text-xs text-gray-400">{r.category}</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-gray-800">
                    {nights(r.otb_room_nights)}
                  </td>
                  <td className="px-4 py-2 text-right tabular-nums text-gray-600">
                    {occ(r.otb_occ_pct)}
                  </td>
                  {compare && (
                    <td className={`px-4 py-2 text-right tabular-nums ${toneFor(r.vs_last_year?.otb_occ_pts, 0.5)}`}>
                      {signedNumber(r.vs_last_year?.otb_occ_pts)}
                    </td>
                  )}
                  <td className="px-4 py-2 text-right tabular-nums text-gray-800">
                    {nights(r.pickup_room_nights)}
                  </td>
                  {compare && (
                    <td className="px-4 py-2 text-right tabular-nums text-gray-500">
                      {nights(r.last_year?.pickup_room_nights)}
                    </td>
                  )}
                  {compare && (
                    <td className={`px-4 py-2 text-right tabular-nums font-medium ${
                      idx === null || idx === undefined ? "text-gray-400"
                        : idx >= 1.05 ? "text-emerald-600"
                        : idx <= 0.95 ? "text-red-600" : "text-gray-500"
                    }`}>
                      {idx === null || idx === undefined ? "no base" : `${idx.toFixed(2)}×`}
                    </td>
                  )}
                  <td className="px-4 py-2 text-right tabular-nums text-gray-600">
                    {revenue}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── forecast ─────────────────────────────────────────────────────────────────

/**
 * The tooltips that show a projected number's arithmetic.
 *
 * Every figure on these cards is the end of a chain — on the books, plus last
 * year's remaining pickup, priced at a rate derived from two other numbers —
 * and a chain nobody can follow is a number nobody should act on. The same
 * hover panel the KPI forecast uses on Home, with the same job: the inputs,
 * the operation, the result, in the order they happen.
 */
/**
 * What kind of number this is. Cards here put revenue that has been earned
 * next to revenue that has been projected, in the same typeface at the same
 * size, and only a paragraph at the bottom said which was which — so a
 * projection could be read as money in the bank. Every figure now carries its
 * own provenance.
 */
function Tag({ kind }) {
  const style = {
    actual: "bg-gray-100 text-gray-600",
    projected: "bg-indigo-50 text-indigo-700",
    both: "bg-indigo-50 text-indigo-700",
    target: "bg-amber-50 text-amber-700",
  }[kind];
  const label = { actual: "actual", projected: "projected",
                  both: "actual + projected", target: "from target" }[kind];
  return (
    <span className={`ml-1.5 align-middle text-[10px] font-medium px-1.5 py-px rounded ${style}`}>
      {label}
    </span>
  );
}

function TipRow({ label, children, strong, note }) {
  return (
    <div className={`flex items-baseline gap-2 ${strong ? "text-white font-semibold" : ""}`}>
      <span className={strong ? "" : "text-gray-300"}>{label}</span>
      <span className="ml-auto font-mono whitespace-nowrap">{children}</span>
      {note && <span className="text-gray-500">{note}</span>}
    </div>
  );
}

const POINTS_TIP = {
  otb: ["OCC on the books", "room-nights sold ÷ room-nights the house has"],
  speed: ["At this speed", "room-nights a day now × days left to sell"],
  needed: ["Needed for target", "the occupancy the revenue target implies at today's rate"],
};

/** How a set of branch-months reaches one of its three point figures. */
function pointsWorking(cells, block, which) {
  const [title, sub] = POINTS_TIP[which];
  const line = (c) => {
    const r = c.run_rate;
    const cap = c.available_room_nights;
    if (which === "otb")
      return `${nights(r.otb_room_nights)} of ${nights(cap)} = ${occ(r.otb_occ_pct)}`;
    if (which === "speed")
      return `${r.room_nights_per_day.toFixed(2)}/d × ${r.days_left}d = ${nights(r.room_nights_added)} = +${occ(r.points_added)}`;
    return r.needed_occ_pct == null
      ? "—"
      : `${nights(r.needed_room_nights)} of ${nights(cap)} = ${occ(r.needed_occ_pct)}`;
  };
  const total = { otb: block.otb_occ_pct, speed: block.points_added,
                  needed: block.needed_occ_pct }[which];
  return (
    <>
      <div className="font-semibold text-white mb-1">{title}</div>
      <div className="text-gray-300 mb-1.5">{sub}</div>
      <div className="space-y-0.5">
        {cells.map((c) => (
          <TipRow key={`${c.branch_id}-${c.stay_month}`}
                  label={`${c.branch_name.replace("MEANDER ", "")} ${monthLabel(c.stay_month).slice(0, 3)}`}>
            {line(c)}
          </TipRow>
        ))}
      </div>
      <div className="border-t border-gray-700 mt-1.5 pt-1.5">
        <TipRow label={which === "speed" ? "Adds" : "Together"} strong>
          {which === "speed" ? `+${occ(total)}` : occ(total)}
        </TipRow>
      </div>
      <div className="text-gray-500 mt-1.5">
        {which === "otb" &&
          "Everything sold so far for these months, whenever it was booked. Counted in beds, the way the KPI page counts them."}
        {which === "speed" &&
          `Room-nights a day over the ${block.window_days}-day window set above, carried flat to
           the end of each stay month. Bookings crowd towards check-in rather than arriving
           evenly, so this is the floor if nothing speeds up.`}
        {which === "needed" &&
          `Target revenue minus what is already booked, divided by the same rate the nights
           beside it are priced at — the ${block.window_days}-day window's own. Above 100% means
           a full house would still be short, which is a rate problem, not a pace one.`}
      </div>
      <div className="text-gray-500 mt-1">
        Percentages are divided out of the totals once — never averaged across months or branches.
      </div>
    </>
  );
}

/** How the projected revenue is built, per branch-month. */
function revenueWorking(cells, block, currency) {
  return (
    <>
      <div className="font-semibold text-white mb-1">Revenue at this speed</div>
      <div className="text-gray-300 mb-1.5">
        booked already + nights still to come × the rate they are selling at
      </div>
      <div className="space-y-1">
        {cells.filter((c) => c.run_rate.revenue_native != null).map((c) => {
          const r = c.run_rate;
          return (
            <div key={`${c.branch_id}-${c.stay_month}`}>
              <TipRow label={`${c.branch_name.replace("MEANDER ", "")} ${monthLabel(c.stay_month).slice(0, 3)}`}>
                {money(r.revenue_native, c.currency)}
              </TipRow>
              <div className="text-gray-400 ml-2 font-mono text-[10px]">
                {money(c.booked_revenue_native, c.currency)} + {nights(r.room_nights_added)} ×{" "}
                {money(r.adr, c.currency)}
              </div>
            </div>
          );
        })}
      </div>
      <div className="border-t border-gray-700 mt-1.5 pt-1.5 space-y-0.5">
        <TipRow label="At this speed" strong>
          {currency ? money(block.revenue_native, currency) : money(block.revenue_vnd, "VND")}
        </TipRow>
        <TipRow label="Target">
          {currency ? money(block.target_native, currency) : money(block.target_vnd, "VND")}
        </TipRow>
      </div>
      <div className="text-gray-500 mt-1.5">
        Nights already booked keep the revenue they actually sold for; only the nights still to
        come are priced at the window's rate.
      </div>
    </>
  );
}

/**
 * Does this month reach its revenue target at the rate rooms are filling now?
 *
 * Three readings in one unit — what is sold, what today's speed adds, what the
 * target asks for — and none of them a prediction: two are arithmetic and one
 * is a division on the target. The reader does the comparing.
 *
 * Deliberately narrow. An earlier version also projected where the month lands
 * by the shape of the year before, and carried both; it answered a question
 * nobody had asked and doubled the numbers on screen.
 */
function ForecastCard({ data, oneMonth }) {
  const f = data.forecast;
  if (f && !f.available && f.reason === "filtered") {
    const by = f.filtered_by || [];
    return (
      <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-900">
        <div className="font-semibold">
          No projection for {by.includes("source") ? "a source" : "a room-type"} selection
        </div>
        <div className="mt-1 opacity-90">
          Booking pace can be narrowed to {by.includes("source") ? "one source" : "one room type"},
          but the things it has to be measured against cannot: the booked revenue and the rate
          both come from a table with no source column and no room split, and the KPI target is
          set for the whole branch. Every figure would be a slice divided by a whole — a wrong
          number that looks right. Clear the filter to read the projection.
        </div>
      </div>
    );
  }
  if (!f?.available) return null;

  const t = f.total;
  const rr = t.run_rate;
  const cells = f.cells || [];
  const currency = t.currency || "VND";
  const revenue = t.currency ? rr.revenue_native : rr.revenue_vnd;
  const target = t.currency ? rr.target_native : rr.target_vnd;
  const hit = t.currency ? rr.achievement_pct : rr.achievement_vnd_pct;
  const gap = revenue == null || !target ? null : revenue - target;
  const shortBy = rr.needed_occ_pct == null
    ? null
    : Math.max(0, rr.needed_occ_pct - rr.otb_occ_pct - rr.points_added);

  const runwayDays = [...new Set(cells.map((c) => c.run_rate.days_left))].sort((a, b) => a - b);
  const runway = runwayDays.length === 1
    ? `${runwayDays[0]} days`
    : `${runwayDays[0]}–${runwayDays[runwayDays.length - 1]} days`;

  const TILE_KIND = { otb: "actual", speed: "projected",
                      needed: "target", revenue: "both" };
  const tiles = [
    ["otb", "OCC on the books", occ(rr.otb_occ_pct), `${nights(t.otb_room_nights)} room-nights sold`],
    ["speed", "At this speed", `+${occ(rr.points_added)}`,
     `${rr.room_nights_per_day.toFixed(2)}/day × ${runway} left`],
    ["needed", "Needed for target",
     rr.needed_occ_pct == null ? "—" : occ(rr.needed_occ_pct),
     shortBy == null
       ? "no target to price"
       : shortBy <= 0.05
         ? "clear at this speed"
         : `+${rr.needed_extra_per_day.toFixed(2)}/day to reach target`],
    ["revenue", "Revenue at this speed", shortMoney(revenue, currency),
     hit == null
       ? "no target to compare"
       : `${hit.toFixed(0)}% of target · ${gap >= 0 ? "ahead by" : "short by"} ${
           shortMoney(Math.abs(gap), currency)}`],
  ];

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-800">
          At this speed, {oneMonth ? "does this month" : "do these months"} reach target?
        </h2>
        <span className="text-xs text-gray-400">
          on the books + room-nights a day × days left to sell
        </span>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mt-3">
        {tiles.map(([key, label, value, sub]) => (
          <div key={key}>
            <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">
              {label}<Tag kind={TILE_KIND[key]} />
            </div>
            <HoverTooltip
              content={key === "revenue"
                ? revenueWorking(cells, rr, t.currency)
                : pointsWorking(cells, rr, key)}
              width="w-96"
            >
              <div className={`text-3xl font-bold mt-1 tabular-nums decoration-dotted underline-offset-4 hover:underline ${
                key === "needed" && rr.needed_occ_pct > 100 ? "text-red-600"
                  : key === "revenue" && hit != null ? (hit >= 100 ? "text-emerald-600" : "text-red-600")
                  : "text-gray-900"}`}>
                {value}
              </div>
            </HoverTooltip>
            <div className="text-sm text-gray-500 mt-0.5 tabular-nums">{sub}</div>
          </div>
        ))}
      </div>

      {revenue != null && (
        <div className="mt-3 text-sm text-gray-600 tabular-nums">
          <span className="font-semibold text-gray-900">{money(revenue, currency)}</span> of{" "}
          {money(target, currency)} target
          {/* Name the rate. A revenue figure whose price nobody showed is a
              figure nobody can check. */}
          <div className="text-xs text-gray-500 mt-1">
            {rr.adr
              ? `Nights still to come priced at ${money(rr.adr, currency)} each — what the last
                 ${rr.window_days} days actually sold at. Nights already booked keep what they
                 sold for.`
              : `Nights still to come priced at what each branch's last ${rr.window_days} days
                 actually sold at. Nights already booked keep what they sold for.`}
          </div>
        </div>
      )}

      <p className="text-xs text-gray-500 mt-3 leading-snug">
        The first three are points of the same house, so they read against each other directly;
        the fourth is what they come to in money.
        <span className="font-medium text-gray-600"> At this speed</span> is arithmetic, not a
        forecast: today's rate carried flat. Bookings crowd towards check-in rather than arriving
        evenly, so a month still months away sits low here by construction — read it as the floor
        if nothing accelerates.
      </p>

      {/* A target a full house cannot reach is not a pace problem, and the
          card must not let it be read as one. */}
      {rr.over_capacity?.length > 0 && (
        <div className="mt-3 bg-red-50 border border-red-200 rounded-lg p-3 text-xs text-red-900">
          <div className="font-semibold">
            {rr.over_capacity.length === 1
              ? "One month needs" : `${rr.over_capacity.length} months need`}{" "}
            more than the house holds — selling out would still miss the target
          </div>
          <ul className="mt-1 space-y-0.5">
            {rr.over_capacity.map((o) => (
              <li key={`${o.branch_id}-${o.stay_month}`} className="tabular-nums">
                {o.branch_name} {monthLabel(o.stay_month)}: needs {occ(o.needed_occ_pct)} of the
                house at {money(o.adr_now, o.currency)} a night. A full house clears the target
                only at {money(o.adr_needed, o.currency)}
                {o.adr_now ? ` (${o.adr_needed >= o.adr_now ? "+" : ""}${
                  Math.round((o.adr_needed / o.adr_now - 1) * 100)}%)` : ""}.
              </li>
            ))}
          </ul>
          <div className="mt-1 opacity-80">Rate, not pace. No amount of filling fixes these.</div>
        </div>
      )}

      {/* Per month, because a quarter that clears its target routinely hides a
          month that does not. */}
      {f.months.length > 1 && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 border-b border-gray-200">
                <th className="text-left font-medium py-1.5">Month</th>
                <th className="text-right font-medium">OCC on the books</th>
                <th className="text-right font-medium">At this speed</th>
                <th className="text-right font-medium">Needed</th>
                <th className="text-right font-medium">vs target</th>
              </tr>
            </thead>
            <tbody>
              {f.months.map((m) => {
                const r = m.run_rate;
                const mHit = m.currency ? r.achievement_pct : r.achievement_vnd_pct;
                const mCells = cells.filter((c) => c.stay_month === m.stay_month);
                return (
                  <tr key={m.stay_month} className="border-b border-gray-100 last:border-0">
                    <td className="py-1.5 text-gray-700">
                      {monthLabel(m.stay_month)}
                      <span className="text-xs text-gray-400 ml-1.5">
                        {mCells[0]?.run_rate.days_left}d left
                      </span>
                    </td>
                    <td className="text-right tabular-nums text-gray-500">{occ(r.otb_occ_pct)}</td>
                    <td className="text-right tabular-nums font-medium text-gray-900">
                      <HoverTooltip
                        content={pointsWorking(mCells, r, "speed")}
                        width="w-96"
                        className="decoration-dotted underline-offset-4 hover:underline"
                      >
                        +{occ(r.points_added)}
                      </HoverTooltip>
                    </td>
                    <td className={`text-right tabular-nums ${
                      r.needed_occ_pct > 100 ? "text-red-600" : "text-gray-500"}`}>
                      {occ(r.needed_occ_pct)}
                    </td>
                    <td className={`text-right tabular-nums ${toneFor(
                      mHit == null ? null : mHit - 100, 2)}`}>
                      {mHit == null ? "—" : `${mHit.toFixed(0)}%`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function monthSpan(months) {
  if (!months?.length) return "";
  return months.length === 1
    ? MONTH_ABBR[months[0] - 1]
    : `${MONTH_ABBR[months[0] - 1]}–${MONTH_ABBR[months[months.length - 1] - 1]}`;
}

/** One branch's year: what it banked, then every month still open. */
function yearWorking(row, months) {
  const cur = row.currency;
  const shown = row.projected_detail.filter(
    (d) => !months || months.includes(d.month));
  const total = months
    ? shown.reduce((a, d) => a + d.revenue_native, 0)
    : row.projection_native;
  const target = months
    ? shown.reduce((a, d) => a + d.target_native, 0)
    : row.target_native;
  return (
    <>
      <div className="font-semibold text-white mb-1">
        {row.branch_name} · {months ? "Q4" : "full year"}
      </div>
      <div className="text-gray-300 mb-1.5">
        {months
          ? "each month projected from booking pace"
          : "months that have finished + months still open"}
      </div>
      <div className="space-y-0.5">
        {!months && (
          <TipRow label={`Banked · ${row.settled_count} months`}>
            {money(row.actual_to_date_native, cur)}
          </TipRow>
        )}
        {shown.map((d) => (
          <TipRow
            key={d.month}
            label={MONTH_ABBR[d.month - 1]}
            note={d.basis === "own_run_rate" ? "run rate" : null}
          >
            {money(d.revenue_native, cur)}
          </TipRow>
        ))}
        {row.months_not_projected.length > 0 && (
          <TipRow label="Not projected">
            {row.months_not_projected.map((m) => MONTH_ABBR[m - 1]).join(", ")}
          </TipRow>
        )}
      </div>
      <div className="border-t border-gray-700 mt-1.5 pt-1.5 space-y-0.5">
        <TipRow label={months ? "Q4 projection" : "Full year"} strong>
          {money(total, cur)}
        </TipRow>
        <TipRow label="Target">{money(target, cur)}</TipRow>
        <TipRow label="Hit" strong>
          {target ? `${(total / target * 100).toFixed(0)}%` : "—"}
        </TipRow>
      </div>
      {!months && (
        <div className="text-gray-500 mt-1.5">
          Banked months are the KPI grid's own figures, accounting overrides included. Projected
          months carry the same deduction and other-revenue treatment, so both halves are on the
          basis the target was set against.
        </div>
      )}
    </>
  );
}

/** The group's year, one line per branch, summed in VND. */
function yearGroupWorking(data) {
  const t = data.total;
  return (
    <>
      <div className="font-semibold text-white mb-1">Full year {data.year}</div>
      <div className="text-gray-300 mb-1.5">
        each branch in its own currency, summed in VND
      </div>
      <div className="space-y-0.5">
        {data.branches.map((b) => (
          <TipRow key={b.branch_id} label={b.branch_name.replace("MEANDER ", "")}>
            {money(b.projection_native, b.currency)} / {money(b.target_native, b.currency)}
            {b.achievement_pct === null ? "" : ` = ${b.achievement_pct.toFixed(0)}%`}
          </TipRow>
        ))}
      </div>
      <div className="border-t border-gray-700 mt-1.5 pt-1.5 space-y-0.5">
        <TipRow label="Banked">{money(t.actual_to_date_vnd, "VND")}</TipRow>
        <TipRow label="Still to sell">{money(t.forecast_remaining_vnd, "VND")}</TipRow>
        <TipRow label="Full year" strong>{money(t.projection_vnd, "VND")}</TipRow>
        <TipRow label="Target">{money(t.target_vnd, "VND")}</TipRow>
        <TipRow label="Hit" strong>
          {t.achievement_pct === null ? "—" : `${t.achievement_pct.toFixed(0)}%`}
        </TipRow>
      </div>
      <div className="text-gray-500 mt-1.5">
        TWD and JPY are converted at the rate the app holds (830 / 165), which has never come
        from a live feed — read the group figure as the planning number it is.
      </div>
    </>
  );
}

/**
 * Does the YEAR clear its revenue target — the question the stay-month picker
 * above cannot answer, because eight of the twelve months are behind us and
 * none of them are in the selection.
 *
 * Deliberately not wired to the picker. Its scope is the calendar year and
 * nothing else, so it says so in its own header rather than moving under
 * someone who changed the stay month to December.
 *
 * The two halves are never merged into one number without being named: "we
 * have earned 79% of the year's target" and "we are on course for 96% of it"
 * are different claims, and only the second is a forecast.
 */
function YearOutlook({ branchId, days }) {
  const { data, isPending, isError } = useQuery({
    queryKey: ["kpi-pace-forecast", branchId || "all", days],
    queryFn: () => axios
      .get(`/api/kpi/pace-forecast?days=${days}${branchId ? `&branch_id=${branchId}` : ""}`)
      .then((r) => r.data.data),
    staleTime: 5 * 60 * 1000,
  });

  if (isPending) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-4 text-sm text-gray-500 animate-pulse">
        Working out where the year lands…
      </div>
    );
  }
  if (isError || !data?.available) return null;

  const t = data.total;
  const single = data.branches.length === 1 ? data.branches[0] : null;
  const cur = single ? single.currency : "VND";
  const val = (row, nativeKey, vndKey) => (single ? row[nativeKey] : t[vndKey]);
  const projection = single ? single.projection_native : t.projection_vnd;
  const target = single ? single.target_native : t.target_vnd;
  const banked = single ? single.actual_to_date_native : t.actual_to_date_vnd;
  const toCome = single ? single.forecast_remaining_native : t.forecast_remaining_vnd;
  const hit = single ? single.achievement_pct : t.achievement_pct;
  const low = single ? single.achievement_low_pct : t.achievement_low_pct;
  const high = single ? single.achievement_high_pct : t.achievement_high_pct;
  const gap = projection - target;
  const q4Hit = single ? single.q4_achievement_pct : t.q4_achievement_pct;
  const bankedPct = target ? (banked / target) * 100 : null;
  const yearTip = single ? yearWorking(single, null) : yearGroupWorking(data);

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-800">
          At this speed, does {data.year} reach target?
        </h2>
        <span className="text-xs text-gray-400">
          the whole year — follows the branch and the booking window, not the stay month or the
          source and room-type filters
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-4 gap-4 mt-3">
        <div>
          <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">
            Full year at this speed<Tag kind="both" />
          </div>
          <HoverTooltip content={yearTip} width="w-96">
            <div className={`text-3xl font-bold mt-1 tabular-nums decoration-dotted underline-offset-4 hover:underline ${
              hit === null ? "text-gray-900" : hit >= 100 ? "text-emerald-600" : "text-red-600"}`}>
              {hit === null ? "—" : `${hit.toFixed(0)}%`}
            </div>
          </HoverTooltip>
          <div className="text-sm text-gray-500 mt-0.5 tabular-nums">of target</div>
        </div>

        <div>
          <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">
            {gap >= 0 ? "Ahead by" : "Short by"}<Tag kind="both" />
          </div>
          <HoverTooltip content={yearTip} width="w-96">
            <div className="text-3xl font-bold text-gray-900 mt-1 tabular-nums decoration-dotted underline-offset-4 hover:underline">
              {shortMoney(Math.abs(gap), cur)}
            </div>
          </HoverTooltip>
          <div className="text-sm text-gray-500 mt-0.5 tabular-nums">
            {shortMoney(projection, cur)} of {shortMoney(target, cur)}
          </div>
        </div>

        <div>
          <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">
            Banked · {monthSpan(data.settled_months)}<Tag kind="actual" />
          </div>
          <div className="text-3xl font-bold text-gray-900 mt-1 tabular-nums">
            {shortMoney(banked, cur)}
          </div>
          <div className="text-sm text-gray-500 mt-0.5 tabular-nums">
            {bankedPct === null ? "already earned" : `${bankedPct.toFixed(0)}% of the year's target, already earned`}
          </div>
        </div>

        <div>
          <div className="text-xs font-medium text-gray-500 uppercase tracking-wide">
            Still to sell · {monthSpan(data.projected_months)}<Tag kind="projected" />
          </div>
          <div className="text-3xl font-bold text-gray-900 mt-1 tabular-nums">
            {shortMoney(toCome, cur)}
          </div>
          <div className="text-sm text-gray-500 mt-0.5 tabular-nums">
            {q4Hit === null || q4Hit === undefined
              ? "Q4 not fully projected"
              : `Q4 alone reaches ${q4Hit.toFixed(0)}% of its target`}
          </div>
        </div>
      </div>

      {!single && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 border-b border-gray-200">
                <th className="text-left font-medium py-1.5">Branch</th>
                <th className="text-right font-medium">
                  Banked<div className="font-normal text-gray-400">actual</div>
                </th>
                <th className="text-right font-medium">
                  Still to sell<div className="font-normal text-indigo-500">projected</div>
                </th>
                <th className="text-right font-medium">
                  Full year<div className="font-normal text-indigo-500">actual + projected</div>
                </th>
                <th className="text-right font-medium">Target</th>
                <th className="text-right font-medium">Gap</th>
                <th className="text-right font-medium">Year</th>
                <th className="text-right font-medium">Q4</th>
              </tr>
            </thead>
            <tbody>
              {data.branches.map((b) => (
                <tr key={b.branch_id} className="border-b border-gray-100 last:border-0">
                  <td className="py-1.5 text-gray-700">
                    {b.branch_name}
                    <span className="text-xs text-gray-400 ml-1.5">{b.currency}</span>
                  </td>
                  <td className="text-right tabular-nums text-gray-500">
                    {shortMoney(b.actual_to_date_native, b.currency)}
                  </td>
                  <td className="text-right tabular-nums text-gray-500">
                    {shortMoney(b.forecast_remaining_native, b.currency)}
                  </td>
                  <td className="text-right tabular-nums font-medium text-gray-900">
                    <HoverTooltip
                      content={yearWorking(b, null)}
                      width="w-96"
                      className="decoration-dotted underline-offset-4 hover:underline"
                    >
                      {shortMoney(b.projection_native, b.currency)}
                    </HoverTooltip>
                  </td>
                  <td className="text-right tabular-nums text-gray-500">
                    {shortMoney(b.target_native, b.currency)}
                  </td>
                  <td className={`text-right tabular-nums ${toneFor(b.gap_native, 0)}`}>
                    {b.gap_native >= 0 ? "+" : "−"}{shortMoney(Math.abs(b.gap_native), b.currency)}
                  </td>
                  <td className={`text-right tabular-nums font-medium ${
                    b.achievement_pct === null ? "text-gray-400"
                      : b.achievement_pct >= 100 ? "text-emerald-600" : "text-red-600"}`}>
                    {b.achievement_pct === null ? "—" : `${b.achievement_pct.toFixed(0)}%`}
                  </td>
                  <td className={`text-right tabular-nums ${
                    b.q4_achievement_pct === null ? "text-gray-400"
                      : b.q4_achievement_pct >= 100 ? "text-emerald-600" : "text-red-600"}`}>
                    <HoverTooltip
                      content={yearWorking(b, [10, 11, 12])}
                      width="w-96"
                      className="decoration-dotted underline-offset-4 hover:underline"
                    >
                      {b.q4_achievement_pct === null ? "—" : `${b.q4_achievement_pct.toFixed(0)}%`}
                    </HoverTooltip>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-xs text-gray-500 mt-3 leading-snug">
        {monthSpan(data.settled_months)} counted as {data.settled_months.length === 1 ? "it" : "they"}{" "}
        happened — the same figure the KPI grid shows, accounting overrides included.{" "}
        {monthSpan(data.projected_months)} carried at the speed rooms are filling now, the month
        underway included, because a month two weeks old still has most of its revenue ahead of
        it. Months still far off sit low here by construction — this is the floor if nothing
        accelerates, not a call on where the year ends.
        {data.branches.some((b) => b.months_not_projected.length > 0) && (
          <>
            {" "}
            {data.branches
              .filter((b) => b.months_not_projected.length > 0)
              .map((b) => `${b.branch_name} (${b.months_not_projected.map((m) => MONTH_ABBR[m - 1]).join(", ")})`)
              .join(", ")}{" "}
            could not be projected, so those months are out of both the projection and the
            target it is read against.
          </>
        )}
      </p>
    </div>
  );
}

// ── main component ───────────────────────────────────────────────────────────

export default function PerformanceFillPace() {
  const { selected, isAll } = useBranch();
  const [stayMonths, setStayMonths] = useState(() => [defaultMonth()]);
  const [days, setDays] = useState(60);
  // null while a preset is active; {from, to} once a custom range is picked.
  const [range, setRange] = useState(null);
  const [sources, setSources] = useState([]);
  const [roomCategory, setRoomCategory] = useState("");
  // "last_year" compares the same countdown a year back, which controls for the
  // seasonal shape by construction. "previous" compares the window before this
  // one, which does not — see the note the page carries in that mode.
  const [basis, setBasis] = useState("last_year");

  // A custom range is expressed to the API in the same terms as a preset —
  // a day count ending at an as-of date — so both go down one code path.
  // Backwards ranges are read the way they were obviously meant.
  const asOf = range ? (range.to < range.from ? range.from : range.to) : null;
  const rangeStart = range ? (range.to < range.from ? range.to : range.from) : null;
  const rawDays = range ? daysBetweenISO(rangeStart, asOf) + 1 : days;
  const effectiveDays = Math.min(Math.max(rawDays, 1), MAX_WINDOW_DAYS);
  const cappedFrom = rawDays > MAX_WINDOW_DAYS ? shiftISO(asOf, -(MAX_WINDOW_DAYS - 1)) : null;

  const params = new URLSearchParams({ days: String(effectiveDays) });
  stayMonths.forEach((m) => params.append("stay_month", m));
  // Only sent for a custom range: on a preset the server's own "today" is the
  // authority, which keeps the window right for a user in another timezone.
  if (asOf) params.set("as_of", asOf);
  if (!isAll && selected) params.set("branch_id", selected);
  sources.forEach((s) => params.append("source", s));
  if (roomCategory) params.set("room_category", roomCategory);

  const { data, isPending, isError, error, isPlaceholderData } = useQuery({
    queryKey: ["fill-pace", stayMonths.join("|"), effectiveDays, asOf, sources.join("|"),
               roomCategory, selected, isAll],
    queryFn: () => axios.get(`/api/metrics/fill-pace?${params}`).then((r) => r.data.data),
    placeholderData: keepPreviousData,
  });

  const compare = Boolean(data?.last_year);
  // Room-nights counted in beds. Absent when the projection is (a source or
  // room-type filter, or no year-ago comparison), and the tile falls back to
  // the booking count with no claim that it is the KPI figure.
  const otbUnits = data?.current?.otb_units_room_nights ?? null;
  const otbUnitsPct = data?.current?.otb_units_occ_pct ?? null;
  const onPrev = basis === "previous";
  const prev = data?.previous_period;
  const vsPrev = data?.vs_previous_period;
  // "Finished at" is only true of a month that has ended. Pick a stay month far
  // enough ahead and its "last year" is also in the future, and the card would
  // otherwise report today's on-the-books number as an outcome.
  const lyStatus = data?.last_year?.status;
  const lySettled = lyStatus === "finished";
  const unfinishedLabel = monthsLabel(data?.last_year?.unfinished_stay_months || []);
  const currency = data?.scope?.currency;
  const v = useMemo(
    () => (data ? (onPrev ? previousPeriodVerdict(data) : verdict(data)) : null),
    [data, onPrev],
  );

  // Daily pickup is spiky enough that raw bars hide the trend. A 7-day trailing
  // mean is what makes "speeding up / slowing down" visible at all.
  const chartData = useMemo(() => {
    if (!data?.curve) return [];
    // Null until seven days are actually available. Dividing by however many
    // days happened to be in range made the first point a single day, the
    // second a pair, and so on — so every window opened with a low, rising
    // stretch that was the arithmetic warming up rather than bookings speeding
    // up, and it read as an acceleration wherever the window happened to start.
    //
    // Refusing to draw those points left the other half of the problem: the
    // axis still spanned the whole window, so the line began six days into it
    // and the gap read as missing data. The curve now arrives with those six
    // days of run-up in front of it, marked `lead_in` — they feed the average
    // and are dropped before plotting, so the first point drawn is the window's
    // own first day and it is a full week's mean like every other.
    const roll = (arr, key, i, n = SMOOTHING_DAYS) => {
      if (i < n - 1) return null;
      const slice = arr.slice(i - n + 1, i + 1);
      return slice.reduce((s, p) => s + (p[key] || 0), 0) / n;
    };
    return data.curve
      .map((p, i) => ({
        ...p,
        day_avg: roll(data.curve, "day_room_nights", i),
        ly_day_avg: compare ? roll(data.curve, "ly_day_room_nights", i) : undefined,
        prev_day_avg: roll(data.curve, "prev_day_room_nights", i),
      }))
      .filter((p) => !p.lead_in);
  }, [data, compare]);

  // Today is a day still in progress: its bookings are a few hours old, not a
  // day's worth, so the speed line always drooped at the right edge whatever
  // was really happening. The cumulative chart keeps it — on the books today is
  // a true figure — but the per-day chart drops it and says so.
  const endsToday = data?.as_of === todayISO();
  const speedData = useMemo(
    () => (endsToday ? chartData.slice(0, -1) : chartData),
    [chartData, endsToday],
  );

  // Every source the month actually saw, grouped by category so the picker can
  // offer "all of Direct" in one click without needing a rolled-up row.
  const sourceGroups = useMemo(() => {
    const seen = new Map();
    for (const s of data?.by_source || []) {
      seen.set(s.source, s.category || "OTA");
    }
    // A hand-picked source stays visible even if it booked nothing this month,
    // otherwise the filter would silently drop itself out of its own list.
    for (const s of sources) if (!seen.has(s)) seen.set(s, "Other");

    const groups = new Map();
    for (const [name, category] of seen) {
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push(name);
    }
    // Direct first — it is the one the team argues about — then the rest by size.
    return [...groups.entries()]
      .sort((a, b) => (a[0] === "Direct" ? -1 : b[0] === "Direct" ? 1 : b[1].length - a[1].length))
      .map(([category, names]) => ({
        group: category,
        options: names.map((n) => ({ value: n, label: n })),
      }));
  }, [data, sources]);

  const toggleSource = (name) =>
    setSources((cur) => (cur.includes(name) ? cur.filter((s) => s !== name) : [...cur, name]));

  const toggleGroup = (names) =>
    setSources((cur) => {
      const all = names.every((n) => cur.includes(n));
      return all ? cur.filter((s) => !names.includes(s)) : [...new Set([...cur, ...names])];
    });

  const shownMonths = data?.stay_months || stayMonths;
  const monthStartLabel = monthsLabel(shownMonths);
  // A countdown axis only names a point in time when there is one month to
  // count down to. Across several, the curve is read by booking date instead.
  const oneMonth = shownMonths.length === 1;

  // Tooltip heading. One month counts down to its own start; several are read
  // by booking date, which is the same date in every month's curve anyway.
  const axisLabel = (d) => {
    if (typeof d === "number") {
      return `${d >= 0 ? d : -d} days ${d >= 0 ? "before" : "into"} the month`;
    }
    const asDate = longDate(d);
    return asDate ? `Booked ${asDate}` : "";
  };

  const toggleMonth = (ym) =>
    setStayMonths((cur) => {
      const next = cur.includes(ym) ? cur.filter((m) => m !== ym) : [...cur, ym];
      // Never leave the page with nothing to measure.
      return (next.length ? next : [ym]).sort();
    });

  const toggleMonthGroup = (values) =>
    setStayMonths((cur) => {
      const all = values.every((v) => cur.includes(v));
      const next = all ? cur.filter((m) => !values.includes(m)) : [...new Set([...cur, ...values])];
      return (next.length ? next : values).slice(0, MAX_STAY_MONTHS).sort();
    });

  return (
    <div className="space-y-5">
      {/* Header — the comparison basis lives here rather than down among the
          filters: it changes what every number on the page is measured against,
          which is a different kind of control from narrowing the scope. */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Fill Pace</h1>
          <p className="text-sm text-gray-500">
            How fast {monthStartLabel} {oneMonth ? "is" : "are"} filling up, against the same
            countdown one year earlier
            <SyncBadge timestamp={data?.data_synced_at} />
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-400">Compare with</span>
          <div className="flex gap-0.5 bg-gray-100 rounded-lg p-1">
            {[
              { key: "last_year", label: "Last year" },
              { key: "previous", label: `Previous ${data?.days ?? 30}d` },
            ].map((v) => (
              <button key={v.key} onClick={() => setBasis(v.key)}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                  basis === v.key
                    ? "bg-white text-gray-800 shadow-sm"
                    : "text-gray-500 hover:text-gray-700"
                }`}>
                {v.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Controls */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 flex flex-wrap items-end gap-4">
        <Field label={stayMonths.length > 1 ? "Stay months" : "Stay month"}>
          <MultiPicker
            groups={MONTH_GROUPS}
            selected={stayMonths}
            emptyLabel=""
            summarise={monthsLabel}
            onToggle={toggleMonth}
            onToggleGroup={toggleMonthGroup}
            width="11rem"
          />
        </Field>

        <Field label="Booking window">
          <div className="flex items-center gap-1.5">
            {WINDOWS.map((d) => (
              <button key={d} onClick={() => { setRange(null); setDays(d); }}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  !range && days === d
                    ? "bg-indigo-600 text-white shadow-sm"
                    : "bg-white border border-gray-200 text-gray-600 hover:border-indigo-300"
                }`}>
                {d}d
              </button>
            ))}
            <button
              onClick={() =>
                setRange(range
                  ? null
                  : { from: shiftISO(todayISO(), -(days - 1)), to: todayISO() })}
              className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                range
                  ? "bg-indigo-600 text-white shadow-sm"
                  : "bg-white border border-gray-200 text-gray-600 hover:border-indigo-300"
              }`}>
              Custom
            </button>
          </div>
        </Field>

        {range && (
          <Field label="Booked between">
            <div className="flex items-center gap-2">
              <input type="date" className={SELECT_CLS} value={range.from} max={todayISO()}
                     onChange={(e) => setRange({ ...range, from: e.target.value })} />
              <span className="text-gray-400 text-sm">→</span>
              <input type="date" className={SELECT_CLS} value={range.to} max={todayISO()}
                     onChange={(e) => setRange({ ...range, to: e.target.value })} />
            </div>
            {cappedFrom && (
              <span className="text-xs text-amber-700">
                Capped at {MAX_WINDOW_DAYS} days — reading from {longDate(cappedFrom)}.
              </span>
            )}
          </Field>
        )}

        <Field label="Source">
          <MultiPicker
            groups={sourceGroups}
            selected={sources}
            emptyLabel="All sources"
            summarise={(s) => (s.length === 1 ? s[0] : `${s.length} sources`)}
            onToggle={toggleSource}
            onToggleGroup={toggleGroup}
            onClear={() => setSources([])}
          />
        </Field>

        <Field label="Room type">
          <select className={SELECT_CLS} value={roomCategory}
                  onChange={(e) => setRoomCategory(e.target.value)}>
            <option value="">Rooms + dorms</option>
            <option value="Room">Rooms only</option>
            <option value="Dorm">Dorms only</option>
          </select>
        </Field>

        {data && (
          <div className="text-xs text-gray-500 ml-auto leading-relaxed">
            Booked {shortDate(data.window.from)}–{shortDate(data.window.to)} ({data.days}d)
            {oneMonth && <> ·{" "}{data.days_out?.to} days before {monthStartLabel.split(" ")[0]} 1</>}
            {compare && oneMonth && (
              <>
                <br />
                Last year: {shortDate(data.last_year.window.from)}–{shortDate(data.last_year.window.to)}
              </>
            )}
            {compare && !oneMonth && (
              <>
                <br />
                Each month against its own countdown a year earlier
              </>
            )}
          </div>
        )}
      </div>

      {isError && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-sm text-red-800">
          Could not load fill pace: {error?.response?.data?.error || error?.message || "unknown error"}
        </div>
      )}

      {isPending && !data && (
        <div className="text-sm text-gray-500 animate-pulse">Loading fill pace…</div>
      )}

      {data && (
        <div className={isPlaceholderData ? "opacity-60 transition-opacity space-y-5" : "space-y-5"}>
          {/* Verdict */}
          {v && (
            <div className={`border rounded-xl p-4 ${VERDICT_STYLE[v.tone]}`}>
              <div className="font-semibold">{v.headline}</div>
              <div className="text-sm mt-1 opacity-90">{v.detail}</div>
            </div>
          )}

          {/* When last year has not finished either, every comparison below is
              measuring against a period still taking bookings. Said once, up
              top, rather than left for someone to infer from a suspiciously
              low "finished at". */}
          {compare && !lySettled && (
            <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-900">
              <div className="font-semibold">
                {lyStatus === "future"
                  ? "Last year has not happened yet"
                  : "Last year has not finished yet"}
              </div>
              <div className="mt-1 opacity-90">
                The comparison period {unfinishedLabel} {lyStatus === "future" ? "is" : "is still"}{" "}
                {lyStatus === "future" ? "in the future" : "taking bookings"}, so every year-ago
                figure on this page is a snapshot rather than an outcome. Pace, the points gap and
                the percentage change are all measured against a number that has not settled — read
                them as direction, not as a verdict.
              </div>
            </div>
          )}

          {/* Period-over-period does not control for the shape of the booking
              curve, and the shape is steep: measured across settled months this
              group picks up 1.5x to 4.6x more in each window than the one
              before, having done nothing. So the caveat is not a footnote here,
              it sits above the numbers it applies to. */}
          {onPrev && (
            <div className="bg-amber-50 border border-amber-200 rounded-xl p-4 text-sm text-amber-900">
              <div className="font-semibold">Read this against the calendar, not as a score</div>
              <div className="mt-1 opacity-90">
                Bookings arrive faster the closer check-in gets, so a window beats the one before
                it whether or not anything was done — across settled months this group runs 1.5×
                to 4.6× window over window on its own.
                {vsPrev?.natural_acceleration
                  ? ` Last year this same stretch ran ${vsPrev.natural_acceleration.toFixed(2)}×, which is the number to beat.`
                  : " There is no year-ago volume here to say what the norm was, so the figure below is unchecked against anything."}
                {compare && data.last_year.share_of_final_pct != null && (
                  <> And it is early: by this point last year only{" "}
                    {occ(data.last_year.share_of_final_pct)} of the month had sold.</>
                )}
              </div>
            </div>
          )}

          {/* Headline numbers */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <Stat
              label="OCC on the books"
              // Beds, not bookings — the count the KPI page and the target
              // use. The pace figures beside this one stay on the booking
              // count, which is what they compare like for like.
              value={occ(otbUnitsPct ?? data.current.otb_occ_pct)}
              sub={`${nights(otbUnits ?? data.current.otb_room_nights)} of ${nights(data.scope.available_room_nights)} room-nights`}
              hint={otbUnits
                ? `${data.scope.units_in_scope} units × ${data.stay_days} nights, counting every bed sold — the same count the KPI page uses`
                : `${data.scope.units_in_scope} units × ${data.stay_days} nights`}
            />
            <Stat
              label={`Picked up (${data.days}d)`}
              value={nights(data.current.pickup_room_nights)}
              sub={compare
                ? `${pctLabel(data.vs_last_year.pickup_room_nights_pct)} vs LY · LY ${nights(data.last_year.pickup_room_nights)}`
                : `${data.current.pickup_bookings} bookings`}
              subTone={compare ? toneFor(data.vs_last_year.pickup_room_nights_pct, 2) : "text-gray-500"}
              hint="Room-nights added inside the window — this is the speed"
            />
            {onPrev ? (
            <Stat
              label={`vs previous ${data.days} days`}
              value={vsPrev?.acceleration ? (
                <span className="whitespace-nowrap">
                  {nights(data.current.pickup_room_nights)}
                  <span className="text-base font-normal text-gray-400 mx-1.5">vs</span>
                  <span className="text-gray-500">{nights(prev?.pickup_room_nights)}</span>
                </span>
              ) : "—"}
              sub={vsPrev?.acceleration
                ? `${vsPrev.acceleration.toFixed(2)}× the window before${
                    vsPrev.excess_acceleration
                      ? ` · ${vsPrev.excess_acceleration.toFixed(2)}× once the season is allowed for`
                      : " · no norm to check it against"
                  }`
                : "nothing booked in the window before"}
              subTone={vsPrev?.excess_acceleration
                ? toneFor(vsPrev.excess_acceleration - 1, 0.05)
                : "text-amber-700"}
              hint={`Room-nights booked in this window against the ${data.days} days before it, same stay month. The raw multiple is mostly the calendar — bookings crowd towards check-in — so the second figure is the one to read.`}
            />
            ) : (
            <Stat
              label="vs last year, same point"
              // Both sides shown rather than the gap alone: two percentages side
              // by side cannot be misread, where a lone "+15.1 pts" needed the
              // reader to know that pts and % are different things — and the
              // same gap written as a percentage change would say +368%.
              value={compare ? (
                <span className="whitespace-nowrap">
                  {occ(data.current.otb_occ_pct)}
                  <span className="text-base font-normal text-gray-400 mx-1.5">vs</span>
                  <span style={{ color: LAST_YEAR }}>{occ(data.last_year.otb_occ_pct)}</span>
                </span>
              ) : "—"}
              sub={compare
                ? `${gapLabel(data.vs_last_year.otb_occ_pts)}${
                    oneMonth ? ` · ${data.days_out?.to} days to go` : " at the same point"
                  }`
                : "comparison off"}
              subTone={toneFor(data?.vs_last_year?.otb_occ_pts, 0.5)}
              hint={compare && oneMonth
                ? `Both read ${data.days_out?.to} days before the month starts — the same distance from check-in, not the same calendar date. Stated as a gap in fill, not as a percentage change: ${occ(data.last_year.otb_occ_pct)} → ${occ(data.current.otb_occ_pct)} is +${Math.round((data.current.otb_occ_pct / data.last_year.otb_occ_pct - 1) * 100)}% relative, which is true and useless on a base this small.`
                : "Both read at the same distance from the month, not the same calendar date"}
            />
            )}
            <Stat
              label={lySettled ? "Last year ended at" : "Last year, so far"}
              value={compare ? occ(data.last_year.final_occ_pct) : "—"}
              sub={!compare ? "comparison off"
                : lySettled
                  ? `${nights(data.last_year.otb_room_nights)} → ${nights(data.last_year.final_room_nights)} room-nights: +${nights(data.last_year.remaining_after_window_room_nights)} arrived after this point`
                  : `${monthsLabel(data.last_year.stay_months)} has not happened yet — this is what it holds today, not what it ends at`}
              subTone={lySettled ? "text-gray-500" : "text-amber-700"}
              hint={lySettled
                ? `Answers "from where I stand, how much more is there left to sell?" — last year that stretch was worth ${nights(data.last_year.remaining_after_window_room_nights)} room-nights.`
                : "Not an outcome: an unfinished period keeps taking bookings"}
            />
          </div>

          {/* The window control moves exactly one of the four cards above, and
              it is not obvious which — so it is stated rather than inferred. */}
          <p className="text-xs text-gray-500 -mt-2">
            Only <span className="font-medium text-gray-600">Picked up</span> follows the booking
            window. The other three are read at {shortDate(data.as_of)} whatever the window is:
            on the books is everything sold so far, and the year-ago figures are the same date
            counted back from each stay month.
          </p>

          {compare && <ForecastCard data={data} oneMonth={oneMonth} />}

          <YearOutlook branchId={!isAll && selected ? selected : null} days={effectiveDays} />

          {/* The speed. The cumulative curve that used to sit above this was
              dropped: a line that only ever rises said less about pace than
              its own slope does, and the position it carried is on the cards. */}
          <div className="bg-white border border-gray-200 rounded-xl p-4">
            <h2 className="text-sm font-semibold text-gray-800">
              How fast it is selling
            </h2>
            <p className="text-xs text-gray-500 mt-0.5 mb-3">
              Room-nights sold per booking day, smoothed over {SMOOTHING_DAYS} days. Above the
              other line means selling faster than {onPrev ? `the ${data.days} days before` : "the same run-up last year"}.
              Each point is the week ending on it, and the week before the window opens is
              read too, so the line covers the window end to end{endsToday
                ? " — bar today, a day still in progress." : "."}
            </p>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={speedData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis
                  dataKey={oneMonth ? "days_out" : "date"}
                  tick={{ fontSize: 11, fill: "#9ca3af" }}
                  tickFormatter={(d) =>
                    oneMonth ? (d >= 0 ? `${d}d` : `+${-d}d`) : shortDate(d)}
                />
                <YAxis tick={{ fontSize: 11, fill: "#9ca3af" }} width={44} />
                <Tooltip
                  contentStyle={{ fontSize: 12, borderRadius: 8 }}
                  labelFormatter={axisLabel}
                  formatter={(val, name) => [
                    val === null || val === undefined ? "—" : Number(val).toFixed(1),
                    name,
                  ]}
                />
                <Legend iconSize={10} wrapperStyle={{ fontSize: 12 }} />
                <Line type="monotone" dataKey="day_avg" name="This year"
                      stroke={THIS_YEAR} strokeWidth={2.5} dot={false} />
                {onPrev ? (
                  <Line type="monotone" dataKey="prev_day_avg"
                        name={`Previous ${data.days} days`}
                        stroke={LAST_YEAR} strokeWidth={2} strokeDasharray="5 4" dot={false} />
                ) : compare && (
                  <Line type="monotone" dataKey="ly_day_avg" name="Last year, same countdown"
                        stroke={LAST_YEAR} strokeWidth={2} strokeDasharray="5 4" dot={false} />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Which month is pacing ahead — a healthy quarter can hide a bad month */}
          {data.months?.length > 1 && (
            <PaceTable
              title="By month"
              subtitle="Each month read against its own countdown a year earlier, so October is compared with last October at the same distance from check-in — not with December's."
              rows={data.months.map((m) => ({ ...m, month_label: monthLabel(m.stay_month) }))}
              nameKey="month_label"
              nameLabel="Stay month"
              currency={currency}
              compare={compare}
            />
          )}

          {/* Which source is pacing ahead */}
          <PaceTable
            title="By source"
            subtitle={`One row per source, always the full month whatever the Source filter is set to — otherwise this table could not answer which channel is carrying ${monthStartLabel.split(" ")[0]}. The rows sum back to the whole month.`}
            rows={data.by_source}
            nameKey="source"
            nameLabel="Source"
            currency={currency}
            compare={compare}
          />

          {/* Which branch is pacing ahead */}
          {data.branches?.length > 1 && (
            <PaceTable
              title="By branch"
              subtitle="Fill % is each branch against its own inventory, so a 12-room property and a 60-room one are read on their own terms."
              rows={data.branches}
              nameKey="branch_name"
              nameLabel="Branch"
              currency={currency}
              compare={compare}
            />
          )}

          {/* What the numbers mean, and where they stop being trustworthy */}
          <div className="bg-gray-50 border border-gray-200 rounded-xl p-4 text-xs text-gray-600 space-y-2 leading-relaxed">
            <p>
              <span className="font-semibold text-gray-700">On the books</span> is room-nights sold
              for {monthStartLabel} as of a date — it only goes up.{" "}
              <span className="font-semibold text-gray-700">Pickup</span> is what the{" "}
              {data.days}-day window itself added: the slope of that line, and the only one of the
              two that means speed. A month that has been on sale longer sits higher on the books
              without booking any faster.
            </p>
            <p>
              Nights are clipped to each stay month, so a stay crossing a month boundary counts
              only its nights inside that month. Revenue is prorated the same way and excludes the
              non-paying sources (blogger, KOL, house use), which still occupy a bed and so still
              count toward fill. Cancelled and no-show bookings are out of both.
            </p>
            <p>
              <span className="font-semibold text-gray-700">The one caveat worth knowing:</span>{" "}
              this curve is rebuilt from today's reservation data, so a booking made and since
              cancelled is missing from every point on the line, not just the points after it was
              cancelled. Last year has settled all of its cancellations; {monthStartLabel}
              {" "}{oneMonth ? "has" : "have"} not had them yet. The current line is the more
              generous of the two by construction — a lead of a few points is not proof of a
              real one.
            </p>
            {data.current.undated_room_nights > 0 && (
              <p>
                {nights(data.current.undated_room_nights)} room-nights come from bookings with no
                recorded booking date. They are counted as already on the books at the start of the
                window, since they cannot be placed in time.
              </p>
            )}
            {!currency && (
              <p>
                The group spans VND, TWD and JPY, so source revenue is shown in VND at the rate
                stored with each booking. Branch rows stay in their own currency. Put one branch in
                scope to read every figure natively.
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
