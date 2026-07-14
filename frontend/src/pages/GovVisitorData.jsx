import { useState, useEffect, useRef } from "react";
import { useAuth } from "../context/AuthContext";
import SyncBadge from "../components/SyncBadge";

const MONTHS = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];
const MONTH_LABELS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const IMPORT_YEARS = [2023, 2024, 2025, 2026];

const fmt = (n) => n == null ? "—" : new Intl.NumberFormat("en").format(n);

function pct(cur, base) {
  if (base == null || base === 0) return null;
  return ((cur - base) / base) * 100;
}

function DeltaBadge({ value, size = "xs" }) {
  if (value == null) return null;
  const sign = value > 0 ? "+" : "";
  const color = value > 0 ? "text-emerald-600" : "text-red-500";
  const arrow = value > 0 ? "▲" : "▼";
  const cls = size === "xs" ? "text-[10px]" : "text-xs";
  return (
    <div className={`${cls} ${color} leading-none mt-0.5 whitespace-nowrap`}>
      {arrow} {sign}{value.toFixed(1)}%
    </div>
  );
}

function api(path, opts = {}) {
  const token = localStorage.getItem("token");
  return fetch(path, {
    ...opts,
    headers: { ...opts.headers, ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  }).then((r) => r.json());
}

export default function GovVisitorData() {
  const { isAdmin } = useAuth();
  const fileRef = useRef(null);

  const [allData, setAllData] = useState([]);
  const [destinations, setDestinations] = useState([]);
  const [years, setYears] = useState([]);
  const [selectedDest, setSelectedDest] = useState("");
  const [selectedYear, setSelectedYear] = useState(null);
  const [compareYear, setCompareYear] = useState(null);
  const [viewMode, setViewMode] = useState("raw");
  const [importYear, setImportYear] = useState(2025);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState(null);

  const loadAll = (keepYear = false) => {
    setLoading(true);
    Promise.all([
      api("/api/gov-visitor/destinations").then(j => j.success ? j.data : []),
      api("/api/gov-visitor/years").then(j => j.success ? j.data : []),
      api("/api/gov-visitor").then(j => j.success ? j.data : []),
    ]).then(([dests, yrs, data]) => {
      setDestinations(dests);
      setYears(yrs);
      setAllData(data);
      if (!keepYear && yrs.length > 0) {
        setSelectedYear(yrs[0]);
        setCompareYear(yrs[1] ?? null);
      }
    }).finally(() => setLoading(false));
  };

  useEffect(() => { loadAll(); }, []);

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setUploading(true);
    setMsg(null);
    const fd = new FormData();
    fd.append("file", file);
    fd.append("year", String(importYear));
    try {
      const j = await api("/api/gov-visitor/import", { method: "POST", body: fd });
      if (j.success) {
        setMsg({ type: "ok", text: `Imported ${j.data.imported_rows} rows (${j.data.year}) · ${j.data.destinations.join(", ")}` });
        loadAll(true);
      } else {
        setMsg({ type: "err", text: j.error || "Import failed" });
      }
    } catch {
      setMsg({ type: "err", text: "Network error" });
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const handleDeleteDest = async (dest, year) => {
    const label = year ? `"${dest}" (${year})` : `"${dest}" (all years)`;
    if (!confirm(`Delete all data for ${label}?`)) return;
    const params = `destination=${encodeURIComponent(dest)}${year ? `&year=${year}` : ""}`;
    const j = await api(`/api/gov-visitor?${params}`, { method: "DELETE" });
    if (j.success) {
      setMsg({ type: "ok", text: `Deleted ${j.data.deleted_count} rows for ${label}` });
      loadAll(true);
    }
  };

  // Group all rows by (destination, year)
  const grouped = {};
  allData.forEach(r => {
    const key = `${r.destination}|||${r.data_year ?? "null"}`;
    if (!grouped[key]) grouped[key] = { dest: r.destination, year: r.data_year, rows: [] };
    grouped[key].rows.push(r);
  });

  // Visible groups for raw/mom
  const rawGroups = Object.values(grouped)
    .filter(g =>
      (!selectedDest || g.dest === selectedDest) &&
      (!selectedYear || g.year === selectedYear || g.year == null)
    )
    .sort((a, b) => a.dest.localeCompare(b.dest) || (b.year ?? 0) - (a.year ?? 0));

  // YoY: merge by destination
  const yoyGroups = {};
  allData.forEach(r => {
    if (!yoyGroups[r.destination]) yoyGroups[r.destination] = { curRows: [], priorRows: [] };
    if (r.data_year === selectedYear) yoyGroups[r.destination].curRows.push(r);
    if (r.data_year === compareYear) yoyGroups[r.destination].priorRows.push(r);
  });

  if (!isAdmin) {
    return <div className="text-center py-16 text-gray-400">Admin access required.</div>;
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-bold text-gray-900">Government Visitor Data</h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Import & manage government tourism statistics by country
          <SyncBadge timestamp={allData[0]?.data_synced_at} />
        </p>
      </div>

      {/* Import */}
      <div className="bg-white rounded-lg border border-gray-200 p-5">
        <label className="block text-sm font-medium text-gray-700 mb-1">Import Excel File</label>
        <p className="text-xs text-gray-400 mb-3">
          Upload an .xlsx file with sheets named by destination (e.g. Taiwan, Japan, Vietnam).
          Each sheet: columns = #, Country, Jan–Dec, Sum. Existing data for the matching destination + year will be replaced.
        </p>
        <div className="flex items-center gap-3 flex-wrap">
          <input
            ref={fileRef}
            type="file"
            accept=".xlsx,.xls"
            className="block text-sm text-gray-500 file:mr-3 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:font-medium file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100"
          />
          <select
            value={importYear}
            onChange={e => setImportYear(Number(e.target.value))}
            className="border border-gray-300 rounded px-3 py-2 text-sm font-medium"
          >
            {IMPORT_YEARS.map(y => <option key={y} value={y}>{y}</option>)}
          </select>
          <button
            onClick={handleUpload}
            disabled={uploading}
            className="px-4 py-2 bg-indigo-600 text-white text-sm font-medium rounded hover:bg-indigo-700 disabled:opacity-50 whitespace-nowrap"
          >
            {uploading ? "Importing..." : "Import"}
          </button>
          <a
            href="/api/gov-visitor/template"
            download
            className="px-4 py-2 bg-white border border-gray-300 text-gray-700 text-sm font-medium rounded hover:bg-gray-50 flex items-center gap-1.5 whitespace-nowrap"
          >
            <span className="text-base leading-none">&#8615;</span> Template
          </a>
        </div>
        {msg && (
          <div className={`mt-3 text-sm px-3 py-2 rounded ${msg.type === "ok" ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600"}`}>
            {msg.text}
          </div>
        )}
      </div>

      {/* Filters + view mode */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-600">Destination:</label>
          <select value={selectedDest} onChange={e => setSelectedDest(e.target.value)}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm">
            <option value="">All</option>
            {destinations.map(d => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>

        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-600">Year:</label>
          <select value={selectedYear ?? ""} onChange={e => setSelectedYear(Number(e.target.value) || null)}
            className="border border-gray-300 rounded px-3 py-1.5 text-sm">
            <option value="">All</option>
            {years.map(y => <option key={y} value={y}>{y}</option>)}
          </select>
        </div>

        {viewMode === "yoy" && (
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600">vs:</label>
            <select value={compareYear ?? ""} onChange={e => setCompareYear(Number(e.target.value) || null)}
              className="border border-gray-300 rounded px-3 py-1.5 text-sm">
              <option value="">—</option>
              {years.filter(y => y !== selectedYear).map(y => <option key={y} value={y}>{y}</option>)}
            </select>
          </div>
        )}

        <div className="ml-auto flex items-center gap-1">
          {[
            { key: "raw", label: "Data" },
            { key: "yoy", label: "YoY" },
            { key: "mom", label: "MoM" },
          ].map(({ key, label }) => (
            <button key={key} onClick={() => setViewMode(key)}
              className={`px-3 py-1.5 text-xs font-semibold rounded transition-colors ${
                viewMode === key
                  ? "bg-indigo-600 text-white"
                  : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}>
              {label}
            </button>
          ))}
        </div>
      </div>

      {loading && <div className="flex items-center justify-center h-40 text-gray-400 text-sm">Loading...</div>}

      {!loading && allData.length === 0 && (
        <div className="text-center py-16 text-gray-400">
          No government visitor data yet. Import an Excel file to get started.
        </div>
      )}

      {/* RAW / MOM view */}
      {!loading && viewMode !== "yoy" && rawGroups.map(({ dest, year, rows }) => (
        <RawTable
          key={`${dest}-${year}`}
          dest={dest}
          year={year}
          rows={rows}
          viewMode={viewMode}
          onDelete={() => handleDeleteDest(dest, year)}
        />
      ))}

      {/* YoY view */}
      {!loading && viewMode === "yoy" && Object.entries(yoyGroups)
        .filter(([dest]) => !selectedDest || dest === selectedDest)
        .filter(([, { curRows, priorRows }]) => curRows.length > 0 || priorRows.length > 0)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([dest, { curRows, priorRows }]) => (
          <YoyTable
            key={dest}
            dest={dest}
            curYear={selectedYear}
            priorYear={compareYear}
            curRows={curRows}
            priorRows={priorRows}
            onDelete={(y) => handleDeleteDest(dest, y)}
          />
        ))
      }
    </div>
  );
}

function RawTable({ dest, year, rows, viewMode, onDelete }) {
  const isMom = viewMode === "mom";

  return (
    <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
      <div className="flex items-center justify-between px-5 py-3 bg-gray-50 border-b">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-gray-800">{dest}</span>
          {year != null
            ? <span className="text-xs bg-indigo-100 text-indigo-700 px-2 py-0.5 rounded-full font-semibold">{year}</span>
            : <span className="text-xs bg-gray-200 text-gray-500 px-2 py-0.5 rounded-full">year unknown</span>
          }
          <span className="text-xs text-gray-400">{rows.length} countries</span>
          {isMom && <span className="text-xs bg-amber-50 text-amber-600 px-2 py-0.5 rounded-full font-medium">MoM trend</span>}
        </div>
        <button onClick={onDelete} className="text-xs text-red-500 hover:text-red-700 font-medium">Delete</button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="text-xs text-gray-500 bg-gray-50 border-b">
              <th className="px-3 py-2 text-left w-8">#</th>
              <th className="px-3 py-2 text-left">Source Country</th>
              {MONTH_LABELS.map(m => <th key={m} className="px-3 py-2 text-right">{m}</th>)}
              <th className="px-3 py-2 text-right font-bold">Total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id} className="border-b hover:bg-gray-50">
                <td className="px-3 py-2 text-xs text-gray-400">{r.rank}</td>
                <td className="px-3 py-2 text-sm font-medium text-gray-800">{r.source_country}</td>
                {MONTHS.map((m, i) => {
                  const delta = isMom && i > 0 ? pct(r[m] || 0, r[MONTHS[i - 1]] || 0) : null;
                  return (
                    <td key={m} className="px-3 py-2 text-right">
                      <div className="text-xs font-mono text-gray-600">{fmt(r[m])}</div>
                      {isMom && delta != null && <DeltaBadge value={delta} />}
                    </td>
                  );
                })}
                <td className="px-3 py-2 text-xs text-right font-mono font-bold text-gray-800">{fmt(r.total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function YoyTable({ dest, curYear, priorYear, curRows, priorRows, onDelete }) {
  const priorMap = {};
  priorRows.forEach(r => { priorMap[r.source_country] = r; });

  // All countries: cur first, then prior-only
  const allCountries = [
    ...curRows,
    ...priorRows.filter(r => !curRows.find(c => c.source_country === r.source_country)).map(r => ({ ...r, _priorOnly: true })),
  ];

  return (
    <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
      <div className="flex items-center justify-between px-5 py-3 bg-gray-50 border-b">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-semibold text-gray-800">{dest}</span>
          <span className="text-xs bg-indigo-100 text-indigo-700 px-2 py-0.5 rounded-full font-semibold">{curYear ?? "?"}</span>
          <span className="text-xs text-gray-400">vs</span>
          <span className="text-xs bg-gray-200 text-gray-600 px-2 py-0.5 rounded-full font-semibold">{priorYear ?? "?"}</span>
          <span className="text-xs text-gray-400">{allCountries.length} countries</span>
        </div>
        <div className="flex gap-3">
          {curYear && <button onClick={() => onDelete(curYear)} className="text-xs text-red-400 hover:text-red-600">Delete {curYear}</button>}
          {priorYear && <button onClick={() => onDelete(priorYear)} className="text-xs text-red-400 hover:text-red-600">Delete {priorYear}</button>}
        </div>
      </div>

      {/* Legend */}
      <div className="px-5 py-2 border-b bg-white flex items-center gap-4 text-[11px] text-gray-500">
        <span><span className="font-mono text-gray-700">1,234</span> = {curYear ?? "current"}</span>
        <span><span className="font-mono text-gray-400">1,234</span> = {priorYear ?? "prior"}</span>
        <span><span className="text-emerald-600">▲ +x%</span> / <span className="text-red-500">▼ -x%</span> = YoY change</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="text-xs text-gray-500 bg-gray-50 border-b">
              <th className="px-3 py-2 text-left sticky left-0 bg-gray-50">Source Country</th>
              {MONTH_LABELS.map(m => <th key={m} className="px-3 py-2 text-right min-w-[72px]">{m}</th>)}
              <th className="px-3 py-2 text-right font-bold min-w-[80px]">Total</th>
            </tr>
          </thead>
          <tbody>
            {allCountries.map(r => {
              const prior = priorMap[r.source_country];
              const isPriorOnly = r._priorOnly;
              return (
                <tr key={r.id} className={`border-b hover:bg-gray-50 ${isPriorOnly ? "opacity-60" : ""}`}>
                  <td className="px-3 py-2 text-sm font-medium text-gray-800 sticky left-0 bg-white">
                    {r.source_country}
                    {isPriorOnly && <span className="ml-1 text-[10px] text-gray-400">({priorYear} only)</span>}
                  </td>
                  {MONTHS.map(m => {
                    const curVal = isPriorOnly ? null : (r[m] ?? 0);
                    const priorVal = prior ? (prior[m] ?? 0) : null;
                    const delta = curVal != null && priorVal != null ? pct(curVal, priorVal) : null;
                    return (
                      <td key={m} className="px-3 py-2 text-right">
                        {curVal != null && <div className="text-xs font-mono text-gray-700">{fmt(curVal)}</div>}
                        {priorVal != null && <div className="text-[11px] font-mono text-gray-400">{fmt(priorVal)}</div>}
                        <DeltaBadge value={delta} />
                      </td>
                    );
                  })}
                  <td className="px-3 py-2 text-right">
                    {!isPriorOnly && <div className="text-xs font-mono font-bold text-gray-800">{fmt(r.total)}</div>}
                    {prior && <div className="text-[11px] font-mono text-gray-400">{fmt(prior.total)}</div>}
                    <DeltaBadge value={!isPriorOnly && prior ? pct(r.total, prior.total) : null} />
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
