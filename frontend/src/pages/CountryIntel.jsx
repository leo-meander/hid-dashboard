import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { keepPreviousData } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useBranch } from "../context/BranchContext";
import { getUpcomingWindows } from "../api/holidayIntel";

const fmt = (n) =>
  n == null ? "—" : new Intl.NumberFormat("en").format(Math.round(n));

const ISO3_TO_2 = {
  AFG:"AF",ALB:"AL",DZA:"DZ",AND:"AD",AGO:"AO",ARG:"AR",ARM:"AM",AUS:"AU",AUT:"AT",AZE:"AZ",
  BHS:"BS",BHR:"BH",BGD:"BD",BLR:"BY",BEL:"BE",BLZ:"BZ",BEN:"BJ",BTN:"BT",BOL:"BO",BIH:"BA",
  BWA:"BW",BRA:"BR",BRN:"BN",BGR:"BG",BFA:"BF",BDI:"BI",CPV:"CV",KHM:"KH",CMR:"CM",CAN:"CA",
  CAF:"CF",TCD:"TD",CHL:"CL",CHN:"CN",COL:"CO",COM:"KM",COD:"CD",COG:"CG",CRI:"CR",CIV:"CI",
  HRV:"HR",CUB:"CU",CYP:"CY",CZE:"CZ",DNK:"DK",DJI:"DJ",DOM:"DO",ECU:"EC",EGY:"EG",SLV:"SV",
  GNQ:"GQ",ERI:"ER",EST:"EE",SWZ:"SZ",ETH:"ET",FJI:"FJ",FIN:"FI",FRA:"FR",GAB:"GA",GMB:"GM",
  GEO:"GE",DEU:"DE",GHA:"GH",GRC:"GR",GTM:"GT",GIN:"GN",GNB:"GW",GUY:"GY",HTI:"HT",HND:"HN",
  HUN:"HU",ISL:"IS",IND:"IN",IDN:"ID",IRN:"IR",IRQ:"IQ",IRL:"IE",ISR:"IL",ITA:"IT",JAM:"JM",
  JPN:"JP",JOR:"JO",KAZ:"KZ",KEN:"KE",KWT:"KW",KGZ:"KG",LAO:"LA",LVA:"LV",LBN:"LB",LSO:"LS",
  LBR:"LR",LBY:"LY",LIE:"LI",LTU:"LT",LUX:"LU",MDG:"MG",MWI:"MW",MYS:"MY",MDV:"MV",MLI:"ML",
  MLT:"MT",MRT:"MR",MUS:"MU",MEX:"MX",MDA:"MD",MCO:"MC",MNG:"MN",MNE:"ME",MAR:"MA",MOZ:"MZ",
  MMR:"MM",NAM:"NA",NPL:"NP",NLD:"NL",NZL:"NZ",NIC:"NI",NER:"NE",NGA:"NG",MKD:"MK",NOR:"NO",
  OMN:"OM",PAK:"PK",PAN:"PA",PNG:"PG",PRY:"PY",PER:"PE",PHL:"PH",POL:"PL",PRT:"PT",QAT:"QA",
  ROU:"RO",RUS:"RU",RWA:"RW",SAU:"SA",SEN:"SN",SRB:"RS",SLE:"SL",SGP:"SG",SVK:"SK",SVN:"SI",
  SOM:"SO",ZAF:"ZA",ESP:"ES",LKA:"LK",SDN:"SD",SUR:"SR",SWE:"SE",CHE:"CH",SYR:"SY",TWN:"TW",
  TJK:"TJ",TZA:"TZ",THA:"TH",TLS:"TL",TGO:"TG",TTO:"TT",TUN:"TN",TUR:"TR",TKM:"TM",UGA:"UG",
  UKR:"UA",ARE:"AE",GBR:"GB",USA:"US",URY:"UY",UZB:"UZ",VEN:"VE",VNM:"VN",YEM:"YE",ZMB:"ZM",
  ZWE:"ZW",HKG:"HK",MAC:"MO",KOR:"KR",PRK:"KP",
};

const NAME_TO_2 = {
  "afghanistan":"AF","albania":"AL","algeria":"DZ","andorra":"AD","angola":"AO","argentina":"AR",
  "armenia":"AM","australia":"AU","austria":"AT","azerbaijan":"AZ","bahrain":"BH","bangladesh":"BD",
  "belarus":"BY","belgium":"BE","belize":"BZ","bhutan":"BT","bolivia":"BO","bosnia":"BA",
  "botswana":"BW","brazil":"BR","brunei":"BN","bulgaria":"BG","cambodia":"KH","cameroon":"CM",
  "canada":"CA","chile":"CL","china":"CN","colombia":"CO","croatia":"HR","cuba":"CU","cyprus":"CY",
  "czech republic":"CZ","czechia":"CZ","denmark":"DK","ecuador":"EC","egypt":"EG","estonia":"EE",
  "ethiopia":"ET","fiji":"FJ","finland":"FI","france":"FR","germany":"DE","ghana":"GH","greece":"GR",
  "guatemala":"GT","hong kong":"HK","hungary":"HU","iceland":"IS","india":"IN","indonesia":"ID",
  "iran":"IR","iraq":"IQ","ireland":"IE","israel":"IL","italy":"IT","jamaica":"JM","japan":"JP",
  "jordan":"JO","kazakhstan":"KZ","kenya":"KE","kuwait":"KW","laos":"LA","latvia":"LV","lebanon":"LB",
  "libya":"LY","liechtenstein":"LI","lithuania":"LT","luxembourg":"LU","macau":"MO","macao":"MO",
  "malaysia":"MY","maldives":"MV","mali":"ML","malta":"MT","mauritius":"MU","mexico":"MX",
  "moldova":"MD","monaco":"MC","mongolia":"MN","montenegro":"ME","morocco":"MA","mozambique":"MZ",
  "myanmar":"MM","nepal":"NP","netherlands":"NL","new zealand":"NZ","nicaragua":"NI","nigeria":"NG",
  "north korea":"KP","norway":"NO","oman":"OM","pakistan":"PK","panama":"PA","paraguay":"PY",
  "peru":"PE","philippines":"PH","poland":"PL","portugal":"PT","qatar":"QA","romania":"RO",
  "russia":"RU","saudi arabia":"SA","senegal":"SN","serbia":"RS","singapore":"SG","slovakia":"SK",
  "slovenia":"SI","somalia":"SO","south africa":"ZA","south korea":"KR","spain":"ES","sri lanka":"LK",
  "sweden":"SE","switzerland":"CH","syria":"SY","taiwan":"TW","tajikistan":"TJ","tanzania":"TZ",
  "thailand":"TH","turkey":"TR","turkmenistan":"TM","uganda":"UG","ukraine":"UA",
  "united arab emirates":"AE","united kingdom":"GB","united states":"US","united states of america":"US",
  "uruguay":"UY","uzbekistan":"UZ","venezuela":"VE","vietnam":"VN","viet nam":"VN","yemen":"YE",
  "zambia":"ZM","zimbabwe":"ZW","lithuania":"LT","burkina faso":"BF","burundi":"BI","cabo verde":"CV",
  "democratic republic of congo":"CD","republic of congo":"CG","dominican republic":"DO",
  "el salvador":"SV","equatorial guinea":"GQ","eritrea":"ER","eswatini":"SZ","guinea":"GN",
  "guinea-bissau":"GW","guyana":"GY","haiti":"HT","honduras":"HN","ivory coast":"CI",
  "cote d'ivoire":"CI","kyrgyzstan":"KG","lesotho":"LS","liberia":"LR","madagascar":"MG",
  "malawi":"MW","mauritania":"MR","namibia":"NA","niger":"NE","north macedonia":"MK","papua new guinea":"PG",
  "rwanda":"RW","sierra leone":"SL","sudan":"SD","suriname":"SR","timor-leste":"TL","togo":"TG",
  "trinidad and tobago":"TT","tunisia":"TN","tuvalu":"TV",
};

const FLAG = (code) => {
  if (!code) return "🌐";
  const c = code.trim().toUpperCase();
  // Try ISO-2 directly
  let iso2 = c.length === 2 ? c : null;
  // Try ISO-3
  if (!iso2 && c.length === 3) iso2 = ISO3_TO_2[c] || null;
  // Try full country name
  if (!iso2) iso2 = NAME_TO_2[code.trim().toLowerCase()] || null;
  if (!iso2) return "🌐";
  try {
    return [...iso2].map((ch) =>
      String.fromCodePoint(0x1f1e6 - 65 + ch.charCodeAt(0))
    ).join("");
  } catch {
    return "🌐";
  }
};

function KolBadge({ kol }) {
  const hasOrganic = kol.organic_bookings > 0;
  return (
    <div className="bg-green-50 border border-green-200 rounded p-2 text-xs">
      <div className="font-semibold text-green-800">{kol.kol_name}</div>
      <div className="text-green-600 mt-0.5 space-y-0.5">
        {kol.target_audience && <div>Audience: {kol.target_audience}</div>}
        {kol.language && <div>Language: {kol.language}</div>}
        {hasOrganic ? (
          <div>{kol.organic_bookings} bookings · {fmt(kol.organic_revenue_vnd)} VND</div>
        ) : (
          <div className="text-gray-400">No organic bookings yet</div>
        )}
        {kol.status && <div>Status: {kol.status}</div>}
        <div className="flex gap-2 mt-1">
          {kol.link_ig && (
            <a href={kol.link_ig} target="_blank" rel="noopener noreferrer"
               className="text-indigo-600 hover:underline">IG</a>
          )}
          {kol.link_tiktok && (
            <a href={kol.link_tiktok} target="_blank" rel="noopener noreferrer"
               className="text-indigo-600 hover:underline">TikTok</a>
          )}
          {kol.link_youtube && (
            <a href={kol.link_youtube} target="_blank" rel="noopener noreferrer"
               className="text-indigo-600 hover:underline">YT</a>
          )}
        </div>
      </div>
    </div>
  );
}

const CHANNEL_COLORS = {
  Meta: "bg-blue-50 border-blue-200 text-blue-800",
  Google: "bg-green-50 border-green-200 text-green-800",
  TikTok: "bg-pink-50 border-pink-200 text-pink-800",
};

const STATUS_BADGE = {
  running: "bg-green-100 text-green-700 border-green-300",
  stopped: "bg-amber-100 text-amber-700 border-amber-300",
};

function AdsBadge({ ad }) {
  const channels = ad.channels || [];
  return (
    <div className="space-y-2">
      {channels.map((ch, i) => {
        const cls = CHANNEL_COLORS[ch.channel] || "bg-gray-50 border-gray-200 text-gray-800";
        const isRunning = ch.status === "running";
        return (
          <div key={i} className={"border rounded p-2 text-xs " + cls}>
            <div className="flex items-center justify-between mb-1">
              <span className="font-semibold">{ch.channel}</span>
              <span className={`px-1.5 py-0.5 rounded-full text-[10px] font-medium border ${STATUS_BADGE[ch.status] || "bg-gray-100 text-gray-500"}`}>
                {isRunning ? "● Running" : "○ Stopped"}
              </span>
            </div>
            <div className="grid grid-cols-3 gap-x-3 gap-y-0.5">
              <div><span className="opacity-60">Cost</span><br/><span className="font-mono font-medium">{fmt(ch.total_cost_native)}</span></div>
              <div><span className="opacity-60">Revenue</span><br/><span className="font-mono font-medium">{fmt(ch.total_revenue_native)}</span></div>
              <div><span className="opacity-60">ROAS</span><br/><span className="font-mono font-medium">{ch.roas != null ? ch.roas + "x" : "—"}</span></div>
            </div>
            {ch.last_active_date && !isRunning && (
              <div className="text-[10px] opacity-50 mt-1">Last active: {ch.last_active_date}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

const GOV_MONTHS = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"];
const GOV_MONTH_LABELS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];

function GovDataBadge({ gov }) {
  if (!gov) return null;
  const months = GOV_MONTHS.map((m) => gov[m] || 0);
  const maxVal = Math.max(...months, 1);
  return (
    <div className="bg-blue-50 border border-blue-200 rounded p-3 text-xs">
      <div className="flex items-center justify-between mb-2">
        <div className="font-semibold text-blue-800">
          Gov Data: {gov.source_country} → {gov.destination}
        </div>
        <div className="text-blue-600 font-mono font-bold">{fmt(gov.total)} visitors/yr</div>
      </div>
      <div className="flex items-end gap-0.5 h-10">
        {months.map((v, i) => (
          <div key={i} className="flex-1 flex flex-col items-center">
            <div
              className="w-full bg-blue-400 rounded-t"
              style={{ height: `${Math.max((v / maxVal) * 100, 2)}%` }}
              title={`${GOV_MONTH_LABELS[i]}: ${fmt(v)}`}
            />
          </div>
        ))}
      </div>
      <div className="flex gap-0.5 mt-0.5">
        {GOV_MONTH_LABELS.map((m) => (
          <div key={m} className="flex-1 text-center text-[8px] text-blue-400">{m}</div>
        ))}
      </div>
      {gov.rank && (
        <div className="text-blue-500 mt-1">Rank #{gov.rank} source market for {gov.destination}</div>
      )}
    </div>
  );
}

function CoverageDetail({ item }) {
  const hasKol = !item.kol_gap;
  const hasAds = !item.ads_gap;
  const hasGov = !!item.gov_visitor_data;
  return (
    <div className="space-y-4 mt-2">
      {/* Guest Segments by Adults → Top Room Types */}
      {item.room_type_stats?.length > 0 && (
        <div className="bg-purple-50 border border-purple-200 rounded p-3 text-xs">
          <div className="flex items-center justify-between mb-3">
            <span className="font-semibold text-purple-800">Guest Segments</span>
            <span className="text-[10px] text-gray-400 italic">Last 90 days</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {item.room_type_stats.map((seg) => (
              <div key={seg.adults} className="bg-white rounded border border-purple-100 p-2.5">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-semibold text-purple-800 text-sm">
                    {seg.adults} adult{seg.adults !== 1 ? "s" : ""}
                  </span>
                  <span className="font-mono text-purple-600 text-[11px]">
                    {seg.booking_count} ({seg.pct}%)
                  </span>
                </div>
                <div className="w-full bg-purple-100 rounded-full h-1.5 mb-2">
                  <div className="bg-purple-500 h-1.5 rounded-full" style={{ width: `${seg.pct}%` }} />
                </div>
                <div className="space-y-1">
                  {seg.top_rooms?.map((rm, i) => (
                    <div key={rm.room_type} className="flex items-center gap-1.5 text-[11px]">
                      <span className="text-gray-400 w-3">{i + 1}.</span>
                      <span className="text-gray-700 flex-1 truncate" title={rm.room_type}>{rm.room_type}</span>
                      <span className="font-mono text-purple-600 whitespace-nowrap">{rm.count} · {rm.pct}%</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <div className="text-xs font-semibold text-gray-500 uppercase mb-2">KOL Coverage</div>
          {hasKol ? (
            <div className="space-y-2">
              {item.kol_coverage.map((k) => <KolBadge key={k.kol_name} kol={k} />)}
            </div>
          ) : (
            <div className="bg-orange-50 border border-orange-200 rounded p-3 text-xs text-orange-700">
              <div className="font-semibold mb-1">Action needed</div>
              {item.action_items.filter((a) => a.includes("KOL")).map((a, i) => (
                <div key={i}>→ {a}</div>
              ))}
            </div>
          )}
        </div>
        <div>
          <div className="text-xs font-semibold text-gray-500 uppercase mb-2">Paid Ads Coverage</div>
          {hasAds ? (
            <div className="space-y-2">
              {item.ads_coverage.map((ad, i) => <AdsBadge key={i} ad={ad} />)}
            </div>
          ) : (
            <div className="bg-orange-50 border border-orange-200 rounded p-3 text-xs text-orange-700">
              <div className="font-semibold mb-1">Action needed</div>
              {item.action_items.filter((a) => a.includes("Ads")).map((a, i) => (
                <div key={i}>→ {a}</div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function VolumeRow({ item }) {
  const [expanded, setExpanded] = useState(false);
  const hasActions = item.action_items.length > 0;
  return (
    <>
      <tr
        className={`border-b hover:bg-gray-50 cursor-pointer ${hasActions ? "border-l-2 border-l-orange-400" : ""}`}
        onClick={() => setExpanded((v) => !v)}
      >
        <td className="px-3 py-2 text-xs text-gray-400 w-8">{item.rank}</td>
        <td className="px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="text-base">{FLAG(item.country_code)}</span>
            <span className="text-sm font-medium text-gray-800">{item.country}</span>
            {item.gov_visitor_data && (
              <span className="px-1.5 py-0.5 rounded bg-blue-100 text-blue-600 text-[10px] font-medium" title={`Gov rank #${item.gov_visitor_data.rank} · ${fmt(item.gov_visitor_data.total)} visitors/yr`}>
                GOV #{item.gov_visitor_data.rank}
              </span>
            )}
          </div>
        </td>
        <td className="px-3 py-2 text-sm text-right font-mono text-gray-700">{item.booking_count}</td>
        <td className="px-3 py-2">
          {!item.kol_gap ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-green-100 text-green-700 text-xs font-medium">
              ✓ {item.kol_coverage.length} KOL{item.kol_coverage.length > 1 ? "s" : ""}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-100 text-red-600 text-xs font-medium">
              ✕ No KOL
            </span>
          )}
        </td>
        <td className="px-3 py-2">
          {item.ads_status === "running" ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-green-100 text-green-700 text-xs font-medium">
              ● Running
            </span>
          ) : item.ads_status === "stopped" ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-100 text-amber-600 text-xs font-medium">
              ○ Stopped
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-100 text-red-600 text-xs font-medium">
              ✕ No Ads
            </span>
          )}
        </td>
        <td className="px-3 py-2">
          {hasActions ? (
            <span className="text-orange-500 text-xs font-medium">
              {item.action_items.length} action{item.action_items.length > 1 ? "s" : ""}
            </span>
          ) : (
            <span className="text-green-500 text-xs">✓ Covered</span>
          )}
        </td>
        <td className="px-3 py-2 text-gray-400 text-xs">{expanded ? "▲" : "▼"}</td>
      </tr>
      {expanded && (
        <tr className="bg-gray-50 border-b">
          <td colSpan={7} className="px-6 py-3">
            <CoverageDetail item={item} />
          </td>
        </tr>
      )}
    </>
  );
}

function GrowthRow({ item }) {
  const [expanded, setExpanded] = useState(false);
  const hasActions = item.action_items.length > 0;
  return (
    <>
      <tr
        className={`border-b hover:bg-gray-50 cursor-pointer ${hasActions ? "border-l-2 border-l-orange-400" : ""}`}
        onClick={() => setExpanded((v) => !v)}
      >
        <td className="px-3 py-2 text-xs text-gray-400 w-8">{item.rank}</td>
        <td className="px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="text-base">{FLAG(item.country_code)}</span>
            <span className="text-sm font-medium text-gray-800">{item.country}</span>
            {item.gov_visitor_data && (
              <span className="px-1.5 py-0.5 rounded bg-blue-100 text-blue-600 text-[10px] font-medium" title={`Gov rank #${item.gov_visitor_data.rank} · ${fmt(item.gov_visitor_data.total)} visitors/yr`}>
                GOV #{item.gov_visitor_data.rank}
              </span>
            )}
          </div>
        </td>
        <td className="px-3 py-2 text-sm text-right font-mono text-gray-700">{item.recent_bookings}</td>
        <td className="px-3 py-2 text-sm text-right text-gray-400">{item.prev_bookings}</td>
        <td className="px-3 py-2 text-right">
          {item.growth_pct != null ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-100 text-emerald-700 text-xs font-bold">
              ▲ {item.growth_pct}%
            </span>
          ) : (
            <span className="text-xs text-gray-400">New</span>
          )}
        </td>
        <td className="px-3 py-2">
          {!item.kol_gap ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-green-100 text-green-700 text-xs font-medium">
              ✓ {item.kol_coverage.length} KOL{item.kol_coverage.length > 1 ? "s" : ""}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-100 text-red-600 text-xs font-medium">
              ✕ No KOL
            </span>
          )}
        </td>
        <td className="px-3 py-2">
          {item.ads_status === "running" ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-green-100 text-green-700 text-xs font-medium">
              ● Running
            </span>
          ) : item.ads_status === "stopped" ? (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-100 text-amber-600 text-xs font-medium">
              ○ Stopped
            </span>
          ) : (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-red-100 text-red-600 text-xs font-medium">
              ✕ No Ads
            </span>
          )}
        </td>
        <td className="px-3 py-2 text-gray-400 text-xs">{expanded ? "▲" : "▼"}</td>
      </tr>
      {expanded && (
        <tr className="bg-gray-50 border-b">
          <td colSpan={8} className="px-6 py-3">
            <CoverageDetail item={item} />
          </td>
        </tr>
      )}
    </>
  );
}

/* Map country name → ISO-2 code for holiday lookup */
function nameToCode(name) {
  if (!name) return null;
  const n = name.trim().toLowerCase();
  return NAME_TO_2[n] || (n.length === 2 ? n.toUpperCase() : null);
}

/* Holiday tag for country rows */
function HolidayTag({ countryName, holidays }) {
  if (!holidays || !holidays.length) return null;
  const code = nameToCode(countryName);
  if (!code) return null;
  const match = holidays.find(h => h.country_code === code && h.duration_days >= 4);
  if (!match) return null;
  return (
    <span className="px-1.5 py-0.5 rounded bg-orange-100 text-orange-700 text-[10px] font-medium"
      title={`${match.holiday_name} — ${match.duration_days} days off, starts in ${match.days_until}d`}>
      {match.holiday_name} ({match.duration_days}d off)
    </span>
  );
}

function ForecastTable({ forecast, holidays }) {
  const countries = forecast?.countries || [];
  const allGov = forecast?.all_gov_countries || [];
  const hasMatches = countries.length > 0;

  if (allGov.length === 0 && countries.length === 0) {
    return (
      <div className="px-5 py-3 text-xs text-gray-400">
        No government visitor data available. Import data in Admin → Gov Data.
      </div>
    );
  }

  if (!hasMatches) {
    return (
      <div className="px-5 py-4">
        <div className="flex items-center gap-2 px-3 py-2 bg-amber-50 rounded-lg border border-amber-200 mb-3">
          <span className="text-amber-500 text-sm">⚠</span>
          <span className="text-xs text-amber-700">
            No overlap between gov visitor forecast and your top booking countries for this month.
            Below is the full gov visitor ranking for reference.
          </span>
        </div>
        <GovOnlyTable countries={allGov} monthName={forecast?.month_name} holidays={holidays} />
      </div>
    );
  }

  const maxVisitors = Math.max(...countries.map((c) => c.visitor_count), 1);
  return (
    <div>
      {/* Matched countries — combined view */}
      <div className="px-5 py-2 bg-green-50 border-b border-green-100">
        <span className="text-xs font-semibold text-green-700 uppercase tracking-wide">
          Countries to focus — matched gov forecast + top bookings ({countries.length} countries)
        </span>
      </div>
      <table className="w-full">
        <thead>
          <tr className="text-xs text-gray-500 bg-gray-50 border-b">
            <th className="px-3 py-2 text-left w-8">#</th>
            <th className="px-3 py-2 text-left">Country</th>
            <th className="px-3 py-2 text-right">Gov Visitors ({forecast?.month_name})</th>
            <th className="px-3 py-2 text-left w-36">Visitor Volume</th>
            <th className="px-3 py-2 text-right">Bookings ({forecast?.month_name})</th>
            <th className="px-3 py-2 text-right">Avg Lead Time</th>
            <th className="px-3 py-2 text-left">Why Target</th>
          </tr>
        </thead>
        <tbody>
          {countries.map((c, i) => (
            <tr key={c.source_country} className="border-b hover:bg-gray-50">
              <td className="px-3 py-2 text-xs text-gray-400">{i + 1}</td>
              <td className="px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="text-base">{FLAG(c.source_country)}</span>
                  <span className="text-sm font-medium text-gray-800">{c.source_country}</span>
                  <span className="px-1.5 py-0.5 rounded bg-blue-100 text-blue-600 text-[10px] font-medium">
                    Gov #{c.gov_rank}
                  </span>
                  <span className="px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-600 text-[10px] font-medium">
                    Booking #{c.booking_rank}
                  </span>
                  <HolidayTag countryName={c.source_country} holidays={holidays} />
                </div>
              </td>
              <td className="px-3 py-2 text-sm text-right font-mono font-bold text-gray-800">
                {fmt(c.visitor_count)}
              </td>
              <td className="px-3 py-2">
                <div className="w-full bg-gray-100 rounded-full h-2">
                  <div
                    className="bg-violet-500 h-2 rounded-full"
                    style={{ width: `${(c.visitor_count / maxVisitors) * 100}%` }}
                  />
                </div>
              </td>
              <td className="px-3 py-2 text-sm text-right font-mono font-bold text-indigo-700">
                {fmt(c.booking_count)}
              </td>
              <td className="px-3 py-2 text-sm text-right font-mono text-gray-700">
                {c.avg_lead_days != null ? (
                  <span>{c.avg_lead_days}d</span>
                ) : (
                  <span className="text-gray-300">—</span>
                )}
              </td>
              <td className="px-3 py-2">
                <div className="flex flex-col gap-0.5">
                  {(c.reasons || []).map((r, ri) => (
                    <span key={ri} className="text-[10px] text-gray-500 leading-tight">{r}</span>
                  ))}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* Show remaining gov countries that didn't match */}
      {allGov.length > countries.length && (
        <div className="mt-2">
          <div className="px-5 py-2 bg-gray-50 border-y border-gray-200">
            <span className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
              Other gov source markets (not in your top 5 bookings)
            </span>
          </div>
          <GovOnlyTable
            countries={allGov.filter(
              (g) => !countries.some((m) =>
                g.source_country.toLowerCase() === m.source_country.toLowerCase()
              )
            )}
            monthName={forecast?.month_name}
            holidays={holidays}
          />
        </div>
      )}
    </div>
  );
}

function GovOnlyTable({ countries, monthName, holidays }) {
  if (!countries || countries.length === 0) return null;
  const maxV = Math.max(...countries.map((c) => c.visitor_count), 1);
  return (
    <table className="w-full">
      <thead>
        <tr className="text-xs text-gray-500 bg-gray-50 border-b">
          <th className="px-3 py-2 text-left w-8">#</th>
          <th className="px-3 py-2 text-left">Source Country</th>
          <th className="px-3 py-2 text-right">Visitors ({monthName})</th>
          <th className="px-3 py-2 text-left w-48">Volume</th>
          <th className="px-3 py-2 text-right">Yearly Total</th>
        </tr>
      </thead>
      <tbody>
        {countries.map((c, i) => (
          <tr key={c.source_country} className="border-b hover:bg-gray-50">
            <td className="px-3 py-2 text-xs text-gray-400">{i + 1}</td>
            <td className="px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="text-base">{FLAG(c.source_country)}</span>
                <span className="text-sm font-medium text-gray-800">{c.source_country}</span>
                {c.gov_rank && (
                  <span className="px-1.5 py-0.5 rounded bg-blue-100 text-blue-600 text-[10px] font-medium">
                    Gov #{c.gov_rank}
                  </span>
                )}
                <HolidayTag countryName={c.source_country} holidays={holidays} />
              </div>
            </td>
            <td className="px-3 py-2 text-sm text-right font-mono font-bold text-gray-800">
              {fmt(c.visitor_count)}
            </td>
            <td className="px-3 py-2">
              <div className="w-full bg-gray-100 rounded-full h-2">
                <div
                  className="bg-gray-400 h-2 rounded-full"
                  style={{ width: `${(c.visitor_count / maxV) * 100}%` }}
                />
              </div>
            </td>
            <td className="px-3 py-2 text-xs text-right font-mono text-gray-500">
              {fmt(c.yearly_total)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const TABS = [
  { key: "current", label: "Current Performance" },
  { key: "next_month", label: "Next Month" },
  { key: "next_2_months", label: "Next 2 Months" },
];

function BranchSection({ branch, holidays }) {
  const [collapsed, setCollapsed] = useState(false);
  const [tab, setTab] = useState("current");
  const allItems = [...(branch.top_volume || []), ...(branch.top_growth || [])];
  const kolGaps = allItems.filter((c) => c.kol_gap).length;
  const adsGaps = allItems.filter((c) => c.ads_gap).length;
  const forecast = branch.gov_forecast || {};

  return (
    <div className="bg-white rounded-lg border border-gray-200 overflow-hidden mb-4">
      <div
        className="flex items-center justify-between px-5 py-3 bg-gray-50 border-b cursor-pointer"
        onClick={() => setCollapsed((v) => !v)}
      >
        <div className="flex items-center gap-3">
          <span className="font-semibold text-gray-800">{branch.branch_name}</span>
          <span className="text-xs text-gray-400">{branch.currency}</span>
        </div>
        <div className="flex items-center gap-3">
          {kolGaps > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-red-100 text-red-600 text-xs">{kolGaps} KOL gaps</span>
          )}
          {adsGaps > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-red-100 text-red-600 text-xs">{adsGaps} Ads gaps</span>
          )}
          {kolGaps === 0 && adsGaps === 0 && allItems.length > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-green-100 text-green-600 text-xs">Fully covered</span>
          )}
          <span className="text-gray-400 text-xs">{collapsed ? "▶" : "▼"}</span>
        </div>
      </div>

      {!collapsed && (
        <>
          {/* Tab bar */}
          <div className="flex border-b">
            {TABS.map((t) => {
              const isActive = tab === t.key;
              let label = t.label;
              if (t.key === "next_month" && forecast.next_month?.month_name) {
                label = `Next Month (${forecast.next_month.month_name})`;
              } else if (t.key === "next_2_months" && forecast.next_2_months?.month_name) {
                label = `+2 Months (${forecast.next_2_months.month_name})`;
              }
              return (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className={`px-4 py-2 text-xs font-semibold transition-colors ${
                    isActive
                      ? "text-indigo-700 border-b-2 border-indigo-600 bg-white"
                      : "text-gray-400 hover:text-gray-600 hover:bg-gray-50"
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </div>

          {/* Tab content */}
          {tab === "current" && (
            <div className="divide-y divide-gray-100">
              {/* ── Top by Volume ── */}
              {branch.top_volume?.length > 0 && (
                <div>
                  <div className="px-5 py-2 bg-indigo-50 border-b border-indigo-100">
                    <span className="text-xs font-semibold text-indigo-700 uppercase tracking-wide">
                      Top 5 · Most Bookings (last 30 days)
                    </span>
                  </div>
                  <table className="w-full">
                    <thead>
                      <tr className="text-xs text-gray-500 bg-gray-50 border-b">
                        <th className="px-3 py-2 text-left w-8">#</th>
                        <th className="px-3 py-2 text-left">Country</th>
                        <th className="px-3 py-2 text-right">Bookings</th>
                        <th className="px-3 py-2 text-left">KOL</th>
                        <th className="px-3 py-2 text-left">Paid Ads</th>
                        <th className="px-3 py-2 text-left">Actions</th>
                        <th className="px-3 py-2 w-6"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {branch.top_volume.map((c) => (
                        <VolumeRow key={c.country} item={c} />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {/* ── Top by Growth ── */}
              <div>
                <div className="px-5 py-2 bg-emerald-50 border-b border-emerald-100">
                  <span className="text-xs font-semibold text-emerald-700 uppercase tracking-wide">
                    Top 5 · Fastest Growing (last 30 days vs prior 30 days)
                  </span>
                </div>
                {branch.top_growth?.length > 0 ? (
                  <table className="w-full">
                    <thead>
                      <tr className="text-xs text-gray-500 bg-gray-50 border-b">
                        <th className="px-3 py-2 text-left w-8">#</th>
                        <th className="px-3 py-2 text-left">Country</th>
                        <th className="px-3 py-2 text-right">Recent (30d)</th>
                        <th className="px-3 py-2 text-right">Prior (30d)</th>
                        <th className="px-3 py-2 text-right">Growth</th>
                        <th className="px-3 py-2 text-left">KOL</th>
                        <th className="px-3 py-2 text-left">Paid Ads</th>
                        <th className="px-3 py-2 w-6"></th>
                      </tr>
                    </thead>
                    <tbody>
                      {branch.top_growth.map((c) => (
                        <GrowthRow key={c.country} item={c} />
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="px-5 py-3 text-xs text-gray-400">
                    No clear growth trends in the last 30 days.
                  </div>
                )}
              </div>
            </div>
          )}

          {tab === "next_month" && (
            <div>
              <div className="px-5 py-2 bg-violet-50 border-b border-violet-100">
                <span className="text-xs font-semibold text-violet-700 uppercase tracking-wide">
                  {forecast.next_month?.month_name || "Next Month"} — Gov Forecast + Top Bookings
                </span>
              </div>
              <ForecastTable forecast={forecast.next_month} holidays={holidays} />
            </div>
          )}

          {tab === "next_2_months" && (
            <div>
              <div className="px-5 py-2 bg-violet-50 border-b border-violet-100">
                <span className="text-xs font-semibold text-violet-700 uppercase tracking-wide">
                  {forecast.next_2_months?.month_name || "+2 Months"} — Gov Forecast + Top Bookings
                </span>
              </div>
              <ForecastTable forecast={forecast.next_2_months} holidays={holidays} />
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function CountryIntel() {
  const { selected, isAll } = useBranch();
  const [channel, setChannel] = useState("");

  const { data = [], isPending, isError, error: queryError, isPlaceholderData } = useQuery({
    queryKey: ["country-intel", selected, isAll],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (!isAll) params.set("branch_id", selected);
      const r = await fetch(`/api/insights/country-intel?${params}`);
      const j = await r.json();
      if (!j.success) throw new Error(j.error || "Failed to load");
      return j.data;
    },
    placeholderData: keepPreviousData,
  });

  const { data: allHolidays = [], isPlaceholderData: isPlaceholderDataHolidays } = useQuery({
    queryKey: ["upcoming-holidays"],
    queryFn: () => getUpcomingWindows().then((d) => d || []),
    placeholderData: keepPreviousData,
  });

  const holidayAlerts = allHolidays.slice(0, 5);

  const allItems = data.flatMap((b) => [
    ...(b.top_volume || []),
    ...(b.top_growth || []),
  ]);
  const totalKolGaps = allItems.filter((c) => c.kol_gap).length;
  const totalAdsGaps = allItems.filter((c) => c.ads_gap).length;
  const totalActions = allItems.reduce((s, c) => s + c.action_items.length, 0);
  const fullyCovered = allItems.filter((c) => !c.kol_gap && !c.ads_gap).length;

  return (
    <div className={"space-y-5 transition-opacity duration-150 " + (isPlaceholderData || isPlaceholderDataHolidays ? "opacity-40 pointer-events-none" : "")}>
      <div>
        <h1 className="text-xl font-bold text-gray-900">Country Intelligence</h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Top guest countries × KOL coverage × Paid Ads coverage
        </p>
      </div>

      {/* Upcoming Holiday Alerts */}
      {holidayAlerts.length > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-semibold text-amber-800">Upcoming Holidays</h3>
            <Link to="/holiday-intel" className="text-xs text-indigo-600 hover:underline">Full calendar</Link>
          </div>
          <div className="flex flex-wrap gap-2">
            {holidayAlerts.map((a, i) => (
              <span key={i} className="inline-flex items-center gap-1 bg-white border border-amber-200 rounded px-2 py-1 text-xs">
                <span className="font-medium text-gray-700">{a.country_name}</span>
                <span className="text-gray-400">—</span>
                <span className="text-gray-600">{a.holiday_name}</span>
                <span className={`font-bold ml-1 ${a.days_until <= 14 ? "text-red-600" : a.days_until <= 30 ? "text-orange-600" : "text-gray-500"}`}>
                  {a.days_until}d
                </span>
              </span>
            ))}
          </div>
        </div>
      )}

      {data.length > 0 && (
        <div className="grid grid-cols-4 gap-3">
          <div className="bg-white rounded-lg border p-4">
            <div className="text-2xl font-bold text-green-600">{fullyCovered}</div>
            <div className="text-xs text-gray-500 mt-1">Fully Covered</div>
          </div>
          <div className="bg-white rounded-lg border p-4">
            <div className="text-2xl font-bold text-red-500">{totalKolGaps}</div>
            <div className="text-xs text-gray-500 mt-1">KOL Gaps</div>
          </div>
          <div className="bg-white rounded-lg border p-4">
            <div className="text-2xl font-bold text-red-500">{totalAdsGaps}</div>
            <div className="text-xs text-gray-500 mt-1">Ads Gaps</div>
          </div>
          <div className="bg-white rounded-lg border p-4">
            <div className={`text-2xl font-bold ${totalActions > 0 ? "text-orange-500" : "text-gray-400"}`}>
              {totalActions}
            </div>
            <div className="text-xs text-gray-500 mt-1">Actions Needed</div>
          </div>
        </div>
      )}

      {isPending && !data.length && (
        <div className="flex items-center justify-center h-40 text-gray-400 text-sm">Loading…</div>
      )}
      {isError && (
        <div className="bg-red-50 border border-red-200 rounded p-4 text-red-600 text-sm">{queryError?.message}</div>
      )}
      {!isPending && !isError && data.length === 0 && (
        <div className="text-center py-16 text-gray-400">No reservation data found.</div>
      )}
      {!isError && data.map((branch) => (
        <BranchSection key={branch.branch_id} branch={branch} holidays={allHolidays} />
      ))}
    </div>
  );
}
