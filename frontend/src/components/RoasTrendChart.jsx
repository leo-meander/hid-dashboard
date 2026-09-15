/**
 * RoasTrendChart — ROAS by month across the year, one line per channel.
 *
 * KOL rides a second (right) axis on purpose: its ROAS runs in the hundreds
 * against single digits for Paid Ads and CRM, so sharing one axis would flatten
 * the other three lines into the baseline.
 *
 * A month with no cost recorded has no ROAS — it is plotted as a gap, never a
 * zero, so an unspent month can't read as a collapse.
 */
import { useState } from "react";
import {
  ResponsiveContainer,
  ComposedChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";

const SERIES = [
  { key: "total", name: "Blended", color: "#111827", axis: "left", width: 2.5 },
  { key: "paid_ads", name: "Paid Ads", color: "#3b82f6", axis: "left", width: 2 },
  { key: "crm", name: "CRM", color: "#10b981", axis: "left", width: 2 },
  { key: "kol", name: "KOL", color: "#a855f7", axis: "right", width: 2, dashed: true },
];

// 0 means "no cost recorded", not "no return" — keep it off the line.
function roasOf(point, key) {
  const v = point?.[key]?.roas;
  return v == null || v === 0 ? null : v;
}

// Legend entries carry the plotted dataKey ("kol_roas"); the series is "kol".
function seriesKeyOf(entry) {
  return String(entry?.dataKey || "").replace(/_roas$/, "");
}

function fmtRoas(v) {
  return v == null ? "—" : v.toFixed(2) + "x";
}

function fmtMoney(v) {
  if (v == null) return "—";
  return new Intl.NumberFormat("en").format(Math.round(v));
}

function TrendTooltip({ active, payload, label, cur }) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload;
  if (!point) return null;
  return (
    <div className="bg-white border border-gray-200 rounded-lg shadow-sm px-3 py-2 text-xs">
      <p className="font-semibold text-gray-800 mb-1.5">{label} {point.month?.slice(0, 4)}</p>
      {point.unavailable ? (
        <p className="text-gray-400">Not available</p>
      ) : (
        <table>
          <tbody>
            {SERIES.map(({ key, name, color }) => {
              const d = point[key];
              if (!d) return null;
              return (
                <tr key={key}>
                  <td className="pr-3 whitespace-nowrap">
                    <span className="inline-block w-2 h-2 rounded-full mr-1.5" style={{ background: color }} />
                    {name}
                  </td>
                  <td className="pr-3 text-right font-semibold text-gray-800">{fmtRoas(roasOf(point, key))}</td>
                  <td className="text-right text-gray-400 whitespace-nowrap">
                    {fmtMoney(d.revenue)} / {fmtMoney(d.cost)} {cur}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function RoasTrendChart({ months = [], cur = "VND", year, isPending }) {
  // Click a legend entry to drop that line — KOL's scale in particular is
  // easier to read once the rest are out of the way, and vice versa.
  const [hidden, setHidden] = useState({});
  const toggle = (key) => setHidden((h) => ({ ...h, [key]: !h[key] }));

  const data = months.map((m) => ({
    ...m,
    total_roas: roasOf(m, "total"),
    paid_ads_roas: roasOf(m, "paid_ads"),
    crm_roas: roasOf(m, "crm"),
    kol_roas: roasOf(m, "kol"),
  }));

  const hasKol = data.some((d) => d.kol_roas != null) && !hidden.kol;

  return (
    <div className="bg-white rounded-lg border p-4">
      <div className="flex items-baseline justify-between mb-1">
        <p className="text-sm font-semibold text-gray-700">ROAS Trend — {year}</p>
        <p className="text-xs text-gray-400">Jan → today · click a channel to hide it</p>
      </div>
      {hasKol && (
        <p className="text-xs text-gray-400 mb-2">
          KOL reads on the right axis — its ROAS runs far above the other channels.
        </p>
      )}
      {isPending ? (
        <div className="flex items-center justify-center h-64 text-gray-400 text-sm animate-pulse">Loading...</div>
      ) : data.length === 0 ? (
        <div className="flex items-center justify-center h-64 text-gray-400 text-sm">No data available</div>
      ) : (
        <ResponsiveContainer width="100%" height={280}>
          <ComposedChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
            <XAxis dataKey="label" tick={{ fontSize: 11, fill: "#9ca3af" }} tickLine={false} axisLine={false} />
            <YAxis
              yAxisId="left"
              tickFormatter={(v) => v + "x"}
              tick={{ fontSize: 11, fill: "#9ca3af" }}
              tickLine={false}
              axisLine={false}
              width={48}
            />
            <YAxis
              yAxisId="right"
              orientation="right"
              hide={!hasKol}
              tickFormatter={(v) => v + "x"}
              tick={{ fontSize: 11, fill: "#c084fc" }}
              tickLine={false}
              axisLine={false}
              width={52}
            />
            <Tooltip content={<TrendTooltip cur={cur} />} />
            <Legend
              iconSize={10}
              wrapperStyle={{ fontSize: 12, cursor: "pointer" }}
              onClick={(entry) => {
                const key = seriesKeyOf(entry);
                if (key) toggle(key);
              }}
              formatter={(value, entry) => (
                <span style={{ color: hidden[seriesKeyOf(entry)] ? "#d1d5db" : "#4b5563" }}>
                  {value}
                </span>
              )}
            />
            {SERIES.map(({ key, name, color, axis, width, dashed }) => (
              <Line
                key={key}
                yAxisId={axis}
                type="monotone"
                dataKey={key + "_roas"}
                name={name}
                stroke={color}
                strokeWidth={width}
                strokeDasharray={dashed ? "5 4" : undefined}
                dot={{ r: 2.5, strokeWidth: 0, fill: color }}
                activeDot={{ r: 4 }}
                connectNulls={false}
                hide={!!hidden[key]}
              />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
