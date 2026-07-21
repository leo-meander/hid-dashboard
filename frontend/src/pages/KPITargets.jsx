/**
 * KPI Targets — set monthly revenue targets + predicted OCC% per branch
 * Table: rows = branches, columns = months (Jan–Dec)
 */
import { useState } from "react";
import { useQuery, useQueryClient, keepPreviousData } from "@tanstack/react-query";
import axios from "axios";

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

const CURRENCY_SYMBOL = { VND: "₫", TWD: "NT$", JPY: "¥", USD: "$" };

// Format revenue for display in input placeholder
function fmtRevenue(val, currency) {
  if (!val) return "";
  const n = Number(val);
  if (currency === "VND") return (n / 1_000_000).toFixed(0);       // show in millions
  return n.toLocaleString("en-US");
}

// Parse input back to raw number
function parseRevenue(str, currency) {
  const clean = str.replace(/,/g, "").trim();
  if (!clean) return null;
  const n = parseFloat(clean);
  if (isNaN(n)) return null;
  if (currency === "VND") return n * 1_000_000;
  return n;
}

function cellKey(branchId, month) {
  return `${branchId}_${month}`;
}

export default function KPITargets() {
  const now = new Date();
  const [year, setYear] = useState(now.getFullYear());
  const [edits, setEdits] = useState({});            // { "branchId_month": { rev, occ } }
  const [saving, setSaving] = useState({});          // { branchId: true }
  const [toast, setToast] = useState(null);

  const queryClient = useQueryClient();

  const showToast = (msg, type = "success") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  const { data: pageData, isPending } = useQuery({
    queryKey: ["kpi-targets", year],
    queryFn: async () => {
      const [brRes, tgRes] = await Promise.all([
        axios.get("/api/branches"),
        axios.get(`/api/kpi/targets?year=${year}`),
      ]);
      const branchList = brRes.data.data || brRes.data || [];
      const targetList = tgRes.data.data || [];

      // Build map: "branchId_month" → target object
      const map = {};
      for (const t of targetList) {
        map[cellKey(t.branch_id, t.month)] = t;
      }

      return {
        branches: branchList.filter((b) => b.is_active !== false),
        targetsMap: map,
      };
    },
    placeholderData: keepPreviousData,
  });

  const branches = pageData?.branches ?? [];
  const targetsMap = pageData?.targetsMap ?? {};

  const handleRevChange = (branchId, month, currency, rawInput) => {
    const key = cellKey(branchId, month);
    setEdits((prev) => ({
      ...prev,
      [key]: { ...prev[key], revInput: rawInput },
    }));
  };

  const handleOccChange = (branchId, month, rawInput) => {
    const key = cellKey(branchId, month);
    setEdits((prev) => ({
      ...prev,
      [key]: { ...prev[key], occInput: rawInput },
    }));
  };

  const saveRow = async (branch) => {
    setSaving((prev) => ({ ...prev, [branch.id]: true }));
    const currency = branch.currency || branch.native_currency || "VND";

    const calls = [];
    for (let m = 1; m <= 12; m++) {
      const key = cellKey(branch.id, m);
      const edit = edits[key];
      if (!edit) continue;

      const existing = targetsMap[key];
      const revNative =
        edit.revInput !== undefined
          ? parseRevenue(edit.revInput, currency)
          : existing?.target_revenue_native ?? null;

      const occPct =
        edit.occInput !== undefined
          ? (edit.occInput === "" ? null : parseFloat(edit.occInput) / 100)
          : existing?.predicted_occ_pct ?? null;

      if (revNative === null && occPct === null) continue;

      calls.push(
        axios.put("/api/kpi/targets/upsert", {
          branch_id: branch.id,
          year,
          month: m,
          target_revenue_native: revNative ?? existing?.target_revenue_native ?? 0,
          predicted_occ_pct: occPct,
        })
      );
    }

    try {
      await Promise.all(calls);
      showToast(`Saved ${branch.name}`);
      setEdits({});
      queryClient.invalidateQueries({ queryKey: ["kpi-targets", year] });
    } catch (e) {
      showToast("Save failed: " + (e.response?.data?.detail || e.message), "error");
    } finally {
      setSaving((prev) => ({ ...prev, [branch.id]: false }));
    }
  };

  const saveAll = async () => {
    for (const b of branches) {
      await saveRow(b);
    }
  };

  const hasRowEdits = (branchId) =>
    Object.keys(edits).some((k) => k.startsWith(`${branchId}_`));

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-800">KPI Targets</h1>
          <p className="text-sm text-gray-500">Set monthly revenue targets and predicted occupancy per branch</p>
        </div>
        <div className="flex items-center gap-3">
          <select
            className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm bg-white"
            value={year}
            onChange={(e) => setYear(+e.target.value)}
          >
            {[now.getFullYear() - 1, now.getFullYear(), now.getFullYear() + 1].map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
          <button
            onClick={saveAll}
            className="bg-indigo-600 text-white text-sm px-4 py-1.5 rounded-lg hover:bg-indigo-700 transition-colors"
          >
            Save All
          </button>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div className={`fixed top-4 right-4 z-50 px-4 py-2 rounded-lg text-white text-sm shadow-lg transition-all ${
          toast.type === "error" ? "bg-red-500" : "bg-green-500"
        }`}>
          {toast.msg}
        </div>
      )}

      {isPending && !pageData ? (
        <div className="text-gray-400 animate-pulse py-8 text-center">Loading…</div>
      ) : (
        <div className="space-y-6">
          {branches.map((branch) => {
            const currency = branch.currency || branch.native_currency || "VND";
            const symbol = CURRENCY_SYMBOL[currency] || currency;
            const isVND = currency === "VND";
            const rowDirty = hasRowEdits(branch.id);

            return (
              <div key={branch.id} className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                {/* Branch header */}
                <div className="flex items-center justify-between px-5 py-3 bg-gray-50 border-b border-gray-100">
                  <div>
                    <span className="font-semibold text-gray-800">{branch.name}</span>
                    <span className="ml-2 text-xs text-gray-400">{symbol} · {currency}</span>
                    {isVND && (
                      <span className="ml-2 text-xs text-gray-400 italic">(enter in millions, e.g. 3000 = ₫3B)</span>
                    )}
                  </div>
                  <button
                    onClick={() => saveRow(branch)}
                    disabled={!rowDirty || saving[branch.id]}
                    className={`text-sm px-4 py-1.5 rounded-lg transition-colors ${
                      rowDirty && !saving[branch.id]
                        ? "bg-indigo-600 text-white hover:bg-indigo-700"
                        : "bg-gray-100 text-gray-400 cursor-not-allowed"
                    }`}
                  >
                    {saving[branch.id] ? "Saving…" : "Save Row"}
                  </button>
                </div>

                {/* Months grid */}
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-gray-100">
                        <td className="px-3 py-2 text-xs text-gray-400 font-medium w-20">Field</td>
                        {MONTHS.map((m) => (
                          <td key={m} className="px-2 py-2 text-xs font-medium text-gray-500 text-center min-w-[90px]">
                            {m}
                          </td>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {/* Revenue row */}
                      <tr className="border-b border-gray-50">
                        <td className="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">
                          Revenue<br/>{isVND ? "(M)" : ""}
                        </td>
                        {MONTHS.map((_, idx) => {
                          const month = idx + 1;
                          const key = cellKey(branch.id, month);
                          const existing = targetsMap[key];
                          const edit = edits[key];
                          const isDirty = edit?.revInput !== undefined;

                          const placeholder = existing?.target_revenue_native
                            ? fmtRevenue(existing.target_revenue_native, currency)
                            : "—";

                          return (
                            <td key={month} className="px-2 py-1.5 text-center">
                              <input
                                type="text"
                                inputMode="numeric"
                                className={`w-full text-center text-xs rounded-md px-1.5 py-1 border transition-colors ${
                                  isDirty
                                    ? "border-amber-400 bg-amber-50"
                                    : existing?.target_revenue_native
                                    ? "border-gray-200 bg-white"
                                    : "border-dashed border-gray-200 bg-gray-50"
                                }`}
                                placeholder={placeholder}
                                value={isDirty ? edit.revInput : ""}
                                onChange={(e) =>
                                  handleRevChange(branch.id, month, currency, e.target.value)
                                }
                              />
                            </td>
                          );
                        })}
                      </tr>

                      {/* OCC% row */}
                      <tr>
                        <td className="px-3 py-2 text-xs text-gray-400 whitespace-nowrap">
                          OCC%
                        </td>
                        {MONTHS.map((_, idx) => {
                          const month = idx + 1;
                          const key = cellKey(branch.id, month);
                          const existing = targetsMap[key];
                          const edit = edits[key];
                          const isDirty = edit?.occInput !== undefined;

                          const placeholder = existing?.predicted_occ_pct != null
                            ? (existing.predicted_occ_pct * 100).toFixed(0)
                            : "—";

                          return (
                            <td key={month} className="px-2 py-1.5 text-center">
                              <input
                                type="text"
                                inputMode="decimal"
                                className={`w-full text-center text-xs rounded-md px-1.5 py-1 border transition-colors ${
                                  isDirty
                                    ? "border-amber-400 bg-amber-50"
                                    : existing?.predicted_occ_pct != null
                                    ? "border-gray-200 bg-white"
                                    : "border-dashed border-gray-200 bg-gray-50"
                                }`}
                                placeholder={placeholder}
                                value={isDirty ? edit.occInput : ""}
                                onChange={(e) =>
                                  handleOccChange(branch.id, month, e.target.value)
                                }
                              />
                            </td>
                          );
                        })}
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
