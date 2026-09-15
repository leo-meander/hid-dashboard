import axios from "axios";
const BASE = "/api/marketing-activity";

export const getMarketingActivitySummary = (params = {}) =>
  axios.get(`${BASE}/summary`, { params }).then(r => r.data.data);

export const getCRMBranchComparison = (params = {}) =>
  axios.get(`${BASE}/crm-branch-comparison`, { params }).then(r => r.data.data);

// One rate plan's guests — status mix, countries, demographics, stay shape.
// Takes the same window/branch params as the summary so the drill-down and the
// row it opened describe the same reservations.
export const getCRMRatePlanDetail = (params = {}) =>
  axios.get(`${BASE}/crm-rate-plan-detail`, { params }).then(r => r.data.data);

// Hand-typed rate plan → campaign labels, shown next to Rate Plan Name on the
// CRM Reservations tab. Returns a flat { [rate_plan_name]: campaign_name } map.
export const getRatePlanCampaigns = () =>
  axios.get(`${BASE}/rate-plan-campaigns`).then(r => r.data.data);

// Empty campaign_name clears the label.
export const saveRatePlanCampaign = (rate_plan_name, campaign_name) =>
  axios.put(`${BASE}/rate-plan-campaigns`, { rate_plan_name, campaign_name })
    .then(r => r.data.data);

// Seasonal campaigns — the definitions the set-up dialog edits (ad campaign
// names + rate plan names + cost %), separate from the computed performance
// table so editing one campaign doesn't refetch every campaign's numbers.
export const getSeasonalCampaigns = () =>
  axios.get(`${BASE}/seasonal-campaigns`).then(r => r.data.data);

export const getSeasonalCampaignPerformance = (params = {}) =>
  axios.get(`${BASE}/seasonal-campaigns/performance`, { params })
    .then(r => r.data.data);

// Campaign x branch matrix, always VND so the five branches are comparable.
export const getSeasonalBranchComparison = (params = {}) =>
  axios.get(`${BASE}/seasonal-campaigns/branch-comparison`, { params })
    .then(r => r.data.data);

export const createSeasonalCampaign = (body) =>
  axios.post(`${BASE}/seasonal-campaigns`, body).then(r => r.data.data);

export const updateSeasonalCampaign = (id, body) =>
  axios.patch(`${BASE}/seasonal-campaigns/${id}`, body).then(r => r.data.data);

export const deleteSeasonalCampaign = (id) =>
  axios.delete(`${BASE}/seasonal-campaigns/${id}`).then(r => r.data.data);
