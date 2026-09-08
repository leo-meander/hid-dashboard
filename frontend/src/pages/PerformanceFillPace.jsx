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
    const roll = (arr, key, i, n = SMOOTHING_DAYS) => {
      if (i < n - 1) return null;
      const slice = arr.slice(i - n + 1, i + 1);
      return slice.reduce((s, p) => s + (p[key] || 0), 0) / n;
    };
    return data.curve.map((p, i) => ({
      ...p,
      day_avg: roll(data.curve, "day_room_nights", i),
      ly_day_avg: compare ? roll(data.curve, "ly_day_room_nights", i) : undefined,
      prev_day_avg: roll(data.curve, "prev_day_room_nights", i),
    }));
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
              label="On the books"
              value={occ(data.current.otb_occ_pct)}
              sub={`${nights(data.current.otb_room_nights)} of ${nights(data.scope.available_room_nights)} room-nights`}
              hint={`${data.scope.units_in_scope} units × ${data.stay_days} nights`}
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

          {/* Cumulative fill curve */}
          <div className="bg-white border border-gray-200 rounded-xl p-4">
            <h2 className="text-sm font-semibold text-gray-800">
              How full it is — position, not speed
            </h2>
            <p className="text-xs text-gray-500 mt-0.5 mb-3">
              {oneMonth
                ? `Cumulative fill %, counting down to ${monthStartLabel.split(" ")[0]} 1. Both years read at the same distance from the month, so the lines are directly comparable.`
                : "Cumulative fill % across every selected month, by booking date. Each month is still measured against its own countdown a year earlier before they are added up."}
            </p>
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis
                  dataKey={oneMonth ? "days_out" : "date"}
                  tick={{ fontSize: 11, fill: "#9ca3af" }}
                  tickFormatter={(d) =>
                    oneMonth ? (d >= 0 ? `${d}d` : `+${-d}d`) : shortDate(d)}
                  label={{
                    value: oneMonth ? "days before the month starts" : "booking date",
                    position: "insideBottom",
                    offset: -2,
                    style: { fontSize: 11, fill: "#9ca3af" },
                  }}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: "#9ca3af" }}
                  tickFormatter={(v2) => `${v2}%`}
                  width={44}
                />
                <Tooltip
                  contentStyle={{ fontSize: 12, borderRadius: 8 }}
                  labelFormatter={axisLabel}
                  formatter={(val, name) => [
                    val === null || val === undefined ? "—" : `${Number(val).toFixed(1)}%`,
                    name,
                  ]}
                />
                <Legend iconSize={10} wrapperStyle={{ fontSize: 12 }} />
                <Line type="monotone" dataKey="otb_occ_pct" name={`${monthStartLabel} (on the books)`}
                      stroke={THIS_YEAR} strokeWidth={2.5} dot={false} />
                {compare && (
                  <Line type="monotone" dataKey="ly_otb_occ_pct"
                        name={`${monthsLabel(data.last_year.stay_months)} (same countdown)`}
                        stroke={LAST_YEAR} strokeWidth={2} strokeDasharray="5 4" dot={false} />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Daily pickup — the slope, smoothed */}
          <div className="bg-white border border-gray-200 rounded-xl p-4">
            <h2 className="text-sm font-semibold text-gray-800">
              How fast it is selling
            </h2>
            <p className="text-xs text-gray-500 mt-0.5 mb-3">
              Room-nights sold per booking day, smoothed over {SMOOTHING_DAYS} days. This is the
              speed: the chart above is the height reached, this one is how quickly it is being
              reached. Above the other line means selling faster than the same run-up last year.
              The line starts on day {SMOOTHING_DAYS} — before that there is not a full week to
              average{endsToday ? " — and today is left off, being a day still in progress." : "."}
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
