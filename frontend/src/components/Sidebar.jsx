import { useState, useEffect } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { getAlertsSummary } from "../api/alerts";

const NAV = [
  {
    label: "Overview",
    items: [
      { to: "/home", label: "Home", icon: "⊞" },
    ],
  },
  {
    label: "Performance",
    items: [
      { to: "/performance",         label: "Summary",  icon: "▦" },
      { to: "/performance/daily",   label: "Daily",    icon: "▤" },
      { to: "/performance/weekly",  label: "Weekly",   icon: "▥" },
      { to: "/performance/monthly", label: "Monthly",  icon: "▧" },
      { to: "/performance/ota",       label: "OTA Mix",    icon: "◈" },
      { to: "/performance/countries", label: "Countries",  icon: "◍" },
    ],
  },
  {
    label: "Strategy",
    items: [
      { to: "/kpi",           label: "KPI Dashboard", icon: "◎" },
      { to: "/kpi-targets",   label: "Set Targets",   icon: "◌" },
      { to: "/country-intel", label: "Country Intel", icon: "◍" },
      { to: "/holiday-intel", label: "Holiday Intel", icon: "◑" },
    ],
  },
  {
    label: "Marketing",
    items: [
      { to: "/marketing-activity", label: "Marketing Activity", icon: "◈" },
      { to: "/email-marketing", label: "Email Marketing", icon: "✉" },
    ],
  },
  {
    label: "Reports",
    items: [
      { to: "/alerts", label: "Alerts", icon: "▲", badge: true },
      { to: "/report", label: "Weekly Report", icon: "◻" },
    ],
  },
];

export default function Sidebar() {
  const location = useLocation();
  const { user, logout, isAdmin } = useAuth();
  const [alertCount, setAlertCount] = useState(0);

  useEffect(() => {
    const fetchCount = () => {
      getAlertsSummary()
        .then(d => setAlertCount(d?.critical || 0))
        .catch(() => {});
    };
    fetchCount();
    const interval = setInterval(fetchCount, 5 * 60 * 1000); // refresh every 5 min
    return () => clearInterval(interval);
  }, []);

  return (
    <aside className="w-52 bg-gray-900 text-white flex flex-col shrink-0">
      {/* Brand */}
      <div className="px-5 py-4 border-b border-gray-700">
        <span className="text-base font-bold tracking-wide">HiD</span>
        <p className="text-xs text-gray-400 mt-0.5">Hotel Intelligence</p>
      </div>

      {/* Nav groups */}
      <nav className="flex-1 px-2 py-3 space-y-3 overflow-y-auto">
        {NAV.map(({ label, items }) => (
          <div key={label}>
            <p className="px-3 mb-1 text-xs font-semibold uppercase tracking-wider text-gray-500">
              {label}
            </p>
            <div className="space-y-0.5">
              {items.map(({ to, label: itemLabel, icon, badge }) => {
                const isPerformanceParent =
                  to === "/performance" && location.pathname.startsWith("/performance");
                return (
                  <NavLink
                    key={to}
                    to={to}
                    end={to === "/performance"}
                    className={({ isActive }) =>
                      `flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors ${
                        isActive || isPerformanceParent
                          ? "bg-indigo-600 text-white"
                          : "text-gray-400 hover:bg-gray-700 hover:text-white"
                      }`
                    }
                  >
                    <span className="text-xs w-3 text-center">{icon}</span>
                    {itemLabel}
                    {badge && alertCount > 0 && (
                      <span className="ml-auto px-1.5 py-0.5 text-xs font-bold bg-red-600 text-white rounded-full leading-none">
                        {alertCount}
                      </span>
                    )}
                  </NavLink>
                );
              })}
            </div>
          </div>
        ))}

        {/* Admin section — only for admins */}
        {isAdmin && (
          <div>
            <p className="px-3 mb-1 text-xs font-semibold uppercase tracking-wider text-gray-500">Admin</p>
            <div className="space-y-0.5">
              <NavLink to="/marketing"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors ${
                    isActive ? "bg-indigo-600 text-white" : "text-gray-400 hover:bg-gray-700 hover:text-white"
                  }`}>
                <span className="text-xs w-3 text-center">◆</span> Activity Log
              </NavLink>
              <NavLink to="/settings"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors ${
                    isActive ? "bg-indigo-600 text-white" : "text-gray-400 hover:bg-gray-700 hover:text-white"
                  }`}>
                <span className="text-xs w-3 text-center">⚙</span> Settings
              </NavLink>
              <NavLink to="/users"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors ${
                    isActive ? "bg-indigo-600 text-white" : "text-gray-400 hover:bg-gray-700 hover:text-white"
                  }`}>
                <span className="text-xs w-3 text-center">◎</span> Users
              </NavLink>
              <NavLink to="/gov-data"
                className={({ isActive }) =>
                  `flex items-center gap-2 px-3 py-1.5 rounded text-sm transition-colors ${
                    isActive ? "bg-indigo-600 text-white" : "text-gray-400 hover:bg-gray-700 hover:text-white"
                  }`}>
                <span className="text-xs w-3 text-center">◐</span> Gov Data
              </NavLink>
            </div>
          </div>
        )}
      </nav>

      {/* User + logout */}
      <div className="px-4 py-3 border-t border-gray-700">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-7 h-7 rounded-full bg-indigo-600 flex items-center justify-center text-xs font-bold text-white flex-shrink-0">
            {(user?.name || user?.email || "?")[0].toUpperCase()}
          </div>
          <div className="min-w-0">
            <p className="text-xs font-medium text-gray-200 truncate">{user?.name || user?.email}</p>
            <p className="text-xs text-gray-500 capitalize">{user?.role}</p>
          </div>
        </div>
        <button onClick={logout}
          className="w-full text-xs text-gray-500 hover:text-red-400 text-left transition-colors py-0.5">
          Sign out →
        </button>
      </div>
    </aside>
  );
}
