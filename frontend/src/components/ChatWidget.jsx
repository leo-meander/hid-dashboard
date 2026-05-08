import { useEffect, useRef, useState } from "react";
import { useBranch } from "../context/BranchContext";
import { sendChatMessage, getChatHealth } from "../api/chat";

const STORAGE_KEY = "hid_chat_history";
const MAX_HISTORY = 30;

const SUGGESTIONS = [
  "How is OCC this week?",
  "Is this month's KPI on track?",
  "Top 5 source countries last month",
  "Channel mix for the past 30 days",
  "Any active alerts right now?",
  "What upcoming holidays should we prep for?",
];

function loadHistory() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.slice(-MAX_HISTORY) : [];
  } catch {
    return [];
  }
}

function saveHistory(messages) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(messages.slice(-MAX_HISTORY)));
  } catch {}
}

// Tiny markdown renderer — bold, lists, headings, code spans. Keeps it light
// so we don't drag in a markdown lib for a chat bubble.
function renderMarkdown(text) {
  if (!text) return null;
  const lines = text.split("\n");
  const blocks = [];
  let listBuf = [];
  const flushList = () => {
    if (listBuf.length) {
      blocks.push(
        <ul key={`ul-${blocks.length}`} className="list-disc pl-5 space-y-1 my-1">
          {listBuf.map((item, i) => (
            <li key={i} dangerouslySetInnerHTML={{ __html: inlineMd(item) }} />
          ))}
        </ul>
      );
      listBuf = [];
    }
  };
  lines.forEach((raw, i) => {
    const line = raw.trimEnd();
    if (/^\s*[-*]\s+/.test(line)) {
      listBuf.push(line.replace(/^\s*[-*]\s+/, ""));
      return;
    }
    flushList();
    if (/^##\s+/.test(line)) {
      blocks.push(
        <h4 key={i} className="font-semibold text-gray-900 mt-2 mb-1 text-sm">
          {line.replace(/^##\s+/, "")}
        </h4>
      );
    } else if (/^#\s+/.test(line)) {
      blocks.push(
        <h3 key={i} className="font-bold text-gray-900 mt-2 mb-1">
          {line.replace(/^#\s+/, "")}
        </h3>
      );
    } else if (line.trim() === "") {
      blocks.push(<div key={i} className="h-1" />);
    } else {
      blocks.push(
        <p key={i} className="leading-relaxed" dangerouslySetInnerHTML={{ __html: inlineMd(line) }} />
      );
    }
  });
  flushList();
  return blocks;
}

function inlineMd(s) {
  // escape HTML, then apply bold + code
  let out = s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/`([^`]+)`/g, '<code class="bg-gray-100 px-1 rounded text-xs">$1</code>');
  return out;
}

export default function ChatWidget() {
  const { selected, currentBranch } = useBranch();
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState(loadHistory);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [configured, setConfigured] = useState(true);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    saveHistory(messages);
  }, [messages]);

  useEffect(() => {
    if (!open) return;
    getChatHealth()
      .then(d => setConfigured(!!d?.configured))
      .catch(() => setConfigured(false));
    setTimeout(() => inputRef.current?.focus(), 50);
  }, [open]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, loading]);

  const send = async (text) => {
    const msg = (text ?? input).trim();
    if (!msg || loading) return;
    const next = [...messages, { role: "user", content: msg, ts: Date.now() }];
    setMessages(next);
    setInput("");
    setLoading(true);
    try {
      const branchId = selected === "all" ? "all" : selected;
      // Send only role+content of prior turns (strip ts) + cap to last 12 turns
      const history = messages.slice(-12).map(m => ({ role: m.role, content: m.content }));
      const res = await sendChatMessage({ message: msg, history, branch_id: branchId });
      setMessages([
        ...next,
        {
          role: "assistant",
          content: res?.reply || "(no response)",
          tools_used: res?.tools_used || [],
          ts: Date.now(),
        },
      ]);
    } catch (e) {
      setMessages([
        ...next,
        {
          role: "assistant",
          content:
            "Chat API error: " +
            (e?.response?.data?.error || e?.message || "unknown"),
          ts: Date.now(),
          error: true,
        },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  const clear = () => {
    if (!confirm("Clear all chat history?")) return;
    setMessages([]);
    localStorage.removeItem(STORAGE_KEY);
  };

  return (
    <>
      {/* Floating launcher */}
      {!open && (
        <button
          onClick={() => setOpen(true)}
          className="fixed bottom-5 right-5 z-50 bg-indigo-600 hover:bg-indigo-700 text-white rounded-full w-14 h-14 shadow-lg flex items-center justify-center transition-all hover:scale-105"
          title="HiD Assistant"
        >
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
        </button>
      )}

      {/* Panel */}
      {open && (
        <div className="fixed bottom-5 right-5 z-50 w-[420px] max-w-[95vw] h-[640px] max-h-[85vh] bg-white border border-gray-200 rounded-xl shadow-2xl flex flex-col overflow-hidden">
          {/* Header */}
          <div className="px-4 py-3 bg-gray-900 text-white flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold">HiD Assistant</div>
              <div className="text-xs text-gray-400">
                {currentBranch ? currentBranch.name : "All branches"} · powered by Claude
              </div>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={clear}
                title="Clear history"
                className="text-gray-400 hover:text-white p-1 rounded"
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="3 6 5 6 21 6" />
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                </svg>
              </button>
              <button
                onClick={() => setOpen(false)}
                className="text-gray-400 hover:text-white p-1 rounded"
                title="Close"
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </div>
          </div>

          {/* Phase 1 banner */}
          <div className="px-3 py-1.5 bg-amber-50 border-b border-amber-200 text-[11px] text-amber-800">
            Phase 1: <strong>suggestions only</strong>. Phase 2 will be able to <strong>execute</strong> the suggested actions.
          </div>

          {/* Messages */}
          <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 py-3 space-y-3 bg-gray-50">
            {!configured && (
              <div className="text-xs bg-red-50 border border-red-200 text-red-700 rounded p-2">
                ANTHROPIC_API_KEY is not configured on the backend. Please contact your admin.
              </div>
            )}

            {messages.length === 0 && (
              <div className="space-y-3">
                <div className="text-xs text-gray-500">
                  Hi — I'm the HiD Assistant. Ask me anything about performance, KPI, ads, KOL, countries, or alerts.
                </div>
                <div className="space-y-1.5">
                  {SUGGESTIONS.map(s => (
                    <button
                      key={s}
                      onClick={() => send(s)}
                      className="w-full text-left text-xs bg-white border border-gray-200 hover:border-indigo-400 hover:bg-indigo-50 rounded-lg px-3 py-2 transition-colors"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((m, i) => (
              <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[88%] rounded-lg px-3 py-2 text-sm ${
                    m.role === "user"
                      ? "bg-indigo-600 text-white"
                      : m.error
                      ? "bg-red-50 border border-red-200 text-red-800"
                      : "bg-white border border-gray-200 text-gray-800"
                  }`}
                >
                  {m.role === "user" ? (
                    <div className="whitespace-pre-wrap">{m.content}</div>
                  ) : (
                    <>
                      <div className="space-y-1 text-[13px]">{renderMarkdown(m.content)}</div>
                      {m.tools_used?.length > 0 && (
                        <div className="mt-2 pt-2 border-t border-gray-100 flex flex-wrap gap-1">
                          {[...new Set(m.tools_used)].map(t => (
                            <span key={t} className="text-[10px] bg-gray-100 text-gray-600 px-1.5 py-0.5 rounded">
                              {t}
                            </span>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            ))}

            {loading && (
              <div className="flex justify-start">
                <div className="bg-white border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-500 flex items-center gap-2">
                  <span className="inline-block w-2 h-2 bg-indigo-500 rounded-full animate-pulse" />
                  <span className="inline-block w-2 h-2 bg-indigo-500 rounded-full animate-pulse" style={{ animationDelay: "0.15s" }} />
                  <span className="inline-block w-2 h-2 bg-indigo-500 rounded-full animate-pulse" style={{ animationDelay: "0.3s" }} />
                  <span className="text-xs ml-1">thinking...</span>
                </div>
              </div>
            )}
          </div>

          {/* Input */}
          <div className="border-t border-gray-200 p-2 bg-white">
            <div className="flex gap-2 items-end">
              <textarea
                ref={inputRef}
                value={input}
                onChange={e => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Ask about OCC, KPI, ads, KOL, alerts..."
                rows={2}
                className="flex-1 resize-none text-sm border border-gray-300 rounded-lg px-3 py-2 focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
                disabled={loading}
              />
              <button
                onClick={() => send()}
                disabled={loading || !input.trim()}
                className="bg-indigo-600 hover:bg-indigo-700 disabled:bg-gray-300 text-white rounded-lg px-3 py-2 text-sm font-medium transition-colors h-fit"
              >
                Send
              </button>
            </div>
            <div className="text-[10px] text-gray-400 mt-1 px-1">
              Enter to send · Shift+Enter for newline
            </div>
          </div>
        </div>
      )}
    </>
  );
}
