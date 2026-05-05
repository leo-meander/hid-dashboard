import { Routes, Route, Navigate } from "react-router-dom";
import { BranchProvider } from "./context/BranchContext";
import { AuthProvider, useAuth } from "./context/AuthContext";
import Sidebar from "./components/Sidebar";
import BranchSelector from "./components/BranchSelector";
import Login from "./pages/Login";

// Phase 1 pages
import Dashboard      from "./pages/Dashboard";
import Reservations   from "./pages/Reservations";
import KPITargets     from "./pages/KPITargets";

// Phase 2 pages
import Home              from "./pages/Home";
import KPI               from "./pages/KPI";
import Performance       from "./pages/Performance";
import PerformanceDaily  from "./pages/PerformanceDaily";
import PerformanceWeekly from "./pages/PerformanceWeekly";
import PerformanceMonthly from "./pages/PerformanceMonthly";
import PerformanceOTA    from "./pages/PerformanceOTA";
import PerformanceCountry from "./pages/PerformanceCountry";
import Countries         from "./pages/Countries";
import CountryDetail     from "./pages/CountryDetail";

// Phase 3 pages
import CountryIntel from "./pages/CountryIntel";
import Marketing from "./pages/Marketing";
import Report    from "./pages/Report";
import Settings  from "./pages/Settings";
import Users     from "./pages/Users";

// Marketing Activity (consolidated)
import MarketingActivity from "./pages/MarketingActivity";

// Budget Planner — monthly allocation + actual spend per channel
import BudgetPlanner from "./pages/BudgetPlanner";

// Email Marketing (GHL)
import EmailMarketing from "./pages/EmailMarketing";

// Government Visitor Data
import GovVisitorData from "./pages/GovVisitorData";

// Phase 5 — Holiday Intelligence
import HolidayIntel from "./pages/HolidayIntel";

// Alert System
import Alerts from "./pages/Alerts";

// Rate Plan Quotas
import RatePlanQuotas from "./pages/RatePlanQuotas";

export default function App() {
  return (
    <AuthProvider>
      <BranchProvider>
        <AppRoutes />
      </BranchProvider>
    </AuthProvider>
  );
}

function AppRoutes() {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <div className="text-gray-500 text-sm animate-pulse">Loading...</div>
      </div>
    );
  }

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="*"      element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <div className="flex h-screen bg-gray-50">
      <Sidebar />
      <div className="flex-1 flex flex-col overflow-hidden">
        <BranchSelector />
        <main className="flex-1 overflow-auto p-6">
          <Routes>
            <Route path="/" element={<Navigate to="/home" replace />} />
            <Route path="/login" element={<Navigate to="/home" replace />} />

            {/* Phase 2 */}
            <Route path="/home"                  element={<Home />} />
            <Route path="/kpi"                   element={<KPI />} />
            <Route path="/performance"           element={<Performance />} />
            <Route path="/performance/daily"     element={<PerformanceDaily />} />
            <Route path="/performance/weekly"    element={<PerformanceWeekly />} />
            <Route path="/performance/monthly"   element={<PerformanceMonthly />} />
            <Route path="/performance/ota"       element={<PerformanceOTA />} />
            <Route path="/performance/countries" element={<PerformanceCountry />} />
            <Route path="/countries"             element={<Countries />} />
            <Route path="/countries/:code"       element={<CountryDetail />} />

            {/* Phase 3 — Marketing */}
            <Route path="/country-intel" element={<CountryIntel />} />
            <Route path="/marketing" element={<Marketing />} />
            <Route path="/report"    element={<Report />} />

            {/* Marketing Activity (consolidated) */}
            <Route path="/marketing-activity" element={<MarketingActivity />} />

            {/* Budget Planner */}
            <Route path="/budget-planner" element={<BudgetPlanner />} />

            {/* Email Marketing (GHL) */}
            <Route path="/email-marketing" element={<EmailMarketing />} />

            {/* Phase 5 — Holiday Intelligence */}
            <Route path="/holiday-intel" element={<HolidayIntel />} />

            {/* Alert System */}
            <Route path="/alerts" element={<Alerts />} />

            {/* Rate Plan Quotas */}
            <Route path="/rate-plan-quotas" element={<RatePlanQuotas />} />

            {/* Settings & Admin */}
            <Route path="/settings" element={<Settings />} />
            <Route path="/users"    element={<Users />} />
            <Route path="/gov-data" element={<GovVisitorData />} />

            {/* Phase 1 legacy */}
            <Route path="/dashboard"    element={<Dashboard />} />
            <Route path="/reservations" element={<Reservations />} />
            <Route path="/kpi-targets"  element={<KPITargets />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
