import axios from "axios";
const BASE = "/api/marketing-activity";

export const getMarketingActivitySummary = (params = {}) =>
  axios.get(`${BASE}/summary`, { params }).then(r => r.data.data);

export const getCRMBranchComparison = (params = {}) =>
  axios.get(`${BASE}/crm-branch-comparison`, { params }).then(r => r.data.data);
