/**
 * CountryDetail — drill-down with YoY comparison chart
 */
import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import axios from "axios";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer, Cell,
} from "recharts";
import CountryBadge from "../components/CountryBadge";
import SyncBadge from "../components/SyncBadge";

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function flagEmoji(code) {
  if (!code || code.length !== 2) return "🌐";
  const cp = [...code.toUpperCase()].map((c) => 127397 + c.charCodeAt(0));
  return String.fromCodePoint(...cp);
}

export default function CountryDetail() {
  const { code } = useParams();
  const currentYear = new Date().getFullYear();

  const [year,       setYear]       = useState(currentYear);
  const [yoyData,    setYoyData]    = useState([]);
  const [rankData,   setRankData]   = useState(null);
  const [loading,    setLoading]    = useState(true);
  const [loadingRank, setLoadingRank] = useState(true);

  // Load YoY trend data
  useEffect(() => {
    setLoading(true);
    axios.get(`/api/countries/${code}/trend?year=${year}`)
      .then((r) => setYoyData(r.data.data || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [code, year]);

  // Load ranking info for this country
  useEffect(() => {
    setLoadingRank(true);
    axios.get(`/api/countries/ranking?year=${year}`)
      .then((r) => {
        const all = r.data.data || [];
        const found = all.find((c) => c.country_code === code);
        setRankData(found || null);
      })
      .catch(console.error)
      .finally(() => setLoadingRank(false));
  }, [code, year]);

  // Build month-by-month comparison chart data
  const chartData = MONTHS.map((m, i) => {
    const prevYear = yoyData.find((d) => d.year === year - 1 && d.month === i + 1);
    const thisYear = yoyData.find((d) => d.year === year     && d.month === i + 1);
    return {
      month: m,
      [year - 1]: prevYear?.count || 0,
      [year]:     thisYear?.count || 0,
      rev_prev:   prevYear?.revenue_native || 0,
      rev_this:   thisYear?.revenue_native || 0,
    };
  });

  const totalThis = yoyData.filter((d) => d.year === year).reduce((s, d) => s + d.count, 0);
  const totalPrev = yoyData.filter((d) => d.year === year - 1).reduce((s, d) => s + d.count, 0);
  const yoyChange = totalPrev > 0 ? (totalThis - totalPrev) / totalPrev : null;

  const countryName = rankData?.country || code;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4 flex-wrap">
        <Link to="/countries" className="text-sm text-indigo-600 hover:underline">
          ← Back to Rankings
        </Link>
      </div>

      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <span className="text-4xl">{flagEmoji(code)}</span>
          <div>
            <h1 className="text-xl font-bold text-gray-800">{countryName}</h1>
            <div className="flex items-center gap-2 mt-0.5">
              <span className="text-sm text-gray-500">{code}</span>
              {rankData && <CountryBadge tier={rankData.tier} />}
              {rankData?.rank && (
                <span className="text-xs text-gray-400">Rank #{rankData.rank}</span>
              )}
              <SyncBadge timestamp={rankData?.data_synced_at} className="text-xs text-gray-400" prefix="" />
            </div>
          </div>
        </div>
        <select value={year} onChange={(e) => setYear(Number(e.target.value))}
          className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm">
          {[currentYear - 2, currentYear - 1, currentYear].map((y) => (
            <option key={y} value={y}>{y}</option>
          ))}
        </select>
      </div>

      {/* Summary KPI cards */}
      {!loadingRank && rankData && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            { label: "Score", value: (rankData.score * 100).toFixed(0), suffix: "/100" },
            {
              label: "YoY Growth",
              value: yoyChange != null
                ? `${yoyChange > 0 ? "+" : ""}${(yoyChange * 100).toFixed(2)}%`
                : "—",
              color: yoyChange == null ? "" : yoyChange > 0 ? "text-emerald-600" : "text-red-500",
            },
            { label: `${year} Bookings`, value: totalThis.toLocaleString() },
            { label: `${year - 1} Bookings`, value: totalPrev.toLocaleString() },
          ].map(({ label, value, suffix, color }) => (
            <div key={label} className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm text-center">
              <p className="text-xs text-gray-400 uppercase">{label}</p>
              <p className={`text-2xl font-bold text-gray-800 mt-1 ${color || ""}`}>
                {value}{suffix && <span className="text-sm font-normal text-gray-400 ml-1">{suffix}</span>}
              </p>
            </div>
          ))}
        </div>
      )}

      {/* YoY Booking Count chart */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
        <p className="text-sm font-semibold text-gray-700 mb-3">
          Monthly Bookings — {year - 1} vs {year}
        </p>
        {loading ? (
          <div className="h-52 flex items-center justify-center text-gray-400 animate-pulse">Loading…</div>
        ) : (
          <ResponsiveContainer width="100%" height={240}>
            <ComposedChart data={chartData} margin={{ left: 0, right: 8 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="month" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
              <Tooltip />
              <Legend />
              <Bar dataKey={year - 1} name={`${year - 1}`} fill="#cbd5e1" radius={[3,3,0,0]} barSize={10} />
              <Bar dataKey={year}     name={`${year}`}     fill="#6366f1" radius={[3,3,0,0]} barSize={10} />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* YoY Revenue chart */}
      <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm">
        <p className="text-sm font-semibold text-gray-700 mb-3">
          Monthly Revenue — {year - 1} vs {year}
        </p>
        {loading ? (
          <div className="h-52 flex items-center justify-center text-gray-400 animate-pulse">Loading…</div>
        ) : (
          <ResponsiveContainer width="100%" height={240}>
            <ComposedChart data={chartData} margin={{ left: 8, right: 8 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="month" tick={{ fontSize: 11 }} />
              <YAxis
                tick={{ fontSize: 11 }}
                tickFormatter={(v) => new Intl.NumberFormat("en").format(Math.round(v))}
              />
              <Tooltip
                formatter={(v) => new Intl.NumberFormat("en").format(Math.round(v))}
              />
              <Legend />
              <Bar dataKey="rev_prev" name={`${year - 1}`} fill="#cbd5e1" radius={[3,3,0,0]} barSize={10} />
              <Bar dataKey="rev_this" name={`${year}`}     fill="#10b981" radius={[3,3,0,0]} barSize={10} />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Monthly breakdown table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase border-b border-gray-100">
              <th className="px-4 py-3 text-left">Month</th>
              <th className="px-4 py-3 text-right">{year - 1} Bookings</th>
              <th className="px-4 py-3 text-right">{year} Bookings</th>
              <th className="px-4 py-3 text-right">Change</th>
              <th className="px-4 py-3 text-right">{year} Revenue</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {chartData.map((row) => {
              const prev = row[year - 1];
              const curr = row[year];
              const delta = prev > 0 ? (curr - prev) / prev : null;
              return (
                <tr key={row.month} className="hover:bg-gray-50">
                  <td className="px-4 py-2.5 font-medium text-gray-700">{row.month}</td>
                  <td className="px-4 py-2.5 text-right text-gray-500">{prev || "—"}</td>
                  <td className="px-4 py-2.5 text-right text-gray-700 font-medium">{curr || "—"}</td>
                  <td className="px-4 py-2.5 text-right">
                    {delta != null ? (
                      <span className={`text-xs font-medium ${
                        delta > 0 ? "text-emerald-600" :
                        delta < 0 ? "text-red-500" : "text-gray-400"
                      }`}>
                        {delta > 0 ? "+" : ""}{(delta * 100).toFixed(2)}%
                      </span>
                    ) : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-right text-gray-600">
                    {row.rev_this
                      ? new Intl.NumberFormat("en").format(Math.round(row.rev_this))
                      : "—"}
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
