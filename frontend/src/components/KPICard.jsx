/**
 * KPICard — shows a single KPI metric with achievement % and color band.
 * Props:
 *   label        string
 *   actual       number
 *   target       number | null
 *   currency     string  (e.g. "VND", "USD")
 *   suffix       string  (optional, e.g. "%")
 *   forecast     { runrate, occ, occLabel } | null
 *                occLabel names the assumption behind occ — "OCC Forecast"
 *                alone reads as a prediction of its own rather than as the
 *                revenue that follows IF occupancy lands where it was set.
 */
export default function KPICard({ label, actual, target, currency, suffix = "", forecast }) {
  const pct = target > 0 ? actual / target : null;

  const bandColor =
    pct === null   ? "bg-gray-100 text-gray-500"
    : pct >= 1.0   ? "bg-green-100 text-green-700"
    : pct >= 0.8   ? "bg-yellow-100 text-yellow-700"
    : pct >= 0.6   ? "bg-orange-100 text-orange-700"
    : "bg-red-100 text-red-700";

  const barColor =
    pct === null   ? "bg-gray-300"
    : pct >= 1.0   ? "bg-green-500"
    : pct >= 0.8   ? "bg-yellow-400"
    : pct >= 0.6   ? "bg-orange-400"
    : "bg-red-500";

  const barWidth = pct !== null ? `${Math.min(pct * 100, 100).toFixed(2)}%` : "0%";

  const fmt = (n) => {
    if (n == null) return "—";
    if (suffix === "%") return `${(n * 100).toFixed(2)}%`;
    return new Intl.NumberFormat("en").format(Math.round(n));
  };

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">{label}</p>

      <div className="flex items-end justify-between mt-1">
        <span className="text-2xl font-bold text-gray-800">
          {fmt(actual)}{suffix === "%" ? "" : ` ${currency || ""}`}
        </span>
        {pct !== null && (
          <span className={`text-sm font-semibold px-2 py-0.5 rounded-full ${bandColor}`}>
            {(pct * 100).toFixed(2)}%
          </span>
        )}
      </div>

      {/* Progress bar */}
      <div className="mt-3 h-1.5 bg-gray-100 rounded-full overflow-hidden">
        <div className={`h-full ${barColor} transition-all duration-500`} style={{ width: barWidth }} />
      </div>

      {target != null && (
        <p className="text-xs text-gray-400 mt-1">
          Target: {fmt(target)} {currency || ""}
        </p>
      )}

      {/* Forecasts */}
      {forecast && (
        <div className={
          "mt-3 pt-3 border-t border-gray-100 grid gap-2 " +
          // A lone forecast gets the full width — its label spells out the
          // occupancy assumption and needs the room.
          (forecast.runrate != null && forecast.occ != null ? "grid-cols-2" : "grid-cols-1")
        }>
          {forecast.runrate != null && (
            <div>
              <p className="text-xs text-gray-400">Run-rate</p>
              <p className="text-sm font-medium text-gray-700">{fmt(forecast.runrate)}</p>
            </div>
          )}
          {forecast.occ != null && (
            <div>
              <p className="text-xs text-gray-400">{forecast.occLabel || "OCC Forecast"}</p>
              <p className="text-sm font-medium text-gray-700">
                {fmt(forecast.occ)}{suffix === "%" ? "" : ` ${currency || ""}`}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
