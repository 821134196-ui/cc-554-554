/* 公共工具：API 封装、格式化、标签 */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* 空响应 */ }
  if (!res.ok) {
    const msg = (data.detail && data.detail.message) || data.detail || `请求失败(${res.status})`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body || {}) });

const fmtSlot = (s) => (s ? `${s.date} ${s.start_time}-${s.end_time}` : "-");

const TICKET_STATUS = {
  active: ["有效", "green"],
  used: ["已过闸", "gray"],
  refund_pending: ["退款中", "orange"],
  refunded: ["已退款", "red"],
  waitlisted: ["候补中", "blue"],
};
const SLOT_STATUS = { normal: ["正常", "green"], suspended: ["停运", "red"] };
const ACTION_TYPE = { refund: "退款", rebook: "换时段", waitlist: "候补" };
const ACTION_STATUS = {
  pending: ["处理中", "orange"],
  confirmed: ["已生效", "green"],
  rejected: ["已驳回", "red"],
  cancelled: ["已取消", "gray"],
};
const REASON_LABEL = { weather: "天气原因", equipment: "设备原因" };

function newKey() {
  return (crypto.randomUUID && crypto.randomUUID()) ||
    "k-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

/* 简易 toast */
let toastTimer = null;
function toast(msg, ok = true) {
  let el = document.getElementById("global-toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "global-toast";
    document.body.appendChild(el);
  }
  el.className = "toast " + (ok ? "ok" : "err");
  el.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 3200);
}
