import { useState, useMemo, useRef, useEffect } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { getSeasonMatrix, getCountryHolidays } from "../api/holidayIntel";

/* ── Country metadata ──────────────────────────────────────────────────── */
const ALL_COUNTRIES = [
  "VN","TW","JP","KR","CN","US","AU","TH","SG","MY","HK","IN","ID","PH","CA",
  "GB","FR","DE","NL","ES","IT","CH","SE","DK","NO",
];

const COUNTRY_NAME = {
  VN:"Vietnam",TW:"Taiwan",JP:"Japan",KR:"South Korea",CN:"China",
  US:"USA",AU:"Australia",TH:"Thailand",SG:"Singapore",MY:"Malaysia",
  HK:"Hong Kong",IN:"India",ID:"Indonesia",PH:"Philippines",CA:"Canada",
  GB:"United Kingdom",FR:"France",DE:"Germany",NL:"Netherlands",ES:"Spain",
  IT:"Italy",CH:"Switzerland",SE:"Sweden",DK:"Denmark",NO:"Norway",
};

const FLAG = {
  VN:"\u{1F1FB}\u{1F1F3}",TW:"\u{1F1F9}\u{1F1FC}",JP:"\u{1F1EF}\u{1F1F5}",
  KR:"\u{1F1F0}\u{1F1F7}",CN:"\u{1F1E8}\u{1F1F3}",US:"\u{1F1FA}\u{1F1F8}",
  AU:"\u{1F1E6}\u{1F1FA}",TH:"\u{1F1F9}\u{1F1ED}",SG:"\u{1F1F8}\u{1F1EC}",
  MY:"\u{1F1F2}\u{1F1FE}",HK:"\u{1F1ED}\u{1F1F0}",IN:"\u{1F1EE}\u{1F1F3}",
  ID:"\u{1F1EE}\u{1F1E9}",PH:"\u{1F1F5}\u{1F1ED}",CA:"\u{1F1E8}\u{1F1E6}",
  GB:"\u{1F1EC}\u{1F1E7}",FR:"\u{1F1EB}\u{1F1F7}",DE:"\u{1F1E9}\u{1F1EA}",
  NL:"\u{1F1F3}\u{1F1F1}",ES:"\u{1F1EA}\u{1F1F8}",IT:"\u{1F1EE}\u{1F1F9}",
  CH:"\u{1F1E8}\u{1F1ED}",SE:"\u{1F1F8}\u{1F1EA}",DK:"\u{1F1E9}\u{1F1F0}",
  NO:"\u{1F1F3}\u{1F1F4}",
};

const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

/* ── Duration-based cell color (green intensity by long_holiday_days) ── */
function durationColor(days) {
  if (!days || days <= 0) return "bg-gray-50 text-gray-300";
  if (days >= 30) return "bg-green-700 text-white";
  if (days >= 14) return "bg-green-600 text-white";
  if (days >= 7)  return "bg-green-500 text-white";
  if (days >= 4)  return "bg-green-400 text-white";
  return "bg-green-200 text-green-800";
}

function durationLabel(days) {
  if (!days || days <= 0) return "";
  return `${days}d`;
}

/* ── PropensityBadge ─────────────────────────────────────────────────── */
function PropBadge({ level }) {
  const cls = level === "HIGH" ? "bg-green-100 text-green-700" : level === "MEDIUM" ? "bg-yellow-100 text-yellow-700" : "bg-gray-100 text-gray-500";
  return <span className={`px-1.5 py-0.5 rounded text-xs font-semibold ${cls}`}>{level}</span>;
}

/* ── Searchable country filter dropdown ──────────────────────────────── */
function CountrySearch({ value, onChange }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef(null);

  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const filtered = useMemo(() => {
    if (!query) return ALL_COUNTRIES;
    const q = query.toLowerCase();
    return ALL_COUNTRIES.filter(c =>
      COUNTRY_NAME[c].toLowerCase().includes(q) || c.toLowerCase().includes(q)
    );
  }, [query]);

  const selected = value.length;

  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(!open)}
        className="flex items-center gap-2 border border-gray-300 rounded-lg px-3 py-1.5 text-sm bg-white hover:border-gray-400 transition-colors min-w-[200px]">
        <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>
        <span className="text-gray-600">
          {selected === ALL_COUNTRIES.length ? "All 25 countries" : `${selected} countries selected`}
        </span>
        <svg className="w-3 h-3 text-gray-400 ml-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
      </button>
      {open && (
        <div className="absolute z-30 mt-1 w-72 bg-white border border-gray-200 rounded-lg shadow-lg max-h-80 overflow-hidden">
          <div className="p-2 border-b">
            <input
              autoFocus
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search country..."
              className="w-full px-2 py-1.5 text-sm border border-gray-200 rounded focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none"
            />
          </div>
          <div className="flex items-center gap-2 px-3 py-1.5 border-b bg-gray-50">
            <button onClick={() => onChange(ALL_COUNTRIES)} className="text-xs text-indigo-600 hover:underline">Select all</button>
            <span className="text-gray-300">|</span>
            <button onClick={() => onChange([])} className="text-xs text-gray-500 hover:underline">Clear</button>
          </div>
          <div className="overflow-y-auto max-h-56">
            {filtered.map(code => {
              const active = value.includes(code);
              return (
                <button key={code}
                  onClick={() => onChange(active ? value.filter(c => c !== code) : [...value, code])}
                  className={`w-full flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-gray-50 transition-colors ${active ? "bg-indigo-50" : ""}`}>
                  <span className={`w-4 h-4 rounded border flex items-center justify-center text-xs ${active ? "bg-indigo-600 border-indigo-600 text-white" : "border-gray-300"}`}>
                    {active && "✓"}
                  </span>
                  <span>{FLAG[code]}</span>
                  <span className="text-gray-700">{COUNTRY_NAME[code]}</span>
                  <span className="text-gray-400 text-xs ml-auto">{code}</span>
                </button>
              );
            })}
            {filtered.length === 0 && <p className="text-xs text-gray-400 text-center py-3">No match</p>}
          </div>
        </div>
      )}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════════════════ */
export default function HolidayIntel() {
  // Filter + expand state
  const [visibleCountries, setVisibleCountries] = useState(ALL_COUNTRIES);
  const [expandedCountry, setExpandedCountry] = useState(null);
  const [expandedMonth, setExpandedMonth] = useState(null);   // null = show all, 1-12 = filter

  // ── Load initial data ─────────────────────────────────────────────
  const { data: matrix, isPending: matrixPending } = useQuery({
    queryKey: ["holiday-season-matrix"],
    queryFn: () => getSeasonMatrix(),
    placeholderData: keepPreviousData,
  });

  // ── Load holiday detail when a country is expanded ────────────────
  const { data: expandedHolidays } = useQuery({
    queryKey: ["holiday-country-detail", expandedCountry],
    queryFn: () => getCountryHolidays(expandedCountry),
    enabled: !!expandedCountry,
    placeholderData: keepPreviousData,
  });

  // ── Build heatmap lookup ──────────────────────────────────────────
  const matrixMap = useMemo(() => {
    const m = {};
    (matrix || []).forEach(r => { m[`${r.country_code}_${r.month}`] = r; });
    return m;
  }, [matrix]);

  const toggleCountry = (code) => {
    setExpandedCountry(prev => prev === code ? null : code);
    setExpandedMonth(null);
  };

  const clickMonthCell = (code, month) => {
    if (expandedCountry === code && expandedMonth === month) {
      setExpandedCountry(null);
      setExpandedMonth(null);
    } else {
      setExpandedCountry(code);
      setExpandedMonth(month);
    }
  };

  // Filter holidays by selected month (holiday spans month_start to month_end)
  const filteredHolidays = useMemo(() => {
    const holidays = expandedHolidays || [];
    if (!expandedMonth) return holidays;
    return holidays.filter(h => {
      if (h.month_end >= h.month_start) {
        return expandedMonth >= h.month_start && expandedMonth <= h.month_end;
      }
      // Wraps around year (e.g. Dec–Feb)
      return expandedMonth >= h.month_start || expandedMonth <= h.month_end;
    });
  }, [expandedHolidays, expandedMonth]);

  const currentMonth = new Date().getMonth() + 1;

  if (matrixPending && !matrix) {
    return <div className="flex items-center justify-center h-64 text-gray-400 text-sm animate-pulse">Loading Holiday Intelligence...</div>;
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Holiday Intelligence</h1>
        <p className="text-sm text-gray-500 mt-1">Country travel season planner — click any country to see holiday details</p>
      </div>

      {/* ═══ Season Heatmap ════════════════════════════════════════════ */}
      <section className="bg-white rounded-lg shadow p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold text-gray-800">Season Heatmap</h2>
          <CountrySearch value={visibleCountries} onChange={setVisibleCountries} />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr>
                <th className="text-left py-2 px-2 font-semibold text-gray-600 sticky left-0 bg-white z-10 w-36">Country</th>
                {MONTHS.map((m,i) => (
                  <th key={i} className={`text-center py-2 px-1 font-medium w-14 ${i+1 === currentMonth ? "text-indigo-600 font-bold" : "text-gray-500"}`}>{m}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visibleCountries.map(code => {
                const isExpanded = expandedCountry === code;
                const countryHolidays = isExpanded ? (expandedHolidays || []) : [];
                return (
                  <Fragment key={code}>
                    <tr className={`border-t border-gray-100 cursor-pointer transition-colors ${isExpanded ? "bg-indigo-50" : "hover:bg-gray-50"}`}
                      onClick={() => toggleCountry(code)}>
                      <td className="py-1.5 px-2 sticky left-0 z-10" style={{ backgroundColor: isExpanded ? "rgb(238 242 255)" : "white" }}>
                        <div className="flex items-center gap-1.5">
                          <span className="text-gray-400 text-[10px] w-3">{isExpanded ? "▼" : "▶"}</span>
                          <span className="text-sm">{FLAG[code]}</span>
                          <span className={`font-medium ${isExpanded ? "text-indigo-700" : "text-gray-700"}`}>{COUNTRY_NAME[code]}</span>
                        </div>
                      </td>
                      {Array.from({length:12},(_,i)=>i+1).map(m => {
                        const cell = matrixMap[`${code}_${m}`];
                        const days = cell?.long_holiday_days || 0;
                        const isMonthSelected = isExpanded && expandedMonth === m;
                        return (
                          <td key={m} className="text-center py-1.5 px-0.5"
                            title={cell ? `${days}d holidays | ${(cell.holiday_names||[]).join(", ")}` : "No holidays"}
                            onClick={(e) => { e.stopPropagation(); if (days > 0) clickMonthCell(code, m); }}>
                            <div className={`mx-auto w-11 py-1 rounded text-[11px] font-semibold transition-all ${durationColor(days)} ${days > 0 ? "hover:ring-2 hover:ring-indigo-400" : ""} ${isMonthSelected ? "ring-2 ring-indigo-600" : ""}`}>
                              {durationLabel(days) || "—"}
                            </div>
                          </td>
                        );
                      })}
                    </tr>

                    {/* ── Expanded holiday detail row ─────────────────── */}
                    {isExpanded && (
                      <tr>
                        <td colSpan={13} className="bg-indigo-50 px-4 py-3 border-b border-indigo-100">
                          {countryHolidays.length === 0 ? (
                            <p className="text-xs text-gray-400 animate-pulse">Loading holidays...</p>
                          ) : (
                            <div>
                              {/* Month filter hint */}
                              <div className="flex items-center gap-2 mb-3">
                                {expandedMonth ? (
                                  <>
                                    <span className="text-xs text-indigo-700 font-medium">
                                      Showing holidays in {MONTHS[expandedMonth-1]} ({filteredHolidays.length})
                                    </span>
                                    <button onClick={(e) => { e.stopPropagation(); setExpandedMonth(null); }}
                                      className="text-[10px] text-indigo-600 hover:underline">Show all</button>
                                  </>
                                ) : (
                                  <span className="text-xs text-gray-500">All holidays ({countryHolidays.length}) — click a month cell above to filter</span>
                                )}
                              </div>

                              {/* Holiday cards */}
                              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2">
                                {filteredHolidays.map(h => (
                                  <div key={h.id} className="bg-white border border-gray-200 rounded-lg p-2.5">
                                    <div className="flex items-start justify-between mb-1">
                                      <h3 className="font-semibold text-gray-800 text-xs leading-tight">{h.holiday_name}</h3>
                                      <PropBadge level={h.travel_propensity} />
                                    </div>
                                    <p className="text-[11px] text-gray-500">
                                      {MONTHS[h.month_start-1]}{h.day_start ? ` ${h.day_start}` : ""} – {MONTHS[h.month_end-1]}{h.day_end ? ` ${h.day_end}` : ""} ({h.duration_days}d)
                                    </p>
                                    <div className="flex items-center gap-2 mt-1.5">
                                      <span className="text-[10px] text-gray-400">{h.holiday_type}</span>
                                      {h.is_long_holiday && <span className="text-[10px] bg-blue-100 text-blue-600 px-1 py-0.5 rounded font-medium">Long</span>}
                                      {h.days_until_next != null && h.days_until_next >= 0 && (
                                        <span className={`text-[10px] font-bold ml-auto ${h.days_until_next <= 30 ? "text-red-600" : h.days_until_next <= 60 ? "text-orange-600" : "text-gray-500"}`}>
                                          {h.days_until_next === 0 ? "Today!" : `${h.days_until_next}d away`}
                                        </span>
                                      )}
                                    </div>
                                    {h.notes && <p className="text-[10px] text-gray-400 mt-1">{h.notes}</p>}
                                  </div>
                                ))}
                                {filteredHolidays.length === 0 && (
                                  <p className="text-xs text-gray-400 col-span-full py-2">No holidays in {MONTHS[expandedMonth-1]}.</p>
                                )}
                              </div>
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        {/* Legend */}
        <div className="flex items-center gap-3 mt-3 text-xs text-gray-500">
          <span>Holiday duration:</span>
          <span className="flex items-center gap-1"><span className="inline-block w-8 py-0.5 rounded text-center text-[10px] font-semibold bg-green-200 text-green-800">4d</span> Short</span>
          <span className="flex items-center gap-1"><span className="inline-block w-8 py-0.5 rounded text-center text-[10px] font-semibold bg-green-400 text-white">7d</span></span>
          <span className="flex items-center gap-1"><span className="inline-block w-8 py-0.5 rounded text-center text-[10px] font-semibold bg-green-500 text-white">14d</span></span>
          <span className="flex items-center gap-1"><span className="inline-block w-8 py-0.5 rounded text-center text-[10px] font-semibold bg-green-600 text-white">30d</span></span>
          <span className="flex items-center gap-1"><span className="inline-block w-8 py-0.5 rounded text-center text-[10px] font-semibold bg-green-700 text-white">60d+</span> Long</span>
        </div>
      </section>
    </div>
  );
}

/* React.Fragment shorthand */
const Fragment = ({ children }) => <>{children}</>;
