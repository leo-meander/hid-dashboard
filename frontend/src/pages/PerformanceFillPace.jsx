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
  ComposedChart, LineChart, Line, Bar,
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

function longDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
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

/** Percentage POINTS, signed. The gap between two occupancy rates. */
function ptsLabel(v) {
  if (v === null || v === undefined) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(1)} pts`;
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
  if (!iso) return "";
  const [, m, d] = iso.split("-");
  return `${d}/${m}`;
}

/** Stay months on offer: six back, fourteen forward. Past months are still
 *  worth opening — that is how you check whether a pace read came true. */
function monthOptions() {
  const now = new Date();
  const out = [];
  for (let i = -6; i <= 14; i++) {
    const d = new Date(now.getFullYear(), now.getMonth() + i, 1);
    const ym = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    out.push({ value: ym, label: monthLabel(ym), offset: i });
  }
  return out;
}

function defaultMonth() {
  const d = new Date();
  const n = new Date(d.getFullYear(), d.getMonth() + 1, 1);
  return `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, "0")}`;
}

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

  if (pace_index === null || pace_index === undefined) {
    return picked > 0
      ? {
          tone: "neutral",
          headline: "No year-ago pace to compare with",
          detail: `${nights(picked)} room-nights picked up this window. The same countdown to ${monthLabel(d.last_year?.stay_month)} booked nothing, so there is no speed to be faster than.`,
        }
      : {
          tone: "neutral",
          headline: "Nothing picked up in this window",
          detail: `Neither this year nor the same countdown to ${monthLabel(d.last_year?.stay_month)} booked anything for this month. Widen the window or check a different source.`,
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
 * Multi-select over every booking source the month saw.
 *
 * A plain <select> could only hold one, and the sources that matter most are
 * the ones the mix pages bundle away: "our own website" is a different question
 * from "everything we booked directly". So each raw source is its own checkbox,
 * with a category header that ticks all of its sources at once.
 */
function SourcePicker({ groups, selected, onToggle, onToggleGroup, onClear }) {
  const [open, setOpen] = useState(false);

  const label =
    selected.length === 0 ? "All sources"
      : selected.length === 1 ? selected[0]
      : `${selected.length} sources`;

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        className={`${SELECT_CLS} min-w-[13rem] flex items-center justify-between gap-2 text-left`}
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
            <button
              onClick={onClear}
              className={`w-full text-left px-3 py-1.5 text-sm hover:bg-gray-50 ${
                selected.length === 0 ? "text-indigo-600 font-medium" : "text-gray-600"
              }`}
            >
              All sources
            </button>

            {groups.map(({ category, names }) => {
              const allOn = names.every((n) => selected.includes(n));
              return (
                <div key={category} className="border-t border-gray-100 mt-1 pt-1">
                  <button
                    onClick={() => onToggleGroup(names)}
                    className="w-full flex items-center justify-between px-3 py-1
                               text-xs font-semibold uppercase tracking-wide
                               text-gray-400 hover:text-indigo-600"
                  >
                    <span>{category}</span>
                    <span className="normal-case tracking-normal font-medium">
                      {allOn ? "clear" : "all"}
                    </span>
                  </button>
                  {names.map((name) => (
                    <label key={name}
                      className="flex items-center gap-2 px-3 py-1.5 text-sm text-gray-700
                                 hover:bg-gray-50 cursor-pointer">
                      <input type="checkbox" className="accent-indigo-600"
                             checked={selected.includes(name)}
                             onChange={() => onToggle(name)} />
                      <span className="truncate">{name}</span>
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
              {compare && <th className="text-right font-medium px-4 py-2">vs LY</th>}
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
                      {ptsLabel(r.vs_last_year?.otb_occ_pts)}
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
  const [stayMonth, setStayMonth] = useState(defaultMonth);
  const [days, setDays] = useState(60);
  // null while a preset is active; {from, to} once a custom range is picked.
  const [range, setRange] = useState(null);
  const [sources, setSources] = useState([]);
  const [roomCategory, setRoomCategory] = useState("");

  // A custom range is expressed to the API in the same terms as a preset —
  // a day count ending at an as-of date — so both go down one code path.
  // Backwards ranges are read the way they were obviously meant.
  const asOf = range ? (range.to < range.from ? range.from : range.to) : null;
  const rangeStart = range ? (range.to < range.from ? range.to : range.from) : null;
  const rawDays = range ? daysBetweenISO(rangeStart, asOf) + 1 : days;
  const effectiveDays = Math.min(Math.max(rawDays, 1), MAX_WINDOW_DAYS);
  const cappedFrom = rawDays > MAX_WINDOW_DAYS ? shiftISO(asOf, -(MAX_WINDOW_DAYS - 1)) : null;

  const params = new URLSearchParams({
    stay_month: stayMonth,
    days: String(effectiveDays),
  });
  // Only sent for a custom range: on a preset the server's own "today" is the
  // authority, which keeps the window right for a user in another timezone.
  if (asOf) params.set("as_of", asOf);
  if (!isAll && selected) params.set("branch_id", selected);
  sources.forEach((s) => params.append("source", s));
  if (roomCategory) params.set("room_category", roomCategory);

  const { data, isPending, isError, error, isPlaceholderData } = useQuery({
    queryKey: ["fill-pace", stayMonth, effectiveDays, asOf, sources.join("|"),
               roomCategory, selected, isAll],
    queryFn: () => axios.get(`/api/metrics/fill-pace?${params}`).then((r) => r.data.data),
    placeholderData: keepPreviousData,
  });

  const compare = Boolean(data?.last_year);
  const currency = data?.scope?.currency;
  const v = useMemo(() => (data ? verdict(data) : null), [data]);

  // Daily pickup is spiky enough that raw bars hide the trend. A 7-day trailing
  // mean is what makes "speeding up / slowing down" visible at all.
  const chartData = useMemo(() => {
    if (!data?.curve) return [];
    const roll = (arr, key, i, n = 7) => {
      const from = Math.max(0, i - n + 1);
      const slice = arr.slice(from, i + 1);
      return slice.reduce((s, p) => s + (p[key] || 0), 0) / slice.length;
    };
    return data.curve.map((p, i) => ({
      ...p,
      day_avg: roll(data.curve, "day_room_nights", i),
      ly_day_avg: compare ? roll(data.curve, "ly_day_room_nights", i) : undefined,
    }));
  }, [data, compare]);

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
      .map(([category, names]) => ({ category, names }));
  }, [data, sources]);

  const toggleSource = (name) =>
    setSources((cur) => (cur.includes(name) ? cur.filter((s) => s !== name) : [...cur, name]));

  const toggleGroup = (names) =>
    setSources((cur) => {
      const all = names.every((n) => cur.includes(n));
      return all ? cur.filter((s) => !names.includes(s)) : [...new Set([...cur, ...names])];
    });

  const monthStartLabel = monthLabel(data?.stay_month || stayMonth);

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Fill Pace</h1>
          <p className="text-sm text-gray-500">
            How fast {monthStartLabel} is filling up, against the same countdown one year earlier
            <SyncBadge timestamp={data?.data_synced_at} />
          </p>
        </div>
      </div>

      {/* Controls */}
      <div className="bg-white border border-gray-200 rounded-xl p-4 flex flex-wrap items-end gap-4">
        <Field label="Stay month">
          <select className={SELECT_CLS} value={stayMonth}
                  onChange={(e) => setStayMonth(e.target.value)}>
            {monthOptions().map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}{o.offset < 0 ? " (past)" : ""}
              </option>
            ))}
          </select>
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
          <SourcePicker
            groups={sourceGroups}
            selected={sources}
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
            Booked {shortDate(data.window.from)}–{shortDate(data.window.to)} ({data.days}d) ·{" "}
            {data.days_out.to} days before {monthStartLabel.split(" ")[0]} 1
            {compare && (
              <>
                <br />
                Last year: {shortDate(data.last_year.window.from)}–{shortDate(data.last_year.window.to)}
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

          {/* Headline numbers */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            <Stat
              label="On the books"
              value={occ(data.current.otb_occ_pct)}
              sub={`${nights(data.current.otb_room_nights)} of ${nights(data.scope.available_room_nights)} room-nights`}
              hint={`${data.scope.units_in_scope} units × ${data.days_in_month} nights`}
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
            <Stat
              label="vs last year, same point"
              value={compare ? ptsLabel(data.vs_last_year.otb_occ_pts) : "—"}
              sub={compare
                ? `LY was ${occ(data.last_year.otb_occ_pct)} at ${data.days_out.to} days out`
                : "comparison off"}
              subTone="text-gray-500"
              hint="Difference in fill %, both read at the same distance from the month"
            />
            <Stat
              label="Last year finished at"
              value={compare ? occ(data.last_year.final_occ_pct) : "—"}
              sub={compare
                ? `${nights(data.last_year.remaining_after_window_room_nights)} room-nights still came in after this point`
                : "comparison off"}
              subTone="text-gray-500"
              hint="How much of the month was still left to sell from here"
            />
          </div>

          {/* Cumulative fill curve */}
          <div className="bg-white border border-gray-200 rounded-xl p-4">
            <h2 className="text-sm font-semibold text-gray-800">
              How full the month was, day by day
            </h2>
            <p className="text-xs text-gray-500 mt-0.5 mb-3">
              Cumulative fill %, counting down to {monthStartLabel.split(" ")[0]} 1. Both years
              read at the same distance from the month, so the lines are directly comparable.
            </p>
            <ResponsiveContainer width="100%" height={280}>
              <LineChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis
                  dataKey="days_out"
                  reversed={false}
                  tick={{ fontSize: 11, fill: "#9ca3af" }}
                  tickFormatter={(d) => (d >= 0 ? `${d}d` : `+${-d}d`)}
                  label={{
                    value: "days before the month starts",
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
                  labelFormatter={(d) => `${d >= 0 ? d : -d} days ${d >= 0 ? "before" : "into"} the month`}
                  formatter={(val, name, item) => [
                    `${val === null || val === undefined ? "—" : `${Number(val).toFixed(1)}%`}`,
                    name,
                  ]}
                />
                <Legend iconSize={10} wrapperStyle={{ fontSize: 12 }} />
                <Line type="monotone" dataKey="otb_occ_pct" name={`${monthStartLabel} (on the books)`}
                      stroke={THIS_YEAR} strokeWidth={2.5} dot={false} />
                {compare && (
                  <Line type="monotone" dataKey="ly_otb_occ_pct"
                        name={`${monthLabel(data.last_year.stay_month)} (same countdown)`}
                        stroke={LAST_YEAR} strokeWidth={2} strokeDasharray="5 4" dot={false} />
                )}
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* Daily pickup — the slope, smoothed */}
          <div className="bg-white border border-gray-200 rounded-xl p-4">
            <h2 className="text-sm font-semibold text-gray-800">
              Room-nights booked per day
            </h2>
            <p className="text-xs text-gray-500 mt-0.5 mb-3">
              Bars are what each booking day added; the lines are a 7-day trailing average, which
              is what makes speeding up or slowing down visible through the noise.
            </p>
            <ResponsiveContainer width="100%" height={240}>
              <ComposedChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis
                  dataKey="days_out"
                  tick={{ fontSize: 11, fill: "#9ca3af" }}
                  tickFormatter={(d) => (d >= 0 ? `${d}d` : `+${-d}d`)}
                />
                <YAxis tick={{ fontSize: 11, fill: "#9ca3af" }} width={44} />
                <Tooltip
                  contentStyle={{ fontSize: 12, borderRadius: 8 }}
                  labelFormatter={(d) => `${d >= 0 ? d : -d} days ${d >= 0 ? "before" : "into"} the month`}
                  formatter={(val, name) => [
                    val === null || val === undefined ? "—" : Number(val).toFixed(1),
                    name,
                  ]}
                />
                <Legend iconSize={10} wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="day_room_nights" name="Booked that day" fill="#c7d2fe" />
                <Line type="monotone" dataKey="day_avg" name="This year (7-day avg)"
                      stroke={THIS_YEAR} strokeWidth={2.5} dot={false} />
                {compare && (
                  <Line type="monotone" dataKey="ly_day_avg" name="Last year (7-day avg)"
                        stroke={LAST_YEAR} strokeWidth={2} strokeDasharray="5 4" dot={false} />
                )}
              </ComposedChart>
            </ResponsiveContainer>
          </div>

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
              Nights are clipped to the stay month, so a stay crossing the month boundary counts
              only its nights inside it. Revenue is prorated the same way and excludes the
              non-paying sources (blogger, KOL, house use), which still occupy a bed and so still
              count toward fill. Cancelled and no-show bookings are out of both.
            </p>
            <p>
              <span className="font-semibold text-gray-700">The one caveat worth knowing:</span>{" "}
              this curve is rebuilt from today's reservation data, so a booking made and since
              cancelled is missing from every point on the line, not just the points after it was
              cancelled. Last year's month has settled all of its cancellations; {monthStartLabel}
              {" "}has not had them yet. The current line is the more generous of the two by
              construction — a lead of a few points is not proof of a real one.
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
