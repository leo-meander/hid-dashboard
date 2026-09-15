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

function TrendTooltip({ active, payload, label, cur, series = SERIES }) {
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
            {series.map(({ key, name, color }) => {
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
  // Pick one channel to read it on its own — alone it gets the left axis and
  // the whole vertical range, which is the only way KOL's hundreds and CRM's
  // single digits are both legible.
  const [view, setView] = useState("all");
  // Within "All channels", a legend click still drops a line. Cleared on every
  // switch so the picker never inherits a hidden series from the last view.
  const [hidden, setHidden] = useState({});
  const toggle = (key) => setHidden((h) => ({ ...h, [key]: !h[key] }));
  const pick = (key) => {
    setView(key);
    setHidden({});
  };

  const shown = view === "all" ? SERIES : SERIES.filter((s) => s.key === view);
  const isHidden = (key) => (view === "all" ? !!hidden[key] : key !== view);

  const data = months.map((m) => ({
    ...m,
    total_roas: roasOf(m, "total"),
    paid_ads_roas: roasOf(m, "paid_ads"),
    crm_roas: roasOf(m, "crm"),
    kol_roas: roasOf(m, "kol"),
  }));

  // KOL only needs the second axis while it shares the chart with the others.
  const hasKol =
    view === "all" && data.some((d) => d.kol_roas != null) && !hidden.kol;
  const axisOf = (series) => (view === "all" ? series.axis : "left");

  return (
    <div className="bg-white rounded-lg border p-4">
      <div className="flex items-baseline justify-between flex-wrap gap-2 mb-2">
        <p className="text-sm font-semibold text-gray-700">ROAS Trend — {year}</p>
        <p className="text-xs text-gray-400">Jan → today</p>
      </div>
      <div className="flex gap-0.5 bg-gray-100 rounded-lg p-1 w-fit mb-2">
        {[{ key: "all", name: "All channels" }, ...SERIES].map(({ key, name }) => (
          <button
            key={key}
            onClick={() => pick(key)}
            className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
              view === key ? "bg-white text-gray-800 shadow-sm" : "text-gray-500 hover:text-gray-700"
            }`}
          >
            {name}
          </button>
        ))}
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
            <Tooltip content={<TrendTooltip cur={cur} series={shown} />} />
            <Legend
              iconSize={10}
              wrapperStyle={{ fontSize: 12, cursor: view === "all" ? "pointer" : "default" }}
              onClick={(entry) => {
                if (view !== "all") return;
                const key = seriesKeyOf(entry);
                if (key) toggle(key);
              }}
              formatter={(value, entry) => (
                <span style={{ color: isHidden(seriesKeyOf(entry)) ? "#d1d5db" : "#4b5563" }}>
                  {value}
                </span>
              )}
            />
            {SERIES.map((series) => {
              const { key, name, color, width, dashed } = series;
              return (
              <Line
                key={key}
                yAxisId={axisOf(series)}
                type="monotone"
                dataKey={key + "_roas"}
                name={name}
                stroke={color}
                strokeWidth={width}
                strokeDasharray={dashed ? "5 4" : undefined}
                dot={{ r: 2.5, strokeWidth: 0, fill: color }}
                activeDot={{ r: 4 }}
                connectNulls={false}
                hide={isHidden(key)}
              />
              );
            })}
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
