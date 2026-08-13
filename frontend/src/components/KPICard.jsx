/**
 * KPICard — shows a single KPI metric with achievement % and color band.
 * Props:
 *   label        string
 *   actual       number
 *   target       number | null
 *   currency     string  (e.g. "VND", "USD")
 *   suffix       string  (optional, e.g. "%")
 *   forecast     { runrate, occ, occLabel } | null
 *                occLabel names the assumption behind occ — "Revenue Forecast"
 *                on its own reads as a prediction rather than as the revenue
 *                that follows IF occupancy lands where it was set.
 */

// Same thresholds for the actual and the forecast, so a forecast reading 95%
// carries the same colour a 95% actual would.
const bandFor = (pct) =>
  pct === null ? "bg-gray-100 text-gray-500"
  : pct >= 1.0 ? "bg-green-100 text-green-700"
  : pct >= 0.8 ? "bg-yellow-100 text-yellow-700"
  : pct >= 0.6 ? "bg-orange-100 text-orange-700"
  :              "bg-red-100 text-red-700";

const barFor = (pct) =>
  pct === null ? "bg-gray-300"
  : pct >= 1.0 ? "bg-green-500"
  : pct >= 0.8 ? "bg-yellow-400"
  : pct >= 0.6 ? "bg-orange-400"
  :              "bg-red-500";

const widthFor = (pct) => (pct !== null ? `${Math.min(pct * 100, 100).toFixed(2)}%` : "0%");

export default function KPICard({ label, actual, target, currency, suffix = "", forecast }) {
  const pct = target > 0 ? actual / target : null;
  const forecastPct = target > 0 && forecast?.occ != null ? forecast.occ / target : null;

  const fmt = (n) => {
    if (n == null) return "—";
    if (suffix === "%") return `${(n * 100).toFixed(2)}%`;
    return new Intl.NumberFormat("en").format(Math.round(n));
  };
  const unit = suffix === "%" ? "" : ` ${currency || ""}`;

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-1">{label}</p>

      <div className="flex items-end justify-between mt-1">
        <span className="text-2xl font-bold text-gray-800">
          {fmt(actual)}{unit}
        </span>
        {pct !== null && (
          <span className={`text-sm font-semibold px-2 py-0.5 rounded-full ${bandFor(pct)}`}>
            {(pct * 100).toFixed(2)}%
          </span>
        )}
      </div>

      {/* Progress bar */}
      <div className="mt-3 h-1.5 bg-gray-100 rounded-full overflow-hidden">
        <div className={`h-full ${barFor(pct)} transition-all duration-500`} style={{ width: widthFor(pct) }} />
      </div>

      {target != null && (
        <p className="text-xs text-gray-400 mt-1">
          Target: {fmt(target)} {currency || ""}
        </p>
      )}

      {/* Forecast — its own headline, %-of-target badge and bar, so "are we on
          track to land the month" is answerable without doing the division. */}
      {forecast?.occ != null && (
        <div className="mt-4 pt-3 border-t border-gray-100">
          <div className="flex items-end justify-between gap-3">
            <div className="min-w-0">
              <p className="text-xs font-semibold uppercase tracking-wider text-gray-500">
                Revenue Forecast
              </p>
              {forecast.occLabel && (
                <p className="text-[11px] text-gray-400 mt-0.5">{forecast.occLabel}</p>
              )}
            </div>
            {forecastPct !== null && (
              <span className={`shrink-0 text-sm font-semibold px-2 py-0.5 rounded-full ${bandFor(forecastPct)}`}>
                {(forecastPct * 100).toFixed(2)}%
              </span>
            )}
          </div>

          <p className="text-xl font-bold text-gray-800 mt-2">
            {fmt(forecast.occ)}{unit}
          </p>

          <div className="mt-2 h-1.5 bg-gray-100 rounded-full overflow-hidden">
            <div
              className={`h-full ${barFor(forecastPct)} transition-all duration-500`}
              style={{ width: widthFor(forecastPct) }}
            />
          </div>
          <p className="text-xs text-gray-400 mt-1">of target</p>
        </div>
      )}

      {forecast?.runrate != null && (
        <div className="mt-3 pt-3 border-t border-gray-100">
          <p className="text-xs text-gray-400">Run-rate</p>
          <p className="text-sm font-medium text-gray-700">{fmt(forecast.runrate)}{unit}</p>
        </div>
      )}
    </div>
  );
}
