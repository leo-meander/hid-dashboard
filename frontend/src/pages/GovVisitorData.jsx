import { useState, useEffect, useRef } from "react";
import { useAuth } from "../context/AuthContext";
import SyncBadge from "../components/SyncBadge";

const MONTHS = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];
const MONTH_LABELS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

const fmt = (n) => n == null ? "—" : new Intl.NumberFormat("en").format(n);

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

  const [data, setData] = useState([]);
  const [destinations, setDestinations] = useState([]);
  const [selectedDest, setSelectedDest] = useState("");
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [msg, setMsg] = useState(null);

  const loadDestinations = () => {
    api("/api/gov-visitor/destinations").then((j) => {
      if (j.success) setDestinations(j.data);
    });
  };

  const loadData = (dest) => {
    setLoading(true);
    const params = dest ? `?destination=${encodeURIComponent(dest)}` : "";
    api(`/api/gov-visitor${params}`).then((j) => {
      if (j.success) setData(j.data);
    }).finally(() => setLoading(false));
  };

  useEffect(() => {
    loadDestinations();
    loadData("");
  }, []);

  useEffect(() => {
    loadData(selectedDest);
  }, [selectedDest]);

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    setUploading(true);
    setMsg(null);
    const fd = new FormData();
    fd.append("file", file);
    try {
      const j = await api("/api/gov-visitor/import", { method: "POST", body: fd });
      if (j.success) {
        setMsg({ type: "ok", text: `Imported ${j.data.imported_rows} rows from ${j.data.destinations.join(", ")}` });
        loadDestinations();
        loadData(selectedDest);
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

  const handleDeleteDest = async (dest) => {
    if (!confirm(`Delete all data for "${dest}"?`)) return;
    const j = await api(`/api/gov-visitor?destination=${encodeURIComponent(dest)}`, { method: "DELETE" });
    if (j.success) {
      setMsg({ type: "ok", text: `Deleted ${j.data.deleted_count} rows for ${dest}` });
      loadDestinations();
      loadData(selectedDest);
    }
  };

  // Group data by destination
  const grouped = {};
  data.forEach((r) => {
    if (!grouped[r.destination]) grouped[r.destination] = [];
    grouped[r.destination].push(r);
  });

  if (!isAdmin) {
    return (
      <div className="text-center py-16 text-gray-400">
        Admin access required.
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900">Government Visitor Data</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Import & manage government tourism statistics by country
            <SyncBadge timestamp={data[0]?.data_synced_at} />
          </p>
        </div>
      </div>

      {/* Upload section */}
      <div className="bg-white rounded-lg border border-gray-200 p-5">
        <div className="flex items-center gap-4">
          <div className="flex-1">
            <label className="block text-sm font-medium text-gray-700 mb-1">Import Excel File</label>
            <p className="text-xs text-gray-400 mb-2">
              Upload an .xlsx file with sheets named by destination (e.g. Taiwan, Japan, Vietnam).
              Each sheet: columns = #, Country, Jan–Dec, Sum. Existing data for matching destinations will be replaced.
            </p>
            <div className="flex items-center gap-3">
              <input
                ref={fileRef}
                type="file"
                accept=".xlsx,.xls"
                className="block w-full text-sm text-gray-500 file:mr-3 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:font-medium file:bg-indigo-50 file:text-indigo-700 hover:file:bg-indigo-100"
              />
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
                className="px-4 py-2 bg-white border border-gray-300 text-gray-700 text-sm font-medium rounded hover:bg-gray-50 whitespace-nowrap flex items-center gap-1.5"
              >
                <span className="text-base leading-none">&#8615;</span> Template
              </a>
            </div>
          </div>
        </div>
        {msg && (
          <div className={`mt-3 text-sm px-3 py-2 rounded ${msg.type === "ok" ? "bg-green-50 text-green-700" : "bg-red-50 text-red-600"}`}>
            {msg.text}
          </div>
        )}
      </div>

      {/* Filter by destination */}
      <div className="flex items-center gap-3">
        <label className="text-sm font-medium text-gray-700">Filter:</label>
        <select
          value={selectedDest}
          onChange={(e) => setSelectedDest(e.target.value)}
          className="border border-gray-300 rounded px-3 py-1.5 text-sm"
        >
          <option value="">All Destinations</option>
          {destinations.map((d) => (
            <option key={d} value={d}>{d}</option>
          ))}
        </select>
      </div>

      {loading && (
        <div className="flex items-center justify-center h-40 text-gray-400 text-sm">Loading...</div>
      )}

      {!loading && data.length === 0 && (
        <div className="text-center py-16 text-gray-400">
          No government visitor data yet. Import an Excel file to get started.
        </div>
      )}

      {!loading && Object.entries(grouped).map(([dest, rows]) => (
        <div key={dest} className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <div className="flex items-center justify-between px-5 py-3 bg-gray-50 border-b">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-gray-800">{dest}</span>
              <span className="text-xs text-gray-400">{rows.length} countries</span>
            </div>
            <button
              onClick={() => handleDeleteDest(dest)}
              className="text-xs text-red-500 hover:text-red-700 font-medium"
            >
              Delete All
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="text-xs text-gray-500 bg-gray-50 border-b">
                  <th className="px-3 py-2 text-left w-8">#</th>
                  <th className="px-3 py-2 text-left">Source Country</th>
                  {MONTH_LABELS.map((m) => (
                    <th key={m} className="px-3 py-2 text-right">{m}</th>
                  ))}
                  <th className="px-3 py-2 text-right font-bold">Total</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b hover:bg-gray-50">
                    <td className="px-3 py-2 text-xs text-gray-400">{r.rank}</td>
                    <td className="px-3 py-2 text-sm font-medium text-gray-800">{r.source_country}</td>
                    {MONTHS.map((m) => (
                      <td key={m} className="px-3 py-2 text-xs text-right font-mono text-gray-600">
                        {fmt(r[m])}
                      </td>
                    ))}
                    <td className="px-3 py-2 text-xs text-right font-mono font-bold text-gray-800">
                      {fmt(r.total)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  );
}
