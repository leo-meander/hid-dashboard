/**
 * Page-group access control.
 *
 * Permissions are granted per sidebar group (not per individual page).
 * A user's `allowed_pages` holds a subset of these keys; an empty array
 * means "all groups" (full access). The `admin` group is gated by role
 * (isAdmin), NOT by allowed_pages, so it is intentionally excluded here.
 */

// Groups an admin can assign to a user, in sidebar order.
export const PAGE_GROUPS = [
  { key: "overview",    label: "Overview",    hint: "Home" },
  { key: "performance", label: "Performance", hint: "Summary, Daily, Weekly, Monthly, OTA, Countries" },
  { key: "strategy",    label: "Strategy",    hint: "KPI, Targets, Country Intel, Holiday Intel" },
  { key: "marketing",   label: "Marketing",   hint: "Marketing Activity, Budget Planner, Email" },
  { key: "reports",     label: "Reports",     hint: "Alerts, Rate Plan Quotas, Weekly Report" },
];

const PAGE_GROUP_KEYS = PAGE_GROUPS.map((g) => g.key);

// Route prefix → group key. Longest matching prefix wins.
// Routes under the `admin` group are role-gated, not page-gated.
const ROUTE_GROUPS = [
  ["/home",               "overview"],
  ["/dashboard",          "overview"],
  ["/performance",        "performance"],
  ["/countries",          "performance"],
  ["/reservations",       "performance"],
  ["/kpi-targets",        "strategy"],
  ["/kpi",                "strategy"],
  ["/country-intel",      "strategy"],
  ["/holiday-intel",      "strategy"],
  ["/marketing-activity", "marketing"],
  ["/budget-planner",     "marketing"],
  ["/email-marketing",    "marketing"],
  ["/alerts",             "reports"],
  ["/rate-plan-quotas",   "reports"],
  ["/report",             "reports"],
  ["/marketing",          "admin"],
  ["/settings",           "admin"],
  ["/users",              "admin"],
  ["/gov-data",           "admin"],
];

/** Resolve which group a pathname belongs to (or null if unmapped). */
export function pageGroupForPath(pathname) {
  let best = null;
  let bestLen = -1;
  for (const [prefix, key] of ROUTE_GROUPS) {
    if (pathname === prefix || pathname.startsWith(prefix + "/")) {
      if (prefix.length > bestLen) {
        best = key;
        bestLen = prefix.length;
      }
    }
  }
  return best;
}

/** Whether the access list grants a given group. Empty list = all. */
export function groupAllowed(allowedPages, key) {
  if (!Array.isArray(allowedPages) || allowedPages.length === 0) return true;
  return allowedPages.includes(key);
}

/** First sidebar path the user is allowed to open — used as redirect target. */
export function firstAllowedPath(allowedPages) {
  const firstKey = PAGE_GROUP_KEYS.find((k) => groupAllowed(allowedPages, k));
  // Map the group key back to a concrete landing route.
  const landing = {
    overview:    "/home",
    performance: "/performance",
    strategy:    "/kpi",
    marketing:   "/marketing-activity",
    reports:     "/report",
  };
  return landing[firstKey] || "/home";
}
