/**
 * KOL Insights — Paid Ads Opportunities — Phase 3
 */
import axios from "axios";
import { useQuery } from "@tanstack/react-query";
import { useBranch } from "../context/BranchContext";
import SyncBadge from "../components/SyncBadge";

function OpportunityBadge({ types }) {
  if (!types || types.length === 0) return null;
  return (
    <div className="flex gap-1 flex-wrap">
      {types.map((t, i) => (
        <span key={i} className="px-2 py-0.5 rounded text-xs font-medium bg-orange-100 text-orange-700">{t}</span>
      ))}
    </div>
  );
}

export default function Insights() {
  const { selected, isAll } = useBranch();
  const branchParam = !isAll && selected ? selected : null;

  const { data: rows = [], isLoading: loading } = useQuery({
    queryKey: ["insights", branchParam],
    queryFn: () => {
      const params = new URLSearchParams();
      if (branchParam) params.set("branch_id", branchParam);
      return axios.get("/api/insights?" + params).then(r => r.data.data || []);
    },
  });

  const actionable = rows.filter(r => r.is_actionable);
  const rest = rows.filter(r => !r.is_actionable);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-bold text-gray-800">KOL Insights</h1>
        <p className="text-xs text-gray-400 mt-0.5">
          KOL content eligible for paid ads — untapped opportunities
          <SyncBadge timestamp={rows[0]?.data_synced_at} />
        </p>
      </div>

      {loading ? (
        <div className="bg-white rounded-xl border p-8 text-center text-gray-400 animate-pulse">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="bg-white rounded-xl border p-8 text-center text-gray-400">
          No KOL records marked as paid ads eligible yet. Go to KOL page and mark KOLs as eligible.
        </div>
      ) : (
        <>
          {actionable.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold text-orange-600 flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-orange-500 inline-block"></span>
                Action Required ({actionable.length})
              </h2>
              <div className="space-y-2">
                {actionable.map(r => <KOLCard key={r.id} row={r} />)}
              </div>
            </div>
          )}
          {rest.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold text-gray-500">All Eligible KOLs ({rest.length})</h2>
              <div className="space-y-2">
                {rest.map(r => <KOLCard key={r.id} row={r} />)}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function KOLCard({ row }) {
  return (
    <div className={"bg-white rounded-xl border p-4 " + (row.is_actionable ? "border-orange-200" : "border-gray-200")}>
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1 space-y-1">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-gray-800">{row.kol_name}</span>
            {row.kol_nationality && <span className="text-xs text-gray-400">{row.kol_nationality}</span>}
            {row.language && <span className="text-xs text-gray-400">· {row.language}</span>}
          </div>
          {row.target_audience && <p className="text-xs text-gray-500">Audience: {row.target_audience}</p>}
          {row.published_date && <p className="text-xs text-gray-400">Published: {row.published_date}</p>}
          <OpportunityBadge types={row.opportunity_type} />
        </div>
        <div className="text-right space-y-1 shrink-0">
          {row.paid_ads_channel ? (
            <span className="text-xs text-green-600 font-medium">{row.paid_ads_channel}</span>
          ) : (
            <span className="text-xs text-red-500 font-medium">No channel set</span>
          )}
          {row.usage_rights_expiry_date && (
            <p className="text-xs text-gray-400">Expires: {row.usage_rights_expiry_date}</p>
          )}
        </div>
      </div>
      <div className="flex gap-3 mt-3">
        {row.link_ig && <a href={row.link_ig} target="_blank" rel="noopener noreferrer" className="text-xs text-pink-500 hover:underline">Instagram</a>}
        {row.link_tiktok && <a href={row.link_tiktok} target="_blank" rel="noopener noreferrer" className="text-xs text-gray-600 hover:underline">TikTok</a>}
        {row.link_youtube && <a href={row.link_youtube} target="_blank" rel="noopener noreferrer" className="text-xs text-red-500 hover:underline">YouTube</a>}
      </div>
    </div>
  );
}
