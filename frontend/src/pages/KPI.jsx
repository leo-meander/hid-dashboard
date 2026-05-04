/**
 * KPI Page — editable predicted OCC% (room/dorm split) + current & next month forecasts
 */
import { useEffect, useState } from "react";
import axios from "axios";
import KPICard from "../components/KPICard";
import SyncBadge from "../components/SyncBadge";
import { useBranch } from "../context/BranchContext";

const MONTHS = [
  "Jan","Feb","Mar","Apr","May","Jun",
  "Jul","Aug","Sep","Oct","Nov","Dec",
];

const fmt = (v) => v != null ? new Intl.NumberFormat("en").format(Math.round(v)) : "—";

export default function KPI() {
  const { selected, isAll } = useBranch();
  const now = new Date();
  const [year,  setYear]  = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1);
  const [data,  setData]  = useState([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState({});

  const fetchData = () => {
    setLoading(true);
    const bParam = !isAll && selected ? `&branch_id=${selected}` : "";
    axios.get(`/api/kpi/summary?year=${year}&month=${month}${bParam}`)
      .then((r) => setData(r.data.data || []))
      .catch(console.error)
      .finally(() => setLoading(false));
  };

  useEffect(() => { fetchData(); }, [year, month, selected, isAll]);

  const handleOccChange = (branchId, field, val) => {
    setEditing((prev) => ({ ...prev, [branchId]: { ...prev[branchId], [field]: val } }));
  };

  const saveOcc = async (branchId, targetYear, targetMonth, patchFields) => {
    const res = await axios.get(`/api/kpi/targets?branch_id=${branchId}&year=${targetYear}`);
    const targets = res.data.data || [];
    const target = targets.find((t) => t.month === targetMonth);
    if (!target) return;
    await axios.patch(`/api/kpi/targets/${target.id}`, patchFields);
    // Clear editing state for this branch
    setEditing((prev) => {
      const n = { ...prev };
      delete n[branchId];
      return n;
    });
    fetchData();
  };

  const hasSplit = (b) => b.total_room_count > 0 && b.total_dorm_count > 0;

  const saveCurOcc = (b) => {
    const e = editing[b.branch_id] || {};
    const patch = {};
    if (hasSplit(b)) {
      if (e.cur_room !== undefined) patch.predicted_room_occ_pct = parseFloat(e.cur_room) / 100;
      if (e.cur_dorm !== undefined) patch.predicted_dorm_occ_pct = parseFloat(e.cur_dorm) / 100;
    } else {
      if (e.cur !== undefined) patch.predicted_occ_pct = parseFloat(e.cur) / 100;
    }
    if (Object.keys(patch).length) saveOcc(b.branch_id, year, month, patch);
  };

  const saveNextOcc = (b) => {
    const e = editing[b.branch_id] || {};
    const patch = {};
    if (hasSplit(b)) {
      if (e.next_room !== undefined) patch.predicted_room_occ_pct = parseFloat(e.next_room) / 100;
      if (e.next_dorm !== undefined) patch.predicted_dorm_occ_pct = parseFloat(e.next_dorm) / 100;
    } else {
      if (e.next !== undefined) patch.predicted_occ_pct = parseFloat(e.next) / 100;
    }
    if (Object.keys(patch).length) saveOcc(b.branch_id, b.next_year, b.next_month, patch);
  };

  const hasEditing = (branchId, ...fields) =>
    fields.some((f) => editing[branchId]?.[f] !== undefined);

  return (
    <div className="space-y-6">
      {/* Header + selectors */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-800">KPI Dashboard</h1>
          <p className="text-sm text-gray-500">
            Revenue achievement + forecasts
            <SyncBadge timestamp={data[0]?.data_synced_at} />
          </p>
        </div>
        <div className="flex gap-2">
          <select
            className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm"
            value={month}
            onChange={(e) => setMonth(+e.target.value)}
          >
            {MONTHS.map((m, i) => (
              <option key={i} value={i + 1}>{m}</option>
            ))}
          </select>
          <select
            className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm"
            value={year}
            onChange={(e) => setYear(+e.target.value)}
          >
            {[now.getFullYear() - 1, now.getFullYear(), now.getFullYear() + 1].map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
        </div>
      </div>

      {loading ? (
        <div className="text-center py-12">
          <div className="text-gray-400 animate-pulse text-lg">Loading\u2026</div>
          <p className="text-xs text-gray-300 mt-2">Please wait ~30s on first load (data is cached for 3 hours)</p>
        </div>
      ) : data.length === 0 ? (
        <div className="bg-white rounded-xl border p-8 text-center text-gray-400">
          No data. Add branches and set KPI targets first.
        </div>
      ) : (
        <div className="space-y-4">
          {data.map((b) => {
            const split = hasSplit(b);
            return (
            <div key={b.branch_id} className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
              <div className="flex items-center justify-between mb-4">
                <div>
                  <h2 className="text-base font-semibold text-gray-800">{b.branch_name}</h2>
                  <p className="text-xs text-gray-400">{b.branch_city} · {b.currency}</p>
                </div>
                {b.achievement_pct !== null && (
                  <span className={`text-sm font-bold px-3 py-1 rounded-full ${
                    b.achievement_pct >= 1   ? "bg-green-100 text-green-700"
                    : b.achievement_pct >= 0.8 ? "bg-yellow-100 text-yellow-700"
                    : "bg-red-100 text-red-700"
                  }`}>
                    {(b.achievement_pct * 100).toFixed(2)}% of target
                  </span>
                )}
              </div>

              {/* Revenue row */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                <div>
                  <p className="text-xs text-gray-400 uppercase tracking-wide">Actual Revenue</p>
                  <p className="font-semibold text-gray-800 mt-0.5">{fmt(b.actual_revenue_native)}</p>
                </div>
                <div>
                  <p className="text-xs text-gray-400 uppercase tracking-wide">Target</p>
                  <p className="font-semibold text-gray-800 mt-0.5">{fmt(b.target_revenue_native)}</p>
                </div>

                {/* This Month Forecast */}
                <div>
                  <p className="text-xs text-gray-400 uppercase tracking-wide">This Month Forecast</p>
                  <p className="font-semibold text-indigo-600 mt-0.5">{fmt(b.occ_forecast_native)}</p>
                  {split && b.room_forecast_native != null ? (
                    <div className="text-xs text-gray-400 mt-1 space-y-0.5">
                      <p>Room: {fmt(b.room_forecast_native)} <span className="text-gray-300">·</span> ADR {fmt(b.forecast_room_adr ?? b.room_adr_native)}</p>
                      <p>Dorm: {fmt(b.dorm_forecast_native)} <span className="text-gray-300">·</span> ADR {fmt(b.forecast_dorm_adr ?? b.dorm_adr_native)}</p>
                    </div>
                  ) : b.avg_adr_native ? (
                    <p className="text-xs text-gray-400 mt-0.5">
                      ADR {fmt(b.avg_adr_native)} · {b.nights_booked} nights
                    </p>
                  ) : null}
                </div>

                {/* Next Month Forecast */}
                <div>
                  <p className="text-xs text-gray-400 uppercase tracking-wide">Next Month Forecast</p>
                  <p className="font-semibold text-emerald-600 mt-0.5">{fmt(b.next_month_forecast_native)}</p>
                  {split && b.next_month_room_forecast != null ? (
                    <div className="text-xs text-gray-400 mt-1 space-y-0.5">
                      <p>Room: {fmt(b.next_month_room_forecast)} <span className="text-gray-300">·</span> ADR {fmt(b.next_month_forecast_room_adr ?? b.next_month_room_adr)}</p>
                      <p>Dorm: {fmt(b.next_month_dorm_forecast)} <span className="text-gray-300">·</span> ADR {fmt(b.next_month_forecast_dorm_adr ?? b.next_month_dorm_adr)}</p>
                    </div>
                  ) : b.next_month_adr ? (
                    <p className="text-xs text-gray-400 mt-0.5">
                      ADR {fmt(b.next_month_adr)} · {b.next_month_booked_nights} nights
                    </p>
                  ) : null}
                </div>
              </div>

              {/* OCC editors */}
              <div className="mt-4 pt-4 border-t border-gray-100">
                {split ? (
                  /* Room/Dorm split OCC inputs */
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    {/* Current month — Room + Dorm */}
                    <div className="space-y-2">
                      <p className="text-xs font-medium text-gray-500 uppercase">This Month OCC%</p>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-gray-400 w-12">Room:</span>
                        <input
                          type="number" min="0" max="100" step="0.1"
                          className="border border-gray-200 rounded-lg px-2 py-1 text-sm w-20 text-center"
                          defaultValue={b.predicted_room_occ_pct != null ? (b.predicted_room_occ_pct * 100).toFixed(2) : ""}
                          placeholder="—"
                          onChange={(e) => handleOccChange(b.branch_id, "cur_room", e.target.value)}
                        />
                        <span className="text-xs text-gray-400 w-12">Dorm:</span>
                        <input
                          type="number" min="0" max="100" step="0.1"
                          className="border border-gray-200 rounded-lg px-2 py-1 text-sm w-20 text-center"
                          defaultValue={b.predicted_dorm_occ_pct != null ? (b.predicted_dorm_occ_pct * 100).toFixed(2) : ""}
                          placeholder="—"
                          onChange={(e) => handleOccChange(b.branch_id, "cur_dorm", e.target.value)}
                        />
                        {hasEditing(b.branch_id, "cur_room", "cur_dorm") && (
                          <button
                            className="text-xs bg-indigo-600 text-white px-3 py-1 rounded-lg hover:bg-indigo-700"
                            onClick={() => saveCurOcc(b)}
                          >Save</button>
                        )}
                        <p className="text-xs text-gray-400">({b.days_elapsed}/{b.total_days}d)</p>
                      </div>
                    </div>

                    {/* Next month — Room + Dorm */}
                    <div className="space-y-2">
                      <p className="text-xs font-medium text-gray-500 uppercase">Next Month OCC%</p>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-gray-400 w-12">Room:</span>
                        <input
                          type="number" min="0" max="100" step="0.1"
                          className="border border-gray-200 rounded-lg px-2 py-1 text-sm w-20 text-center"
                          defaultValue={b.predicted_room_occ_next != null ? (b.predicted_room_occ_next * 100).toFixed(2) : ""}
                          placeholder="—"
                          onChange={(e) => handleOccChange(b.branch_id, "next_room", e.target.value)}
                        />
                        <span className="text-xs text-gray-400 w-12">Dorm:</span>
                        <input
                          type="number" min="0" max="100" step="0.1"
                          className="border border-gray-200 rounded-lg px-2 py-1 text-sm w-20 text-center"
                          defaultValue={b.predicted_dorm_occ_next != null ? (b.predicted_dorm_occ_next * 100).toFixed(2) : ""}
                          placeholder="—"
                          onChange={(e) => handleOccChange(b.branch_id, "next_dorm", e.target.value)}
                        />
                        {hasEditing(b.branch_id, "next_room", "next_dorm") && (
                          <button
                            className="text-xs bg-emerald-600 text-white px-3 py-1 rounded-lg hover:bg-emerald-700"
                            onClick={() => saveNextOcc(b)}
                          >Save</button>
                        )}
                      </div>
                    </div>
                  </div>
                ) : (
                  /* Single OCC input (no room/dorm split) */
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div className="flex items-center gap-3">
                      <p className="text-xs text-gray-500 whitespace-nowrap">This month OCC%:</p>
                      <input
                        type="number" min="0" max="100" step="0.1"
                        className="border border-gray-200 rounded-lg px-2 py-1 text-sm w-20 text-center"
                        defaultValue={b.predicted_occ_pct != null ? (b.predicted_occ_pct * 100).toFixed(2) : ""}
                        placeholder="—"
                        onChange={(e) => handleOccChange(b.branch_id, "cur", e.target.value)}
                      />
                      {hasEditing(b.branch_id, "cur") && (
                        <button
                          className="text-xs bg-indigo-600 text-white px-3 py-1 rounded-lg hover:bg-indigo-700"
                          onClick={() => saveCurOcc(b)}
                        >Save</button>
                      )}
                      <p className="text-xs text-gray-400">({b.days_elapsed}/{b.total_days}d)</p>
                    </div>
                    <div className="flex items-center gap-3">
                      <p className="text-xs text-gray-500 whitespace-nowrap">Next month OCC%:</p>
                      <input
                        type="number" min="0" max="100" step="0.1"
                        className="border border-gray-200 rounded-lg px-2 py-1 text-sm w-20 text-center"
                        defaultValue={b.predicted_occ_next != null ? (b.predicted_occ_next * 100).toFixed(2) : ""}
                        placeholder="—"
                        onChange={(e) => handleOccChange(b.branch_id, "next", e.target.value)}
                      />
                      {hasEditing(b.branch_id, "next") && (
                        <button
                          className="text-xs bg-emerald-600 text-white px-3 py-1 rounded-lg hover:bg-emerald-700"
                          onClick={() => saveNextOcc(b)}
                        >Save</button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          );
          })}
        </div>
      )}
    </div>
  );
}
