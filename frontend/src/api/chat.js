import axios from "axios";
const BASE = "/api/chat";

export const sendChatMessage = ({ message, history = [], branch_id = null }) =>
  axios.post(BASE, { message, history, branch_id }).then(r => r.data.data);

export const getChatHealth = () =>
  axios.get(`${BASE}/health`).then(r => r.data.data);
