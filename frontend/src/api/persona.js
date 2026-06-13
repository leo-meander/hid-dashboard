import axios from "axios";

const BASE = "/api/personas";

// params: { branch_id?, months? }  → { window, personas: [...], data_synced_at }
export const getPersonas = (params = {}) =>
  axios.get(BASE, { params }).then((r) => r.data.data);
