/**
 * Countries — ranking table with Hot / Warm / Cold tiers
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import axios from "axios";
import CountryBadge from "../components/CountryBadge";
import SyncBadge from "../components/SyncBadge";

const TIER_ORDER = { Hot: 0, Warm: 1, Cold: 2 };

export default function Countries() {
  const currentYear = new Date().getFullYear();
  const [year,      setYear]      = useState(currentYear);
  const [month,     setMonth]     = useState(""); // "" = full year
  const [countries, setCountries] = useState([]);
  const [loading,   setLoading]   = useState(true);
  const [search,    setSearch]    = useState("");
  const [tierFilter, setTierFilter] = useState("All");

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams({ year });
    if (month) params.set("month", month);
    axios.get(`/api/countries/ranking?${params}`)
      .then((r) => setCountries(r.data.data || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [year, month]);

  const filtered = countries.filter((c) => {
    const matchSearch = !search ||
      (c.country || "").toLowerCase().includes(search.toLowerCase()) ||
      (c.country_code || "").toLowerCase().includes(search.toLowerCase());
    const matchTier = tierFilter === "All" || c.tier === tierFilter;
    return matchSearch && matchTier;
  });

  const hotCount  = countries.filter((c) => c.tier === "Hot").length;
  const warmCount = countries.filter((c) => c.tier === "Warm").length;
  const coldCount = countries.filter((c) => c.tier === "Cold").length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-800">Country Intelligence</h1>
          <p className="text-sm text-gray-500">
            Guest origin ranking by score
            <SyncBadge timestamp={countries[0]?.data_synced_at} />
          </p>
        </div>
        <div className="flex gap-2 items-center text-sm flex-wrap">
          <select value={year} onChange={(e) => setYear(Number(e.target.value))}
            className="border border-gray-200 rounded-lg px-3 py-1.5">
            {[currentYear - 1, currentYear, currentYear + 1].map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
          <select value={month} onChange={(e) => setMonth(e.target.value)}
            className="border border-gray-200 rounded-lg px-3 py-1.5">
            <option value="">Full Year</option>
            {["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].map((m, i) => (
              <option key={i+1} value={i+1}>{m}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Tier summary cards */}
      <div className="grid grid-cols-3 gap-4">
        {[
          { tier: "Hot",  count: hotCount,  color: "bg-red-50 border-red-200",    text: "text-red-700",    emoji: "🔥" },
          { tier: "Warm", count: warmCount, color: "bg-amber-50 border-amber-200", text: "text-amber-700", emoji: "☀️" },
          { tier: "Cold", count: coldCount, color: "bg-blue-50 border-blue-200",   text: "text-blue-700",  emoji: "❄️" },
        ].map(({ tier, count, color, text, emoji }) => (
          <button key={tier}
            onClick={() => setTierFilter(tierFilter === tier ? "All" : tier)}
            className={`rounded-xl border p-4 text-center shadow-sm transition-all ${color} ${
              tierFilter === tier ? "ring-2 ring-offset-1 ring-indigo-400" : ""
            }`}>
            <p className="text-2xl">{emoji}</p>
            <p className={`text-2xl font-bold mt-1 ${text}`}>{count}</p>
            <p className={`text-xs font-medium uppercase mt-0.5 ${text}`}>{tier} Markets</p>
          </button>
        ))}
      </div>

      {/* Search + filter bar */}
      <div className="flex gap-2 items-center">
        <input
          type="text"
          placeholder="Search country..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm flex-1 max-w-xs"
        />
        {tierFilter !== "All" && (
          <button onClick={() => setTierFilter("All")}
            className="text-xs text-indigo-600 hover:underline">
            Clear filter
          </button>
        )}
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-gray-400 animate-pulse">Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="bg-white rounded-xl border p-8 text-center text-gray-400">
          No countries found.
        </div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-gray-500 uppercase border-b border-gray-100">
                <th className="px-4 py-3 text-left w-12">#</th>
                <th className="px-4 py-3 text-left">Country</th>
                <th className="px-4 py-3 text-center">Tier</th>
                <th className="px-4 py-3 text-right">Score</th>
                <th className="px-4 py-3 text-right">Bookings</th>
                <th className="px-4 py-3 text-right">Revenue</th>
                <th className="px-4 py-3 text-right">YoY Growth</th>
                <th className="px-4 py-3 text-center">Details</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {filtered.map((c) => (
                <tr key={c.country_code || c.country} className="hover:bg-gray-50">
                  <td className="px-4 py-2.5 text-gray-400 font-mono text-xs">{c.rank}</td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className="text-base">{flagEmoji(c.country_code)}</span>
                      <div>
                        <p className="font-medium text-gray-800">{c.country || "Unknown"}</p>
                        <p className="text-xs text-gray-400">{c.country_code}</p>
                      </div>
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-center">
                    <CountryBadge tier={c.tier} />
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <div className="w-16 bg-gray-100 rounded-full h-1.5">
                        <div
                          className={`h-1.5 rounded-full ${
                            c.tier === "Hot" ? "bg-red-400" :
                            c.tier === "Warm" ? "bg-amber-400" : "bg-blue-300"
                          }`}
                          style={{ width: `${Math.min(c.score * 100, 100)}%` }}
                        />
                      </div>
                      <span className="font-mono text-xs text-gray-600 w-10 text-right">
                        {(c.score * 100).toFixed(0)}
                      </span>
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-right text-gray-700">{c.count?.toLocaleString()}</td>
                  <td className="px-4 py-2.5 text-right text-gray-700">
                    {new Intl.NumberFormat("en").format(Math.round(c.revenue_native || 0))}
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    {c.yoy_growth != null ? (
                      <span className={`font-medium ${
                        c.yoy_growth > 0 ? "text-emerald-600" :
                        c.yoy_growth < 0 ? "text-red-500" : "text-gray-500"
                      }`}>
                        {c.yoy_growth > 0 ? "+" : ""}{(c.yoy_growth * 100).toFixed(2)}%
                      </span>
                    ) : (
                      <span className="text-gray-300">—</span>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-center">
                    {c.country_code ? (
                      <Link
                        to={`/countries/${c.country_code}`}
                        className="text-indigo-600 hover:text-indigo-800 text-xs font-medium"
                      >
                        View →
                      </Link>
                    ) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Convert ISO 3166-1 alpha-2 code to flag emoji */
function flagEmoji(code) {
  if (!code || code.length !== 2) return "🌐";
  const cp = [...code.toUpperCase()].map((c) => 127397 + c.charCodeAt(0));
  return String.fromCodePoint(...cp);
}
