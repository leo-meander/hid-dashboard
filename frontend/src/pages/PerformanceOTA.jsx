/**
 * OTA Channel Mix — Cancel Rate & Check-in Rate pivot table
 * Same format as Channel Mix: channels × periods, two section blocks
 */
import { useEffect, useState } from "react";
import axios from "axios";
import SyncBadge from "../components/SyncBadge";
import { useBranch } from "../context/BranchContext";

// ── helpers ──────────────────────────────────────────────────────────────────
function pct(v) {
  if (v === null || v === undefined) return null;
  return `${(v * 100).toFixed(2)}%`;
}

// Cancel rate: >25% red, 10-25% amber, 0-10% green  (matches OTA ranking rule)
function cancelBg(rate) {
  if (rate === null) return {};
  if (rate > 0.25) return { backgroundColor: "#fca5a5" };   // red-300
  if (rate > 0.10) return { backgroundColor: "#fde68a" };   // amber-200
  return { backgroundColor: "#bbf7d0" };                    // green-200
}

function cancelTextColor(rate) {
  if (rate === null) return "text-gray-400";
  if (rate > 0.25) return "text-red-800 font-semibold";
  if (rate > 0.10) return "text-amber-800";
  return "text-green-800";
}

// Check-in share: intensity gradient (higher share = greener)
function checkinBg(rate) {
  if (rate === null) return {};
  const t = Math.min(Math.max(rate / 0.5, 0), 1); // cap at 50% share
  const r = Math.round(220 - t * 85);
  const g = Math.round(200 + t * 47);
  const b = Math.round(220 - t * 75);
  return { backgroundColor: `rgb(${r},${g},${b})` };
}

// ── main component ────────────────────────────────────────────────────────────
export default function PerformanceOTA() {
  const { selected, isAll } = useBranch();
  const [mode,     setMode]     = useState("daily");
  const [months,   setMonths]   = useState(3);      // monthly mode: how many months to show
  const [dateType, setDateType] = useState("check_in");
  const [data,     setData]     = useState(null);
  const [loading,  setLoading]  = useState(true);

  const bParam = !isAll && selected ? `&branch_id=${selected}` : "";
  const mParam = mode === "monthly" ? `&months=${months}` : "";

  useEffect(() => {
    setLoading(true);
    axios.get(`/api/metrics/rates-trend?mode=${mode}&date_type=${dateType}${bParam}${mParam}`)
      .then(r => setData(r.data.data))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [mode, months, dateType, selected, isAll]);

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">OTA Channel Mix</h1>
          <p className="text-sm text-gray-500">
            Cancellation &amp; check-in rate by booking source
            <SyncBadge timestamp={data?.data_synced_at} />
          </p>
        </div>

        {/* Mode selector */}
        <div className="flex items-center gap-2">
          {[
            ["daily",   "Daily (7 days)"],
            ["weekly",  "Weekly (7 weeks)"],
            ["monthly", `Monthly (${months} months)`],
          ].map(([k, label]) => (
            <button key={k} onClick={() => setMode(k)}
              className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                mode === k
                  ? "bg-indigo-600 text-white shadow-sm"
                  : "bg-white border border-gray-200 text-gray-600 hover:border-indigo-300"
              }`}>
              {label}
            </button>
          ))}

          {/* Month-count picker — only in monthly mode */}
          {mode === "monthly" && (
            <label className="flex items-center gap-1.5 text-sm text-gray-500 ml-1">
              <span>Show</span>
              <input
                type="number" min={1} max={36} value={months}
                onChange={e => {
                  const n = parseInt(e.target.value, 10);
                  setMonths(Number.isNaN(n) ? 1 : Math.min(36, Math.max(1, n)));
                }}
                className="w-16 px-2 py-1.5 rounded-lg border border-gray-200 text-gray-700 text-sm text-center tabular-nums focus:outline-none focus:border-indigo-400"
              />
              <span>months</span>
            </label>
          )}
        </div>
      </div>

      {/* Date type tabs */}
      <div className="flex border-b border-gray-200">
        {[
          ["check_in", "By Check-in Date"],
          ["booked",   "By Date Booked"],
        ].map(([k, label]) => (
          <button key={k} onClick={() => setDateType(k)}
            className={`px-5 py-2.5 text-sm font-medium transition-colors border-b-2 -mb-px ${
              dateType === k
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
            }`}>
            {label}
          </button>
        ))}
      </div>

      {loading || !data ? (
        <div className="text-gray-400 animate-pulse py-12 text-center">Loading…</div>
      ) : data.channels.length === 0 ? (
        <div className="bg-white rounded-xl border p-8 text-center text-gray-400">No data for this period.</div>
      ) : (
        <RatesPivotTable periods={data.periods} channels={data.channels} />
      )}
    </div>
  );
}

// ── Pivot table with two sections ─────────────────────────────────────────────
function RatesPivotTable({ periods, channels }) {
  const grandBookings = channels.reduce((s, ch) => s + (ch.total || 0), 0);
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="bg-gray-800 text-white text-xs">
            <th className="px-4 py-2.5 text-left font-medium uppercase tracking-wide w-52 sticky left-0 bg-gray-800 z-10">
              Channel
            </th>
            {periods.map(p => (
              <th key={p} className="px-3 py-2.5 text-center font-medium whitespace-nowrap">
                {p}
              </th>
            ))}
            <th className="px-3 py-2.5 text-center font-medium">Bookings (share)</th>
          </tr>
        </thead>

        <tbody>
          {/* ── Cancel Rate section ── */}
          <SectionHeader label="Cancellation Rate %" colSpan={periods.length + 2} color="bg-red-700" />
          {channels.map((ch, i) => (
            <DataRow key={`cancel-${ch.channel}`}
              channel={ch.channel}
              isDirect={ch.is_direct}
              total={ch.total}
              grandTotal={grandBookings}
              cells={ch.cancel_cells}
              bgFn={cancelBg}
              valueKey="rate"
              altRow={i % 2 === 1}
            />
          ))}
          <TotalRow label="Avg cancel rate" periods={periods} channels={channels} grandTotal={grandBookings} />

          {/* ── Check-in Rate section ── */}
          <SectionHeader label="Check-in Rate %" colSpan={periods.length + 2} color="bg-green-700" />
          {channels.map((ch, i) => (
            <DataRow key={`checkin-${ch.channel}`}
              channel={ch.channel}
              isDirect={ch.is_direct}
              total={ch.total}
              grandTotal={grandBookings}
              cells={ch.checkin_cells}
              bgFn={checkinBg}
              valueKey="rate"
              altRow={i % 2 === 1}
            />
          ))}
          <CheckinTotalRow periods={periods} channels={channels} grandTotal={grandBookings} />
        </tbody>
      </table>

      {/* Legend */}
      <div className="px-4 py-2.5 border-t border-gray-100 flex flex-wrap gap-5 text-xs text-gray-500">
        <span className="font-medium text-gray-400 uppercase tracking-wide">Cancel Rate:</span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-8 h-3 rounded" style={{ background: "#bbf7d0" }} />
          0–10% (good)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-8 h-3 rounded" style={{ background: "#fde68a" }} />
          10–25% (warning)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-8 h-3 rounded" style={{ background: "#fca5a5" }} />
          &gt;25% (bad)
        </span>
        <span className="text-gray-300 mx-2">|</span>
        <span className="font-medium text-gray-400 uppercase tracking-wide">Check-in %:</span>
        <span className="text-gray-400">source check-ins ÷ total check-ins all sources (sums to 100% per period)</span>
      </div>
    </div>
  );
}

function SectionHeader({ label, colSpan, color }) {
  return (
    <tr className={`${color} text-white`}>
      <td className={`px-4 py-1.5 text-xs font-semibold uppercase tracking-wide sticky left-0 ${color} z-10`}
        colSpan={colSpan}>
        {label}
      </td>
    </tr>
  );
}

function DataRow({ channel, isDirect, total, grandTotal, cells, bgFn, valueKey, altRow }) {
  const isLocalTA = channel === "Local travel agency";
  const rowBase = isDirect
    ? "bg-green-50 text-green-900"
    : isLocalTA
      ? "bg-sky-50 text-sky-900"
      : altRow ? "bg-red-50 text-gray-800" : "bg-white text-gray-800";
  // labelBg backs the sticky left column — must be fully opaque, otherwise
  // horizontally-scrolled cells bleed through it (e.g. 24-month view).
  const labelBg = isDirect ? "bg-green-50" : isLocalTA ? "bg-sky-50" : altRow ? "bg-red-50" : "bg-white";
  const totalBg = isDirect
    ? "bg-green-50 text-green-700"
    : isLocalTA
      ? "bg-sky-50 text-sky-700"
      : "bg-gray-50 text-gray-600";
  const share = grandTotal > 0 ? (total / grandTotal) * 100 : 0;

  return (
    <tr className={`border-b border-gray-100 hover:brightness-95 transition-all ${rowBase}`}>
      <td className={`px-4 py-2 text-xs font-medium sticky left-0 z-10 whitespace-nowrap ${labelBg}`}>
        {isDirect ? "Direct (Web/Walk-in/Email/Extension/FB/Phone/PR)" : channel}
      </td>

      {cells.map((cell, ci) => {
        const rate     = cell[valueKey];
        const bg       = bgFn(rate);
        const textCls  = bgFn === cancelBg ? cancelTextColor(rate) : "text-gray-800";
        // Cancel section: show channel's total bookings for the period
        // Check-in section: show channel's checked-in bookings for the period
        const count = bgFn === cancelBg ? cell.total : cell.checked_in;
        return (
          <td key={ci} style={bg}
            className={`px-3 py-2 text-center text-xs tabular-nums ${textCls}`}>
            {rate !== null ? (
              <div className="flex flex-col leading-tight">
                <span>{pct(rate)}</span>
                <span className="font-normal opacity-70 text-[11px]">({count.toLocaleString()})</span>
              </div>
            ) : <span className="text-gray-300">—</span>}
          </td>
        );
      })}

      {/* Total column: total bookings + share of grand total */}
      <td className={`px-3 py-2 text-center text-xs font-semibold tabular-nums ${totalBg}`}>
        <div className="flex flex-col leading-tight">
          <span>{total.toLocaleString()}</span>
          <span className="font-normal opacity-70 text-[11px]">({share.toFixed(2)}%)</span>
        </div>
      </td>
    </tr>
  );
}

function TotalRow({ label, periods, channels, grandTotal }) {
  // Weighted average cancel rate + total bookings per period
  const periodStats = periods.map((_, pi) => {
    let totalN = 0, totalD = 0;
    channels.forEach(ch => {
      const cell = ch.cancel_cells[pi];
      if (!cell) return;
      if (cell.total > 0) { totalN += cell.cancelled; totalD += cell.total; }
    });
    return {
      rate:  totalD > 0 ? totalN / totalD : null,
      total: totalD,
    };
  });

  const grandN = channels.reduce((s, ch) =>
    s + ch.cancel_cells.reduce((ss, c) => ss + c.cancelled, 0), 0);
  const grandD = channels.reduce((s, ch) =>
    s + ch.cancel_cells.reduce((ss, c) => ss + c.total, 0), 0);

  return (
    <tr className="bg-gray-100 font-semibold text-gray-700 text-xs border-t border-gray-300">
      <td className="px-4 py-2 sticky left-0 bg-gray-100 z-10 italic text-gray-500">{label}</td>
      {periodStats.map((s, i) => (
        <td key={i} className="px-3 py-2 text-center tabular-nums text-gray-600">
          {s.rate !== null ? (
            <div className="flex flex-col leading-tight">
              <span>{pct(s.rate)}</span>
              <span className="font-normal opacity-70 text-[11px]">({s.total.toLocaleString()})</span>
            </div>
          ) : <span className="text-gray-300">—</span>}
        </td>
      ))}
      <td className="px-3 py-2 text-center tabular-nums">
        {grandTotal > 0 ? (
          <div className="flex flex-col leading-tight">
            <span>{grandTotal.toLocaleString()}</span>
            <span className="font-normal opacity-70 text-[11px]">({grandD > 0 ? pct(grandN / grandD) : "—"})</span>
          </div>
        ) : "—"}
      </td>
    </tr>
  );
}

// Check-in section total row: sum of all shares = 100% per period
function CheckinTotalRow({ periods, channels, grandTotal }) {
  const periodStats = periods.map((_, pi) => {
    let sum = 0, totalCkin = 0;
    channels.forEach(ch => {
      const cell = ch.checkin_cells[pi];
      if (!cell) return;
      sum += cell.rate ?? 0;
      totalCkin = cell.total; // same total across all channels for the period
    });
    return { rate: sum > 0 ? sum : null, total: totalCkin };
  });

  return (
    <tr className="bg-gray-100 font-semibold text-gray-700 text-xs border-t border-gray-300">
      <td className="px-4 py-2 sticky left-0 bg-gray-100 z-10 italic text-gray-500">Total (all sources)</td>
      {periodStats.map((s, i) => (
        <td key={i} className="px-3 py-2 text-center tabular-nums text-gray-600">
          {s.rate !== null ? (
            <div className="flex flex-col leading-tight">
              <span>{pct(s.rate)}</span>
              <span className="font-normal opacity-70 text-[11px]">({s.total.toLocaleString()})</span>
            </div>
          ) : <span className="text-gray-300">—</span>}
        </td>
      ))}
      <td className="px-3 py-2 text-center tabular-nums">
        {grandTotal > 0 ? (
          <div className="flex flex-col leading-tight">
            <span>{grandTotal.toLocaleString()}</span>
            <span className="font-normal opacity-70 text-[11px]">(100.00%)</span>
          </div>
        ) : "—"}
      </td>
    </tr>
  );
}
