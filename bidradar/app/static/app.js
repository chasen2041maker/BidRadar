"use strict";
// BidRadar 工作台前端（原生 JavaScript，不依赖任何框架，不需要构建）。
//
// 页面分五个视图：商机目录、研究、决定与跟踪、企业档案、成员，由 showView 切换。
// 文件从上到下：
//   1. 全局状态 state 和各种中文显示名称
//   2. 通用工具：创建元素、调用接口（api / upload）、带幂等键的提交（command）、格式化
//   3. 登录、切换公司、目录和公告详情（详情里上传招标文件、预估费用、发起研究）
//   4. 企业档案、成员
//   5. 研究：额度、任务列表、轮询进度、渲染报告和追问回答
//   6. 跟踪：决定、关注、变化、提醒、自动复核
//   7. 最底部：给按钮和表单绑定事件
//
// 两条安全规则：
//   - 页面只提交 ID 和版本号；标题、权限、档案版本都由服务端重新核对，不信任页面传的值。
//   - 公告原文、企业填写的内容、模型输出一律用 textContent 显示，绝不当作 HTML 插入页面。
const $ = (id) => document.getElementById(id);
const state = {
  session: null,
  workspace: null,
  profile: null,
  budget: null,
  view: "discover",
  cursor: null,
  query: null,
  epoch: 0,
  searchVersion: 0,
  detailVersion: 0,
  runId: null,
  runVersion: 0,
  runListVersion: 0,
  runBefore: null,
  runStatuses: new Map(),
  pollTimer: null,
  trackedNotice: null,
  trackingVersion: 0,
  notificationAfter: 0,
  changeAfter: 0,
};
const roleNames = { admin: "公司管理员", member: "协作成员", viewer: "只读成员" };
const kindNames = {
  procurement: "采购公告",
  correction: "更正公告",
  award: "中标 / 成交",
  termination: "终止公告",
  intention: "采购意向",
  unknown: "类型待核实",
};
const statusNames = {
  known: "已提取",
  missing: "尚未取得依据",
  unparsed: "待核对原文",
  conflicting: "存在冲突",
  context_required: "需结合金额口径",
  unknown: "尚未确认",
  deadline_passed: "原截止时间已过",
  deadline_not_reached: "原截止时间未到",
  not_opening_notice: "非采购报名公告",
};
// 服务端只返回错误码，这里翻译成具体的中文说明。没列出的错误码按 HTTP 状态给一句通用提示。
const errors = {
  authentication_required: "登录已失效，请重新登录。",
  invalid_credentials: "账号或密码不正确。",
  login_limited: "登录尝试过多，请稍后重试。",
  forbidden: "你目前没有执行此操作的权限，请刷新公司空间。",
  csrf_failed: "登录状态已变化，请刷新页面后重试。",
  invalid_origin: "请求来源不对，请从工作台页面操作。",
  invalid_request: "提交的内容格式不对，请检查后重试。",
  not_found: "记录不存在或当前无权访问。",
  notice_not_found: "这条公告不在目录里。",
  idempotency_conflict: "相同操作标识对应了不同内容，请刷新后重新操作。",
  profile_version_conflict: "企业档案已有新版本，请重新读取后提交。",
  proposal_already_confirmed: "这条建议已经确认过了。",
  profile_required: "请先由管理员确认一版企业档案。",
  invalid_profile: "档案内容不符合要求（字段或长度），请检查后重试。",
  membership_version_conflict: "成员信息已被修改，请刷新后重试。",
  last_admin: "公司需要保留至少一位管理员。",
  decision_version_conflict: "人工决定已被修改，请刷新后重新确认。",
  watch_version_conflict: "关注设置已被修改，请刷新后重新确认。",
  invalid_watch: "关注设置不合法：开启自动复核前要先开启关注。",
  file_too_large: "文件超过 20 MB。",
  empty_file: "文件是空的。",
  unsupported_file_type: "只支持文字版的 PDF 和 Word（.docx）文件。",
  invalid_filename: "文件名不合法。",
  document_not_parsed: "所选文件还没解析完，请稍后再试。",
  invalid_documents: "所选文件不对，请刷新后重新选择。",
  workspace_queue_limit: "公司正在处理的研究任务已达上限（5 个）。请等已有任务结束，或取消不再需要的任务。",
  run_already_finished: "这个任务已经结束了。",
  model_disabled: "模型没有开启：追问需要模型。研究仍可用规则基线。",
  parent_report_unavailable: "这份报告还没完成，暂时不能追问。",
  invalid_question: "追问内容不能为空，最多 2000 字。",
};
// 研究任务停下来的原因。
const runReasons = {
  cancelled_by_user: "已由成员取消。",
  profile_version_changed: "企业档案出了新版本，这个任务的输入已经过期，没有继续花钱。请按新档案重新发起。",
  access_revoked: "发起人已经没有分析权限，任务已停止。",
  access_changed: "发起人的成员权限有变化，任务已停止。",
  billing_unknown:
    "有一次模型调用结果不确定（可能已经计费），自动执行已停止。请先核对供应商账单和用量；系统不会自动重发这次调用。",
  budget_exceeded: "达到费用上限，没有继续调用模型。已花的费用见下方。",
  model_disabled: "模型没有开启。",
  model_call_failed: "模型调用多次失败，任务已停止。",
  document_not_parsed: "有文件还没解析完。",
  reassessment_access_changed: "自动复核的委托因成员权限变化作废。",
  reassessment_watch_changed: "关注设置改了，旧的自动复核作废。",
  reassessment_input_superseded: "同一条公告又有更新的变化，旧的自动复核作废。",
};
const profileFields = [
  ["company_name", "公司名称 / 简称", "例如：山岚软件（虚构）"],
  ["city", "所在城市", "城市不限定可承接的地区"],
  ["project_types", "希望承接的项目", "软件定制、业务系统、AI应用；也可注明不接的类型"],
  ["capabilities", "技术与交付能力", "能负责哪些工作，有哪些技术与交付经验"],
  ["delivery_constraints", "交付与地域限制", "区分优先远程、可出差与明确不接受的驻场要求"],
  ["cases", "相关案例", "工作范围、团队角色、时间；尚不接收证明文件"],
  ["qualifications", "资质与证明声明", "确认有 / 没有 / 尚未确认；文字声明不是核验结果"],
  ["staffing", "人力与排期", "可投入角色、人数、最早时间与已知冲突"],
  ["commercial_constraints", "商务偏好与硬限制", "请分别写清偏好和明确不能接受的条件"],
];
const profileFieldNames = Object.fromEntries(profileFields.map(([name, title]) => [name, title]));

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function badge(text, type = "") {
  return el("span", text, "badge " + type);
}
// 代码里用到的几个图标，和 index.html 里的同一套画法（24×24 画布、描边、颜色跟随文字）。
const iconPaths = {
  plus: "M12 5.5v13M5.5 12h13",
  check: "m5.5 12.5 4.2 4.2L18.5 8",
  file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Zm0 0v5h5M9 13h6M9 17h4",
  inbox:
    "M3.5 13.5 5.8 5.4A2 2 0 0 1 7.7 4h8.6a2 2 0 0 1 1.9 1.4l2.3 8.1V18a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2Zm0 0h4.8l1.4 2.5h4.6l1.4-2.5h4.8",
  upload: "M12 15.5V4m0 0L7.5 8.5M12 4l4.5 4.5M4.5 15v3a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2v-3",
};
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "i");
  svg.setAttribute("aria-hidden", "true");
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  shape.setAttribute("d", iconPaths[name]);
  svg.append(shape);
  return svg;
}
// 头像只取名字的第一个字。Array.from 按字符而不是按 UTF-16 单元切，生僻字和表情不会被切成半个。
function initial(name) {
  return Array.from(String(name || "").trim())[0] || "?";
}
// 状态配色：成功用绿色，失败用红色，需要人处理的用橙色，其余灰色。
function runTone(status) {
  if (["succeeded", "finished", "parsed"].includes(status)) return "";
  if (["failed", "rejected"].includes(status)) return "danger";
  if (["partial", "waiting_input"].includes(status)) return "warning";
  return "neutral";
}
let noticeTimer = null;
function notice(message, failure = false) {
  $("notice").textContent = message;
  $("notice").className = "notice" + (failure ? " failure" : "");
  $("notice").hidden = false;
  // 提示浮在页面右上角，几秒后自动收起；出错的提示多停一会儿，方便看清。点一下也能提前关掉。
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => ($("notice").hidden = true), failure ? 12000 : 6000);
}
function clearPrivateView() {
  stopRunPolling();
  state.runId = null;
  state.runVersion += 1;
  state.runListVersion += 1;
  state.detailVersion += 1;
  state.trackedNotice = null;
  state.trackingVersion += 1;
  state.runStatuses.clear();
  state.runBefore = null;
  state.notificationAfter = 0;
  state.changeAfter = 0;
  state.budget = null;
  for (const id of ["more-research", "more-notifications", "more-changes"]) $(id).hidden = true;
  for (const id of [
    "profile-form",
    "proposals",
    "profile-history",
    "member-list",
    "notice-list",
    "detail-content",
    "research-list",
    "research-detail",
    "research-budget",
    "tracking-context",
    "tracking-notifications",
    "tracking-changes",
    "tracking-reassessments",
  ])
    $(id).replaceChildren();
  $("detail-dialog").close();
  $("profile-label").textContent = "正在读取";
  $("profile-version").textContent = "正在读取";
  $("source-status").textContent = "";
  $("as-of").textContent = "";
  $("total-count").textContent = "—";
}
function showLogin() {
  state.epoch += 1;
  state.session = null;
  state.workspace = null;
  state.profile = null;
  clearPrivateView();
  $("workspace-screen").hidden = true;
  $("login-screen").hidden = false;
}
function csrf() {
  return (
    document.cookie
      .split(";")
      .map((x) => x.trim())
      .find((x) => x.startsWith("br_csrf="))
      ?.slice(8) || ""
  );
}
function stale() {
  const error = new Error("界面范围已变化。");
  error.discarded = true;
  return error;
}
async function send(path, init) {
  const epoch = state.epoch;
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...init });
  } catch (_) {
    throw new Error("连接中断。尚未确认操作结果；恢复后可重试，服务端会核对重复请求。");
  }
  let value = null;
  try {
    value = await response.json();
  } catch (_) {
    value = null;
  }
  // 请求发出后如果已经切换了公司或退出登录（epoch 变了），就丢弃这个迟到的响应，免得把上一家公司的数据显示到当前公司。
  if (epoch !== state.epoch) throw stale();
  if (!response.ok) {
    const code = value?.error?.code || "request_failed";
    const error = new Error(
      errors[code] ||
        (response.status === 401
          ? "登录已失效，请重新登录。"
          : response.status === 503
            ? "服务暂时不可用，已有记录仍保留，请稍后重试。"
            : response.status === 409
              ? "记录或版本已变化，请刷新后重新核对。"
              : "操作未完成，请检查输入和当前权限。"),
    );
    error.code = code;
    error.status = response.status;
    if (response.status === 401 && path !== "/api/login") {
      const hadSession = Boolean(state.session);
      showLogin();
      if (hadSession) $("login-error").textContent = error.message;
    }
    throw error;
  }
  return value;
}
function api(path, body) {
  if (body === undefined) return send(path, { method: "GET" });
  return send(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
    body: JSON.stringify(body),
  });
}
// 上传文件用 multipart。同一份文件重复上传，服务端按内容哈希认出来，返回第一次的那份，所以重试是安全的。
function upload(path, form) {
  return send(path, { method: "POST", headers: { "X-CSRF-Token": csrf() }, body: form });
}
function path(resource) {
  return `/api/workspaces/${encodeURIComponent(state.workspace.workspace_id)}/${resource}`;
}
// 写和分析两种权限当前都给管理员和协作成员；只读成员只能看。真正的检查在服务端。
function canWrite() {
  return state.workspace && ["admin", "member"].includes(state.workspace.role);
}
async function run(button, action) {
  if (button) button.disabled = true;
  try {
    await action();
  } catch (error) {
    if (!error.discarded) notice(error.message, true);
  } finally {
    if (button) button.disabled = false;
  }
}
function actionButton(text, action, cls) {
  const button = el("button", text, cls);
  button.type = "button";
  button.addEventListener("click", () => run(button, action));
  return button;
}
function key() {
  return crypto.randomUUID();
}
// 带幂等键提交修改。网络中断时不知道服务端有没有处理，所以把幂等键存在 sessionStorage 里，
// 重试（甚至刷新页面后重试）时用同一个键，服务端就不会重复处理。
// sessionStorage 里只存"请求指纹 → 幂等键"，不存企业资料、密码或会话令牌。请求成功后删除。
async function command(resource, data) {
  const epoch = state.epoch;
  const endpoint = path(resource);
  const hash = Array.from(
    new Uint8Array(
      await crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(JSON.stringify([state.session.user.id, endpoint, data])),
      ),
    ),
  )
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  if (epoch !== state.epoch) throw stale();
  const storageKey = "bidradar-command-" + hash;
  let commandKey = sessionStorage.getItem(storageKey);
  if (!commandKey) {
    commandKey = key();
    sessionStorage.setItem(storageKey, commandKey);
  }
  // 失败时不换键；如果用户改了内容，指纹不同，自然会用新的键。
  const result = await api(endpoint, { ...data, key: commandKey });
  sessionStorage.removeItem(storageKey);
  return result;
}
function empty(container, title, text, iconName = "inbox") {
  const box = el("div", null, "empty");
  box.append(icon(iconName), el("strong", title), el("p", text));
  container.append(box);
}
// 读取目录时先放几行占位，结果出来时列表高度不会突然跳动。
function skeletonRows(count) {
  return Array.from({ length: count }, () => {
    const row = el("div", null, "row skeleton"),
      lines = el("div");
    lines.append(el("div", null, "bar w-60"), el("div", null, "bar w-35"));
    row.append(lines, el("div", null, "bar w-btn"));
    return row;
  });
}
// "预算 123,400.00 元"这类字段：标签用普通文字，数值用等宽数字，方便上下行对齐比较。
function labelled(label, value) {
  const span = el("span", label + " ");
  span.append(el("span", value, "num"));
  return span;
}
function dateText(value) {
  if (!value) return "尚未确认";
  return value.local.replace("T", " ") + (value.timezone === "+08:00" ? "（北京时间）" : "（原文时区未确认）");
}
// 金额在接口里是十进制字符串。显示时直接处理字符串、不转成浮点数，避免大金额的小数被舍入。
function factText(fact) {
  if (!fact || fact.status !== "known") return statusNames[fact?.status] || "尚未确认";
  if (fact.value?.amount !== undefined) {
    const [whole, fraction] = fact.value.amount.split(".");
    return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction ? "." + fraction : "") + " 元";
  }
  if (fact.value?.local) return dateText(fact.value);
  return String(fact.value);
}
function localTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}
function safeLink(label, value) {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password)
      return el("span", "来源链接待核对");
    const a = el("a", label);
    a.href = url.href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
  } catch (_) {
    return el("span", "来源链接待核对");
  }
}
function valueText(value) {
  if (value === null || value === undefined) return "尚未记录";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}
function jsonDetails(title, value) {
  const box = el("details");
  box.append(el("summary", title), el("pre", valueText(value), "json-evidence"));
  return box;
}
function sampleBadge(simulation) {
  return badge(
    simulation === true ? "虚构演示资料" : simulation === false ? "真实公开样本" : "资料范围待核对",
    simulation === false ? "neutral" : "warning",
  );
}
// 费用单位是百万分之一元（micro-CNY）。用 BigInt 处理，不经过浮点数。去掉末尾多余的 0，至少保留两位小数。
function yuan(value) {
  if (!(typeof value === "number" && Number.isSafeInteger(value) && value >= 0)) return "未知";
  const amount = BigInt(value);
  const fraction = (amount % 1000000n)
    .toString()
    .padStart(6, "0")
    .replace(/0{1,4}$/, "");
  return "¥" + (amount / 1000000n).toString() + "." + fraction;
}
function bytes(value) {
  if (value < 1024 * 1024) return Math.max(1, Math.round(value / 1024)) + " KB";
  return (value / 1024 / 1024).toFixed(1) + " MB";
}

// ---- 登录、公司空间、目录 ----

async function loadSession() {
  const previousWorkspace = state.workspace?.workspace_id;
  state.session = await api("/api/session");
  $("login-screen").hidden = true;
  $("workspace-screen").hidden = false;
  $("account-name").textContent = state.session.user.display_name;
  $("account-avatar").textContent = initial(state.session.user.display_name);
  const select = $("workspace-select");
  select.replaceChildren();
  for (const workspace of state.session.workspaces) {
    const option = el("option", workspace.name);
    option.value = workspace.workspace_id;
    select.append(option);
  }
  if (!state.session.workspaces.length) {
    state.workspace = null;
    state.profile = null;
    clearPrivateView();
    $("role-badge").textContent = "没有可访问的公司空间";
    for (const name of Object.keys(views)) $(name + "-view").hidden = true;
    notice("当前账号没有有效的公司成员关系。", true);
    return;
  }
  if (state.session.workspaces.some((w) => w.workspace_id === previousWorkspace)) select.value = previousWorkspace;
  await switchWorkspace(select.value);
}
async function switchWorkspace(id) {
  state.epoch += 1;
  $("detail-dialog").close();
  clearPrivateView();
  state.workspace = state.session.workspaces.find((w) => w.workspace_id === id);
  state.profile = null;
  $("role-badge").textContent = roleNames[state.workspace.role];
  $("notice").hidden = true;
  await loadProfile();
  await showView(state.view);
}
const views = {
  discover: ["商机目录", "浏览和筛选不会调用模型。打开一条公告，上传招标文件后可以发起研究。"],
  research: ["研究", "只在固定下来的资料里找依据，每条结论都附原文。判断不了的标为待核查，不会猜。"],
  tracking: ["决定与跟踪", "人工决定、关注设置，以及资料变化后的提醒与自动复核。"],
  profile: ["企业档案", "企业对自身能力与限制的声明，管理员确认后生成正式版本。"],
  members: ["成员", "公司成员、角色与访问权限。"],
};
async function showView(view) {
  stopRunPolling();
  state.runVersion += 1;
  state.view = view;
  $("page-title").textContent = views[view][0];
  $("header-context").textContent = views[view][1];
  for (const name of Object.keys(views)) $(name + "-view").hidden = name !== view;
  for (const button of document.querySelectorAll("[data-view]"))
    button.classList.toggle("active", button.dataset.view === view);
  if (!state.workspace) return;
  if (view === "discover") await search(false);
  if (view === "profile") await loadProfile();
  if (view === "members") await loadMembers();
  if (view === "research") await loadResearch();
  if (view === "tracking") await loadTracking();
}
async function search(next) {
  const searchVersion = ++state.searchVersion;
  if (!next) {
    state.cursor = null;
    state.query = {
      keyword: $("keyword").value.trim(),
      kind: $("kind").value,
      simulation: $("data-mode").value,
      page_size: "8",
    };
  }
  const params = new URLSearchParams(state.query);
  if (next && state.cursor) params.set("cursor", state.cursor);
  const list = $("notice-list");
  list.replaceChildren(...skeletonRows(3));
  try {
    const result = await api(path("notices") + "?" + params);
    if (searchVersion !== state.searchVersion) return;
    list.replaceChildren();
    state.cursor = result.next_cursor;
    $("next-page").hidden = !state.cursor;
    $("total-count").textContent = result.total;
    $("data-label").textContent = result.simulation ? "虚构演示样本" : "已归档公开公告";
    $("as-of").textContent = "核对于 " + localTime(result.as_of);
    $("source-status").textContent = result.recent_runs.length
      ? "最近导入：" +
        result.recent_runs
          .slice(0, 3)
          .map(
            (r) =>
              `${r.source_id === "cn_ccgp" ? "中国政府采购网" : r.source_id === "cn_hainan" ? "海南公共资源交易" : r.source_id} ${r.observations} 条${r.failures ? "（有获取缺口）" : ""}`,
          )
          .join("；") +
        "。只覆盖本地已归档的公告，不代表实时或全国范围。"
      : "当前范围还没有导入记录。";
    if (!result.items.length)
      empty(list, "没有符合条件的公告", "换个关键词或公告类型试试。结果只覆盖本地已归档的公告。", "file");
    for (const item of result.items) {
      const row = el("article", null, "row"),
        body = el("div"),
        meta = el("div", null, "row-meta");
      // 标题本身就能打开详情：列表里最常做的事就是看证据，不必每次都去找按钮。
      const title = el("button", item.title || "未取得标题", "row-title");
      title.type = "button";
      title.addEventListener("click", () => run(null, () => showDetail(item.notice_id)));
      meta.append(
        el("span", kindNames[item.notice_type] || "类型待核实"),
        labelled("预算", factText(item.facts.budget)),
        labelled("截止", factText(item.facts.response_deadline)),
        badge(item.simulation ? "虚构样例" : "真实公开公告", item.simulation ? "warning" : "neutral"),
        badge(statusNames[item.currentness.status] || "状态待核实", "neutral"),
      );
      body.append(title, meta);
      const actions = el("div", null, "row-actions");
      actions.append(actionButton("查看证据与研究", () => showDetail(item.notice_id)));
      row.append(body, actions);
      list.append(row);
    }
  } catch (error) {
    if (error.discarded || searchVersion !== state.searchVersion) return;
    list.replaceChildren();
    empty(list, "目录暂时无法读取", error.message);
    $("next-page").hidden = true;
    $("total-count").textContent = "—";
    throw error;
  }
}
function evidenceBlock(title, evidence) {
  const section = el("details");
  section.append(el("summary", `${title}（${evidence.length} 条依据）`));
  if (!evidence.length) section.append(el("p", "尚未取得可用于这一项的原文依据。", "muted"));
  for (const entry of evidence) {
    const quote = el("div", entry.text, "quote");
    quote.append(el("small", `${entry.label || "原文"} · 位置：${entry.locator}`));
    section.append(quote);
  }
  return section;
}
async function showDetail(id) {
  const version = ++state.detailVersion;
  const result = await api(path("notices/" + id));
  if (version !== state.detailVersion) return;
  const item = result.current,
    container = $("detail-content");
  container.replaceChildren();
  container.append(
    badge(kindNames[item.notice_type]),
    el("h2", item.title),
    safeLink("打开来源原文 ↗", item.source_url),
    el(
      "p",
      "材料获取时间：" + localTime(item.observed_at) + " · " + (item.simulation ? "虚构演示资料" : "真实公开资料"),
      "muted",
    ),
  );
  const facts = el("dl", null, "facts");
  for (const [name, title] of [
    ["project_number", "项目编号"],
    ["buyer", "采购人"],
    ["budget", "预算与口径"],
    ["response_deadline", "响应截止"],
    ["published_at", "公告日期"],
    ["lot_identifier", "标段标识"],
  ]) {
    const box = el("div");
    box.append(el("dt", title), el("dd", factText(item.facts[name])));
    facts.append(box);
  }
  container.append(facts);
  container.append(
    el(
      "p",
      (statusNames[result.currentness.status] || "当前状态待核实") +
        "。公告日期和原截止时间不代表项目现在一定可报名；资格与交付条件需要逐项核查。",
      "note",
    ),
  );
  // 研究入口放在事实下面、原文证据上面：看完概况，下一步通常就是"上传标书、看我们能不能做"。
  const research = el("section", null, "detail-research");
  container.append(research);
  for (const [name, title] of [
    ["budget", "金额原文与冲突依据"],
    ["response_deadline", "截止时间依据"],
  ])
    container.append(evidenceBlock(title, item.facts[name]?.evidence || []));
  const evidence = item.evidence_fields;
  if (evidence) {
    for (const [name, title] of [
      ["technical_evidence", "技术与需求"],
      ["qualification_evidence", "资格条件"],
      ["delivery_evidence", "交付要求"],
      ["category_evidence", "分类依据"],
    ])
      container.append(evidenceBlock(title, evidence[name] || []));
    container.append(evidenceBlock("采购文件获取时间", evidence.acquisition_window?.evidence || []));
    container.append(
      evidenceBlock(
        "登录、申请或付费等获取门槛",
        (evidence.access_conditions || []).flatMap((x) => x.evidence),
      ),
    );
  }
  const history = el("details");
  history.append(el("summary", `历史观察与关联（${result.history.length} 个版本）`));
  for (const observation of result.history)
    history.append(
      el("p", localTime(observation.observed_at) + " · " + observation.observation_id.slice(0, 12), "muted"),
    );
  history.append(
    el(
      "p",
      result.relationships.length
        ? `发现 ${result.relationships.length} 条有依据的公告关系；更正和结果各自保留身份，不覆盖原公告。`
        : "尚无已建立的关联证据。",
      "muted",
    ),
  );
  container.append(history);
  container.append(
    actionButton("记录决定 / 设置关注", async () => {
      $("detail-dialog").close();
      await openTracking(item.notice_id, item.title, item.simulation);
    }),
  );
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
  await renderDetailResearch(research, item, version);
}

// ---- 公告详情里的研究入口：上传招标文件 → 选择文件 → 预估 → 确认发起 ----

const documentStates = { queued: "等待解析", parsing: "正在解析", parsed: "已解析", failed: "解析失败" };
const documentErrors = {
  document_unreadable: "文件打不开或已损坏",
  document_too_large: "页数或内容超过上限",
  unsupported_file_type: "不是文字版 PDF 或 .docx",
};
async function renderDetailResearch(section, item, version) {
  const result = await api(path("documents") + "?notice_id=" + encodeURIComponent(item.notice_id));
  if (version !== state.detailVersion) return;
  section.replaceChildren();
  const head = el("div", null, "detail-research-head");
  head.append(
    el("h3", "研究这条公告"),
    el("p", "研究材料 = 公告里已归档的原文证据 + 你上传的招标文件。上传的文件只有本公司成员能看到。", "muted"),
  );
  section.append(head);
  const list = el("div", null, "doc-list"),
    picked = new Set();
  if (!result.items.length)
    list.append(el("p", "还没有上传招标文件。只用公告原文也能研究，但公告里的要求通常不全。", "muted"));
  for (const doc of result.items) {
    const row = el("label", null, "doc-row"),
      box = el("input"),
      info = el("span", null, "doc-info");
    box.type = "checkbox";
    box.disabled = doc.status !== "parsed" || !canWrite();
    box.checked = doc.status === "parsed";
    if (box.checked) picked.add(doc.id);
    box.addEventListener("change", () => (box.checked ? picked.add(doc.id) : picked.delete(doc.id)));
    info.append(
      el("strong", doc.filename),
      el(
        "span",
        `${doc.file_type.toUpperCase()} · ${bytes(doc.size_bytes)}` +
          (doc.status === "parsed" ? ` · ${doc.page_count ?? "?"} 页 · ${doc.chunk_count} 段` : ""),
        "muted",
      ),
    );
    row.append(box, info, badge(documentStates[doc.status] || doc.status, runTone(doc.status)));
    if (doc.status === "failed")
      row.append(el("span", documentErrors[doc.error_code] || "解析没有成功", "error doc-error"));
    if (doc.warning === "no_text_extracted")
      row.append(el("span", "没有读到文字：可能是扫描件，研究时用不上它", "note doc-error"));
    list.append(row);
  }
  section.append(list);
  const pending = result.items.some((d) => ["queued", "parsing"].includes(d.status));
  const actions = el("div", null, "record-actions");
  if (canWrite()) {
    const form = el("form", null, "upload-form"),
      input = el("input"),
      choose = el("label", null, "upload-button");
    input.type = "file";
    input.accept = ".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    input.className = "sr-only";
    choose.append(icon("upload"), el("span", "上传招标文件"), input);
    input.addEventListener("change", () =>
      run(null, async () => {
        const file = input.files?.[0];
        if (!file) return;
        if (file.size > 20 * 1024 * 1024) throw new Error(errors.file_too_large);
        const body = new FormData();
        body.append("file", file);
        body.append("notice_id", item.notice_id);
        await upload(path("documents"), body);
        notice("文件已上传，后台正在解析。解析完成后可以勾选它参与研究。");
        await renderDetailResearch(section, item, version);
      }),
    );
    form.append(choose);
    actions.append(form);
  }
  if (pending) actions.append(actionButton("刷新解析状态", () => renderDetailResearch(section, item, version)));
  const estimateBox = el("div", null, "estimate");
  if (canWrite())
    actions.append(
      actionButton(
        "预估范围和费用",
        async () => {
          if (!state.profile?.current) throw new Error(errors.profile_required);
          const documentIds = [...picked].sort();
          const estimate = await api(path("research/estimate"), {
            notice_id: item.notice_id,
            document_ids: documentIds,
          });
          if (version !== state.detailVersion) return;
          renderEstimate(estimateBox, item, documentIds, estimate);
        },
        "primary",
      ),
    );
  else section.append(el("p", "只读成员可以查看研究报告，不能发起研究。", "muted"));
  section.append(actions, estimateBox);
}
function renderEstimate(box, item, documentIds, estimate) {
  box.replaceChildren();
  const grid = el("div", null, "budget-grid");
  for (const [title, value] of [
    ["研究方式", estimate.mode === "agent" ? "AI 研究（会调用模型）" : "规则基线（不调用模型）"],
    ["材料", `${estimate.documents} 份文件 + 公告原文 · ${estimate.chunks} 段 · ${estimate.characters} 字`],
    ["预计模型调用", estimate.mode === "agent" ? `最多约 ${estimate.model_calls} 次` : "0 次"],
    ["预计费用上限", estimate.mode === "agent" ? yuan(estimate.cost_micro) : "¥0"],
    ["单任务费用上限", yuan(estimate.run_cap_micro)],
    ["企业档案", `第 ${state.profile.current.version} 版`],
  ]) {
    const cell = el("div");
    cell.append(el("span", title), el("strong", value));
    grid.append(cell);
  }
  box.append(
    grid,
    el(
      "p",
      estimate.mode === "agent"
        ? "预计费用是按“每条都要问模型”估的上限，实际通常更少；任何一层额度用完都会停下，不会超支。"
        : "模型没有开启：抽取要求用规则，判断不了的一律标为待核查。不花钱，可以离线演示。",
      "muted",
    ),
  );
  box.append(
    actionButton(
      estimate.mode === "agent" ? "确认费用，发起研究" : "确认，生成规则基线报告",
      async () => {
        const created = await command("research/runs", { notice_id: item.notice_id, document_ids: documentIds });
        $("detail-dialog").close();
        state.runId = created.id;
        notice("研究任务已保存，后台开始执行。刷新或离开页面不会重复建任务。");
        await showView("research");
      },
      "primary",
    ),
  );
}

// ---- 企业档案、成员 ----

async function loadProfile() {
  state.profile = await api(path("profile"));
  const current = state.profile.current;
  const version = current?.version || 0;
  $("profile-label").textContent = version ? `已确认 · 第 ${version} 版` : "尚未确认";
  $("profile-version").textContent = version ? `当前第 ${version} 版` : "尚未确认";
  const form = $("profile-form");
  form.replaceChildren();
  for (const [name, title, placeholder] of profileFields) {
    const label = el("label", title);
    const input = el(name === "company_name" || name === "city" ? "input" : "textarea");
    input.name = name;
    input.id = "profile-" + name;
    input.placeholder = placeholder;
    input.value = current?.payload?.[name] || "";
    input.maxLength = name === "company_name" || name === "city" ? 200 : 4000;
    input.disabled = !canWrite();
    if (name === "company_name") input.required = true;
    label.append(input);
    form.append(label);
  }
  const submit = el("button", "提交档案修改建议", "primary full");
  submit.type = "submit";
  submit.disabled = !canWrite();
  form.append(submit);
  const proposals = $("proposals");
  proposals.replaceChildren();
  const pending = state.profile.proposals.filter((p) => p.status === "proposed");
  if (!pending.length) proposals.append(el("p", "暂无待确认建议。", "muted"));
  for (const proposal of pending) {
    const record = el("div", null, "record");
    record.append(
      el("p", `${proposal.payload.company_name} · 基于第 ${proposal.base_version} 版`),
      el("p", "提交于 " + localTime(proposal.created_at), "muted"),
    );
    const details = el("details");
    details.append(el("summary", "查看建议内容"));
    for (const [name, title] of profileFields)
      details.append(el("p", `${title}：${proposal.payload[name] || "尚未确认"}`, "long-text"));
    record.append(details);
    if (state.workspace.role === "admin") {
      const confirm = actionButton(
        "确认并生成正式版本",
        async () => {
          await command("profile/confirm", { proposal_id: proposal.id, expected_version: version });
          notice("正式档案已生成新版本，历史内容保持不变。基于旧版本的研究报告会标注“档案已变化”。");
          await loadProfile();
        },
        "primary",
      );
      confirm.disabled = proposal.base_version !== version;
      record.append(confirm);
      if (proposal.base_version !== version) record.append(el("p", "这条建议基于旧档案，请重新提出建议。", "muted"));
    } else record.append(el("p", "等待公司管理员确认。", "muted"));
    proposals.append(record);
  }
  const history = $("profile-history");
  history.replaceChildren();
  if (!state.profile.history.length) history.append(el("p", "尚无正式档案版本。", "muted"));
  for (const entry of state.profile.history) {
    const details = el("details");
    details.append(el("summary", `第 ${entry.version} 版 · ${localTime(entry.confirmed_at)}`));
    for (const [name, title] of profileFields)
      details.append(el("p", `${title}：${entry.payload[name] || "尚未确认"}`, "long-text"));
    details.append(el("p", "企业自己的声明 · 证明未提供 · 有效性未核查", "muted"));
    history.append(details);
  }
}
async function loadMembers() {
  const result = await api(path("members"));
  const list = $("member-list");
  list.replaceChildren();
  for (const member of result.items) {
    const row = el("div", null, "member-row"),
      avatar = el("span", initial(member.display_name), "avatar"),
      info = el("div");
    avatar.setAttribute("aria-hidden", "true");
    info.append(
      el("strong", member.display_name),
      el("p", member.username + (member.active ? " · 可访问" : " · 已撤权"), "muted"),
    );
    row.append(avatar, info);
    if (state.workspace.role === "admin") {
      const select = el("select");
      select.setAttribute("aria-label", member.display_name + "的角色");
      for (const [role, label] of Object.entries(roleNames)) {
        const option = el("option", label);
        option.value = role;
        select.append(option);
      }
      select.value = member.role;
      row.append(
        select,
        actionButton("保存角色", async () => {
          await api(path("members/change"), {
            target_user_id: member.user_id,
            role: select.value,
            active: member.active,
            expected_version: member.version,
          });
          notice("成员角色已更新。下一次操作按新权限检查；这位成员没结束的研究和自动复核会停下。");
          await loadSession();
        }),
        actionButton(member.active ? "撤销访问" : "恢复访问", async () => {
          await api(path("members/change"), {
            target_user_id: member.user_id,
            role: member.role,
            active: !member.active,
            expected_version: member.version,
          });
          notice("成员访问状态已更新。");
          await loadSession();
        }),
      );
    } else row.append(badge(roleNames[member.role], "neutral"));
    list.append(row);
  }
}

// ---- 研究 ----
// 都通过当前公司的 /api/workspaces/{公司}/research/... 接口访问。公告原文和模型输出都只当作文字显示。

const runStates = {
  queued: "排队中",
  running: "正在研究",
  succeeded: "已完成",
  partial: "部分完成 · 有缺口",
  failed: "失败",
  cancelled: "已停止",
  waiting_input: "等人处理 · 自动执行已停止",
};
const reassessmentStates = {
  pending: "待派发",
  dispatched: "已建研究任务",
  finished: "已完成",
  cancelled: "已作废",
  rejected: "没能发起",
};
const findingStates = {
  met: "满足",
  unmet: "明确不满足",
  unknown: "待核查",
  conflicting: "资料冲突",
  not_applicable: "不适用",
};
// 满足用绿色，不满足用红色，待核查和冲突用橙色（需要人去核查）。
const findingTones = {
  met: "",
  unmet: "danger",
  unknown: "warning",
  conflicting: "warning",
  not_applicable: "neutral",
};
const dimensionNames = {
  qualification: "资格",
  capability: "能力与技术",
  experience: "业绩案例",
  schedule: "工期",
  onsite: "驻场与现场",
  location: "服务地点",
  staffing: "人员要求",
  commercial: "商务与付款",
  materials: "投标材料",
  scoring: "评分项",
  fact: "项目事实",
  other: "其他",
};
const verdictNames = {
  supported: "模型核验：依据充分",
  uncertain: "模型核验：拿不准",
  unsupported: "未通过核验",
  skipped: "只做了程序检查",
};
const kindLabels = { analysis: "项目研究", question: "追问", reassessment: "变化复核" };
const decisionStates = { needs_review: "需要进一步核查", follow_up: "人工决定跟进", dismissed: "人工决定放弃" };

function modeBadge(mode) {
  return badge(mode === "baseline" ? "规则基线 · 不调模型" : "AI 研究", mode === "baseline" ? "neutral" : "");
}
function stopRunPolling() {
  if (state.pollTimer !== null) clearTimeout(state.pollTimer);
  state.pollTimer = null;
}
async function loadBudget() {
  const container = $("research-budget");
  try {
    const result = await api(path("research/budget"));
    state.budget = result;
    container.replaceChildren();
    for (const [title, value] of [
      ["模型", result.model_enabled ? "已开启" : "未开启 · 研究用规则基线"],
      ["本公司已花（含预留）", yuan(result.workspace_used)],
      ["本公司额度", yuan(result.workspace_cap)],
      ["单任务上限", yuan(result.run_cap)],
      ["全部公司合计上限", yuan(result.total_cap)],
      ["本公司调用 / 结果不确定", `${result.workspace_calls} / ${result.workspace_unknown}`],
    ]) {
      const box = el("div");
      box.append(el("span", title), el("strong", value));
      container.append(box);
    }
    container.append(
      el(
        "p",
        `价格版本：${result.price_version}。已花 = 已结算的实际费用 + 还在进行中或结果不确定的预留额，宁可多算；不代表供应商的实际账单。`,
        "muted budget-basis",
      ),
    );
  } catch (error) {
    if (error.discarded) return;
    container.replaceChildren(el("p", "费用信息暂时读不到；不能把未知费用当作零。" + error.message, "error"));
  }
}
function latestRunStatus(run) {
  // 按版本号取最新状态：如果详情已经显示"完成"，晚到的旧列表数据不能把它改回"排队中"。
  const previous = state.runStatuses.get(run.id),
    version = Number.isInteger(run.version) ? run.version : 0;
  if (!previous || version >= previous.version) state.runStatuses.set(run.id, { status: run.status, version });
  return state.runStatuses.get(run.id).status;
}
function runStatusBadge(status) {
  return badge(runStates[status] || status, "run-state " + runTone(status));
}
function syncRunListStatus(run) {
  const status = latestRunStatus(run);
  for (const card of $("research-list").querySelectorAll("[data-run-id]")) {
    if (card.dataset.runId === run.id) card.querySelector(".run-state")?.replaceWith(runStatusBadge(status));
  }
}
async function loadRunList(more = false) {
  const version = ++state.runListVersion,
    list = $("research-list");
  const params = new URLSearchParams({ limit: "30" });
  if (more && state.runBefore !== null) params.set("before", state.runBefore);
  const result = await api(path("research/runs") + "?" + params);
  if (version !== state.runListVersion) return;
  if (!more) list.replaceChildren();
  state.runBefore = result.next_before ?? null;
  $("more-research").hidden = state.runBefore === null;
  if (!result.items.length && !more)
    empty(list, "还没有研究任务", "到商机目录打开一条公告，上传招标文件后预估费用、确认发起。");
  for (const item of result.items) {
    const card = el("article", null, "run-card");
    card.dataset.runId = item.id;
    card.classList.toggle("is-active", item.id === state.runId);
    const tags = el("div", null, "tags");
    tags.append(modeBadge(item.mode), sampleBadge(item.simulation));
    card.append(tags, el("h3", item.title || "公告已不在目录里"), runStatusBadge(latestRunStatus(item)));
    card.append(
      el(
        "p",
        `${kindLabels[item.kind] || item.kind} · 档案第 ${item.profile_version} 版 · ${localTime(item.created_at)}`,
        "muted",
      ),
    );
    if (item.question) card.append(el("p", "问：" + item.question, "muted long-text"));
    card.append(actionButton("查看报告与进度", () => viewRun(item.id)));
    list.append(card);
  }
}
async function loadResearch() {
  // 任务列表和额度是两个独立请求，一个失败不影响另一个显示。
  const results = await Promise.allSettled([loadRunList(), loadBudget()]);
  for (const result of results)
    if (result.status === "rejected" && !result.reason.discarded) notice(result.reason.message, true);
  if (state.runId && state.view === "research") await viewRun(state.runId);
}
async function viewRun(id, remaining = 40) {
  stopRunPolling();
  const version = ++state.runVersion;
  state.runId = id;
  const detail = await api(path("research/runs/" + encodeURIComponent(id)));
  if (version !== state.runVersion || state.view !== "research") return;
  renderRun(detail, remaining);
  syncRunListStatus(detail);
  // 左侧列表高亮正在查看的任务。
  for (const card of $("research-list").querySelectorAll("[data-run-id]"))
    card.classList.toggle("is-active", card.dataset.runId === detail.id);
  // 轮询任务状态：最多 40 次，每次间隔 3 秒。只查询已有任务，不会重新创建。
  if (["queued", "running"].includes(detail.status) && remaining > 0) {
    state.pollTimer = setTimeout(async () => {
      if (state.view !== "research" || state.runId !== id || version !== state.runVersion) return;
      try {
        await viewRun(id, remaining - 1);
      } catch (error) {
        if (!error.discarded) notice("进度刷新中断，任务仍保留。" + error.message, true);
      }
    }, 3000);
  }
}
function quoteBlock(chunk, label) {
  const quote = el("div", chunk.text, "quote");
  const where = [
    chunk.document_id.startsWith("notice:") ? "公告原文" : "招标文件",
    chunk.page ? `第 ${chunk.page} 页` : null,
    chunk.section_path?.length ? chunk.section_path.join(" / ") : null,
  ]
    .filter(Boolean)
    .join(" · ");
  quote.append(el("small", (label ? label + " · " : "") + where));
  return quote;
}
// 待核查的结论通常最多，折叠成一行（状态、维度、原文要求），点开再看理由和出处；
// 满足、不满足、冲突是要人认真看的，直接展开。
function renderFinding(finding, requirement, chunks) {
  const compact = finding.status === "unknown";
  const card = el(compact ? "details" : "article", null, "finding finding-" + finding.status);
  const head = el(compact ? "summary" : "div", null, compact ? "finding-summary" : "");
  const tags = el("div", null, "tags");
  tags.append(
    badge(findingStates[finding.status] || finding.status, findingTones[finding.status] ?? "neutral"),
    badge(dimensionNames[requirement?.dimension] || "其他", "neutral"),
  );
  if (finding.basis === "rule") tags.append(badge("规则判断", "neutral"));
  if (finding.verification?.downgraded) tags.append(badge("核验后降为待核查", "warning"));
  head.append(tags, el("h3", requirement?.quote || "（原文缺失）"));
  card.append(head);
  card.append(el("p", finding.reason, "long-text"));
  if (finding.profile_quote) {
    const fields = finding.profile_fields.map((name) => profileFieldNames[name] || name).join("、");
    card.append(el("p", `档案依据（${fields}）：“${finding.profile_quote}”`, "profile-quote"));
  }
  if (finding.gap) card.append(el("p", "还缺：" + finding.gap, "note"));
  const sources = el("details");
  sources.append(el("summary", `原文出处（${requirement?.chunk_ids.length || 0} 段）`));
  for (const id of requirement?.chunk_ids || []) {
    const chunk = chunks.get(id);
    if (chunk) sources.append(quoteBlock(chunk));
  }
  if (compact) for (const id of requirement?.chunk_ids || []) chunks.has(id) && card.append(quoteBlock(chunks.get(id)));
  else card.append(sources);
  // 占位匹配没有可核验的内容，不显示核验那一行，免得让人以为"检查过了"。
  const verification = finding.basis === "placeholder" ? null : finding.verification;
  if (verification) {
    const line =
      (verdictNames[verification.verdict] || verification.verdict) +
      (verification.attempts > 1 ? ` · 重做 ${verification.attempts - 1} 次` : "") +
      (verification.checks_failed?.length ? ` · 没通过：${verification.checks_failed.join("、")}` : "");
    card.append(el("p", line, "muted"));
    if (verification.judge_note) card.append(el("p", "核验记录：" + verification.judge_note, "muted long-text"));
  }
  return card;
}
function renderReport(detail, container) {
  const report = detail.report;
  const chunks = new Map(report.chunks.map((c) => [c.id, c]));
  const requirements = new Map(report.requirements.map((r) => [r.id, r]));
  container.append(el("h3", "研究报告"));
  container.append(
    el("p", report.summary.text, "report-summary"),
    el(
      "p",
      (report.summary.source === "model" ? "摘要由模型撰写，已核对其中的数字。" : "摘要由程序按结论统计生成。") +
        "结论只依据已读取的文件和企业档案；档案是企业自己的声明，确认不等于资质认证。",
      "note",
    ),
  );
  if (report.facts.length) {
    const facts = el("dl", null, "facts");
    for (const fact of report.facts) {
      const box = el("div");
      box.append(el("dt", fact.label), el("dd", fact.quote));
      facts.append(box);
    }
    container.append(el("h3", "项目事实"), facts);
  }
  const counts = {};
  for (const finding of report.findings) counts[finding.status] = (counts[finding.status] || 0) + 1;
  container.append(
    el("h3", "要求对照"),
    el(
      "p",
      Object.entries(findingStates)
        .filter(([status]) => counts[status])
        .map(([status, name]) => `${name} ${counts[status]}`)
        .join(" · ") || "没有识别出需要企业满足的要求。",
      "muted",
    ),
  );
  for (const finding of report.findings)
    container.append(renderFinding(finding, requirements.get(finding.requirement_id), chunks));
  if (report.checklist.length) {
    const list = el("ol", null, "checklist");
    for (const item of report.checklist) {
      const li = el("li", item.text);
      if (item.requirement_ids.length > 1)
        li.append(el("span", ` （涉及 ${item.requirement_ids.length} 条要求）`, "muted"));
      list.append(li);
    }
    container.append(el("h3", "待核查清单"), list);
  }
  const coverage = report.coverage;
  const box = el("details");
  box.append(el("summary", "读取范围与限制"));
  box.append(
    el(
      "p",
      `读了 ${coverage.documents.length} 份材料、${coverage.chunk_count} 段原文，分 ${coverage.batches} 批抽取要求；` +
        `漏检复查补回 ${coverage.recall_added} 条；${coverage.downgraded} 条结论没通过核验，降为待核查。`,
    ),
  );
  for (const section of coverage.skipped_sections)
    box.append(el("p", "作为格式模板跳过：" + section.join(" / "), "muted"));
  for (const issue of coverage.extraction_issues)
    box.append(el("p", `抽取出过问题：${issue.section.join(" / ") || "（未分章节）"} · ${issue.code}`, "note"));
  box.append(el("p", "规则版本：" + (report.versions.config_version || "未记录"), "identifier"));
  container.append(box);
}
function renderAnswer(detail, container) {
  const answer = detail.report;
  container.append(el("h3", "追问回答"), el("p", "问：" + detail.question, "muted long-text"));
  container.append(el("p", answer.answer, "report-summary"));
  container.append(
    el(
      "p",
      answer.verified
        ? "回答里的事实都核对过引用的原文。"
        : "这次没能给出有依据的回答，所以只显示“无法确认”，没有把未经核对的内容展示出来。",
      "note",
    ),
  );
  for (const chunk of answer.chunks || []) container.append(quoteBlock(chunk, "引用"));
  if (answer.gaps.length) {
    const list = el("ul", null, "checklist");
    for (const gap of answer.gaps) list.append(el("li", gap));
    container.append(el("h3", "还缺什么"), list);
  }
  container.append(el("p", `模型调用 ${answer.model_calls} 次 · 工具调用 ${answer.tool_calls} 次`, "muted"));
}
function renderRun(detail, remaining) {
  const container = $("research-detail");
  container.replaceChildren();
  const tags = el("div", null, "tags");
  tags.append(modeBadge(detail.mode), sampleBadge(detail.simulation), runStatusBadge(detail.status));
  container.append(tags, el("h2", detail.title || "研究任务"));
  container.append(
    el(
      "p",
      `${kindLabels[detail.kind] || detail.kind} · 档案第 ${detail.profile_version} 版 · ` +
        `${detail.document_ids.length} 份招标文件 · 创建于 ${localTime(detail.created_at)}`,
      "muted",
    ),
  );
  if (detail.reason) container.append(el("p", runReasons[detail.reason] || "原因：" + detail.reason, "note"));
  // 报告是否还是最新的（档案或公告有没有新版本）。报告正文本身永远不会因此被改动。
  const freshness = detail.freshness || {};
  const changed = Object.entries(freshness)
    .filter(([, value]) => value === "changed")
    .map(([name]) => (name === "profile" ? "企业档案" : "公告"));
  if (changed.length)
    container.append(
      el(
        "p",
        `${changed.join("和")}已有新版本。这份报告固定使用旧输入，内容保持原样；需要的话请重新发起研究。`,
        "note",
      ),
    );
  const cost = detail.cost || {};
  container.append(
    el(
      "p",
      `模型调用 ${cost.calls ?? 0} 次 · 已结算 ${yuan(cost.charged ?? 0)}` +
        (cost.reserved_open ? ` · 预留中或结果不确定 ${yuan(cost.reserved_open)}` : "") +
        (detail.estimate_micro ? ` · 发起时预估上限 ${yuan(detail.estimate_micro)}` : ""),
      "muted",
    ),
  );
  if (detail.parent_run_id) container.append(actionButton("查看被追问的报告", () => viewRun(detail.parent_run_id)));
  const actions = el("div", null, "record-actions");
  actions.append(actionButton("刷新", () => viewRun(detail.id)));
  actions.append(
    actionButton("人工决定 / 关注", () => openTracking(detail.notice_id, detail.title, detail.simulation)),
  );
  if (canWrite() && ["queued", "running"].includes(detail.status))
    actions.append(
      actionButton("取消任务", async () => {
        await command(`research/runs/${detail.id}/cancel`, {});
        notice("已取消。已经发出去的模型请求收不回来，但之后不会再发新的。");
        await viewRun(detail.id);
      }),
    );
  container.append(actions);
  if (["queued", "running"].includes(detail.status))
    container.append(
      el(
        "p",
        remaining > 0
          ? "后台执行中，页面每 3 秒刷新一次进度；离开页面不会取消任务。"
          : "自动刷新已停止，可以手动刷新。后台任务仍在执行。",
        "muted",
      ),
    );
  if (detail.report && detail.report_kind === "answer") renderAnswer(detail, container);
  else if (detail.report) renderReport(detail, container);
  else if (!["queued", "running"].includes(detail.status)) container.append(el("p", "这个任务没有生成报告。", "muted"));
  if (detail.report && detail.report_kind === "report" && ["succeeded", "partial"].includes(detail.status))
    renderQuestionForm(detail, container);
}
function renderQuestionForm(detail, container) {
  const form = el("form", null, "question-form"),
    label = el("label", "围绕这份报告追问"),
    input = el("textarea");
  const enabled = canWrite() && state.budget?.model_enabled === true;
  input.required = true;
  input.maxLength = 2000;
  input.placeholder = "例如：驻场要求具体是多长时间、在哪里？";
  input.disabled = !enabled;
  label.append(input);
  const submit = el("button", "提交追问", "primary");
  submit.type = "submit";
  submit.disabled = !enabled;
  form.append(
    label,
    el(
      "p",
      state.budget?.model_enabled
        ? "追问会新建一个任务，沿用这份报告的文件和档案版本，会产生模型费用。回答只在固定材料里找依据。"
        : "追问需要模型；模型没有开启。",
      "muted",
    ),
    submit,
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run(submit, async () => {
      const question = input.value.trim();
      if (!question) throw new Error(errors.invalid_question);
      const created = await command(`research/runs/${detail.id}/questions`, { question });
      notice("追问已保存，后台开始执行。");
      await loadRunList();
      await viewRun(created.id);
    });
  });
  container.append(form);
}

// ---- 跟踪 ----

async function openTracking(noticeId, title, simulation) {
  state.trackedNotice = { notice_id: noticeId, title: title || noticeId, simulation };
  await showView("tracking");
}
async function loadTrackingContext(version) {
  const container = $("tracking-context"),
    target = state.trackedNotice;
  if (!target) {
    container.replaceChildren();
    empty(container, "先指定一个公告", "可以从公告详情或研究报告进入。下面仍可查看本公司的提醒与变化。");
    return;
  }
  container.replaceChildren(el("p", "正在读取这条公告的决定与关注…", "muted"));
  const [decision, watched] = await Promise.all([
    api(path("tracking/decisions/" + target.notice_id)),
    api(path("tracking/watches/" + target.notice_id)),
  ]);
  if (version !== state.trackingVersion) return;
  container.replaceChildren(sampleBadge(target.simulation), el("h3", target.title));
  const current = decision.current,
    watch = watched.watch,
    layout = el("div", null, "tracking-layout");
  const decisionForm = el("form", null, "decision-form"),
    selectLabel = el("label", "人工决定"),
    select = el("select");
  for (const [value, label] of Object.entries(decisionStates)) {
    const option = el("option", label);
    option.value = value;
    select.append(option);
  }
  select.value = current?.state || "needs_review";
  select.disabled = !canWrite();
  selectLabel.append(select);
  const reasonLabel = el("label", "决定原因"),
    reason = el("textarea");
  reason.value = current?.reason || "";
  reason.maxLength = 4000;
  reason.disabled = !canWrite();
  reasonLabel.append(reason);
  const save = el("button", "保存人工决定", "primary");
  save.type = "submit";
  save.disabled = !canWrite();
  decisionForm.append(
    el("h3", `人工记录 · 第 ${current?.version || 0} 版`),
    selectLabel,
    reasonLabel,
    el("p", "人工决定不是资质认证，也不会自动开启或关闭关注。系统不会替你投标、报价或联系采购方。", "muted"),
    save,
  );
  decisionForm.addEventListener("submit", (event) => {
    event.preventDefault();
    run(save, async () => {
      await command("tracking/decisions/" + target.notice_id, {
        state: select.value,
        reason: reason.value.trim(),
        expected_version: current?.version || 0,
      });
      notice("人工决定已保存，关注开关没有改变。");
      await loadTracking();
    });
  });
  if (decision.history?.length) {
    const history = el("details");
    history.append(el("summary", `人工决定历史（${decision.history.length} 版）`));
    for (const entry of decision.history)
      history.append(
        el(
          "p",
          `第 ${entry.version} 版 · ${decisionStates[entry.state] || entry.state} · ${localTime(entry.recorded_at)}` +
            (entry.reason ? ` · ${entry.reason}` : ""),
          "muted long-text",
        ),
      );
    decisionForm.append(history);
  }
  const watchForm = el("form", null, "watch-form"),
    activeLabel = el("label", null, "check-label"),
    active = el("input");
  active.type = "checkbox";
  active.checked = watch?.active === true;
  active.disabled = !canWrite();
  activeLabel.append(active, el("span", "开启公告与档案变化跟踪"));
  const autoLabel = el("label", null, "check-label"),
    auto = el("input");
  auto.type = "checkbox";
  auto.checked = false;
  auto.disabled = !canWrite() || !active.checked;
  autoLabel.append(auto, el("span", "我明确授权：发现变化后自动发起复核"));
  active.addEventListener("change", () => {
    auto.disabled = !canWrite() || !active.checked;
    if (!active.checked) auto.checked = false;
  });
  const saveWatch = el("button", "保存关注设置", "primary");
  saveWatch.type = "submit";
  saveWatch.disabled = !canWrite();
  watchForm.append(
    el("h3", `关注设置 · 第 ${watch?.version || 0} 版`),
    badge(watch?.active ? "正在跟踪" : "未跟踪", "neutral"),
    el(
      "p",
      watch?.auto_reassess ? "自动复核委托到期：" + localTime(watch.expires_at) : "当前没有自动复核委托。",
      "muted",
    ),
    activeLabel,
    autoLabel,
    el(
      "p",
      "授权最多 7 天。每次复核仍按你当前的成员权限、档案版本和剩余额度检查；模型开启时会产生费用。默认不勾选；保存时按这次的勾选重新设置委托。",
      "note",
    ),
    saveWatch,
  );
  watchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    run(saveWatch, async () => {
      await command("tracking/watches/" + target.notice_id, {
        active: active.checked,
        auto_reassess: active.checked && auto.checked,
        expected_version: watch?.version || 0,
      });
      notice("关注设置已保存，人工决定没有改变。");
      await loadTracking();
    });
  });
  if (watch?.active && canWrite())
    watchForm.append(
      actionButton("立即停止关注", async () => {
        await command("tracking/watches/" + target.notice_id, {
          active: false,
          auto_reassess: false,
          expected_version: watch.version,
        });
        notice("关注已停止，委托作废。已经发出去的模型请求收不回来，但之后不会再发新的。");
        await loadTracking();
      }),
    );
  if (watch?.warnings?.length)
    watchForm.append(
      el(
        "p",
        "跟踪限制：" +
          watch.warnings
            .map((value) =>
              value === "unstable_content_snapshot_identity"
                ? "来源使用内容快照身份，无法保证连续版本归为同一公告"
                : value,
            )
            .join("；"),
        "note",
      ),
    );
  layout.append(decisionForm, watchForm);
  container.append(layout);
}
const scopeNames = {
  deadline: "截止时间",
  budget: "金额",
  delivery: "交付",
  qualification: "资格",
  technical: "技术",
  lifecycle: "公告关系",
  materials: "材料",
  profile: "企业档案",
  other: "其他",
};
const changeKinds = {
  material_change: "公告事实或证据有变化",
  unclassified_content_change: "原文有变化 · 还没归类",
  profile_change: "企业档案有新版本",
  no_business_change: "比较过的字段没变",
};
async function loadChanges(more = false, version = state.trackingVersion) {
  const after = more ? state.changeAfter : 0,
    result = await api(path(`tracking/changes?after=${after}&limit=20`));
  if (version !== state.trackingVersion) return;
  const container = $("tracking-changes");
  if (!more) container.replaceChildren();
  state.changeAfter = result.next_after;
  $("more-changes").hidden = result.items.length < 20;
  if (!result.items.length && !more)
    empty(container, "还没有记录到变化", "只说明已处理的归档版本里没有新变化，不保证和来源网站实时同步。");
  for (const change of result.items) {
    const row = el("article", null, "record");
    row.append(
      badge(change.source === "workspace" ? "档案变化" : "公告变化", "neutral"),
      el("h3", changeKinds[change.diff?.kind] || "有待核查的变化"),
      el("p", `${localTime(change.created_at)} · 档案第 ${change.profile_version} 版`, "muted"),
    );
    const scopes = change.diff?.scopes || [];
    if (scopes.length) row.append(el("p", "涉及：" + scopes.map((s) => scopeNames[s] || s).join("、")));
    for (const entry of (change.diff?.entries || []).slice(0, 6))
      row.append(
        el(
          "p",
          `${scopeNames[entry.scope] || entry.scope} · ${entry.path}：${valueText(entry.before)} → ${valueText(entry.after)}`,
          "muted long-text",
        ),
      );
    row.append(el("p", "基于旧资料的报告可能已经过期；原报告不会被覆盖。", "note"));
    row.append(jsonDetails("完整差异", change.diff));
    row.append(actionButton("查看这条公告的决定与关注", () => openTracking(change.notice_id, change.notice_id)));
    container.append(row);
  }
}
async function loadNotifications(more = false, version = state.trackingVersion) {
  const after = more ? state.notificationAfter : 0,
    result = await api(path(`tracking/notifications?after=${after}&limit=20`));
  if (version !== state.trackingVersion) return;
  const container = $("tracking-notifications");
  if (!more) container.replaceChildren();
  state.notificationAfter = result.next_after;
  $("more-notifications").hidden = result.items.length < 20;
  if (!result.items.length && !more)
    empty(container, "暂无站内提醒", "开启关注后，归档里出现的新变化会在这里留下记录。");
  for (const item of result.items) {
    const row = el("article", null, "record");
    const finished = item.kind === "reassessment_finished";
    row.append(
      badge(item.read_at ? "已读" : "未读", item.read_at ? "neutral" : "warning"),
      el("h3", (finished ? "自动复核有结果：" : "关注的公告有变化：") + (item.payload.title || "")),
      el("p", localTime(item.created_at), "muted"),
    );
    if (finished)
      row.append(
        actionButton("查看复核报告", async () => {
          state.runId = item.payload.run_id;
          await showView("research");
        }),
      );
    if (!item.read_at)
      row.append(
        actionButton("标记已读", async () => {
          await api(path(`tracking/notifications/${item.id}/read`), {});
          await loadNotifications();
        }),
      );
    container.append(row);
  }
}
async function loadReassessments(version = state.trackingVersion) {
  const result = await api(path("tracking/reassessments"));
  if (version !== state.trackingVersion) return;
  const container = $("tracking-reassessments");
  container.replaceChildren();
  if (!result.items.length) empty(container, "暂无自动复核", "开启关注不等于授权调用模型；自动复核需要单独明确授权。");
  for (const item of result.items) {
    const row = el("article", null, "record");
    row.append(
      badge(reassessmentStates[item.state] || item.state, runTone(item.state)),
      el("p", localTime(item.created_at), "muted"),
    );
    if (item.reason)
      row.append(
        el(
          "p",
          (item.state === "finished" ? "研究结果：" + (runStates[item.reason] || item.reason) : null) ||
            runReasons["reassessment_" + item.reason] ||
            errors[item.reason] ||
            "原因：" + item.reason,
          "muted",
        ),
      );
    if (item.run_id)
      row.append(
        actionButton("查看研究任务", async () => {
          state.runId = item.run_id;
          await showView("research");
        }),
      );
    container.append(row);
  }
}
async function loadTracking() {
  const version = ++state.trackingVersion;
  const results = await Promise.allSettled([
    loadTrackingContext(version),
    loadChanges(false, version),
    loadNotifications(false, version),
    loadReassessments(version),
  ]);
  for (const result of results)
    if (result.status === "rejected" && !result.reason.discarded) notice(result.reason.message, true);
}

// ---- 事件绑定 ----

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    await api("/api/login", { username: $("username").value, password: $("password").value });
    $("password").value = "";
    await loadSession();
  } catch (error) {
    $("login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("logout").addEventListener("click", () =>
  run($("logout"), async () => {
    await api("/api/logout", {});
    showLogin();
    $("login-error").textContent = "";
  }),
);
$("workspace-select").addEventListener("change", () => run(null, () => switchWorkspace($("workspace-select").value)));
for (const button of document.querySelectorAll("[data-view]"))
  button.addEventListener("click", () => run(button, () => showView(button.dataset.view)));
$("search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(event.submitter, () => search(false));
});
$("next-page").addEventListener("click", () => run($("next-page"), () => search(true)));
$("close-detail").addEventListener("click", () => $("detail-dialog").close());
$("detail-dialog").addEventListener("close", () => (state.detailVersion += 1));
$("notice").addEventListener("click", () => ($("notice").hidden = true));
$("profile-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(event.submitter, async () => {
    const payload = {};
    for (const [name] of profileFields) payload[name] = $("profile-" + name).value.trim() || null;
    await command("profile/proposals", { payload, base_version: state.profile.current?.version || 0 });
    notice("建议已保存，等待公司管理员确认。");
    await loadProfile();
  });
});
$("refresh-research").addEventListener("click", () => run($("refresh-research"), loadResearch));
$("more-research").addEventListener("click", () => run($("more-research"), () => loadRunList(true)));
$("refresh-tracking").addEventListener("click", () => run($("refresh-tracking"), loadTracking));
$("more-notifications").addEventListener("click", () => run($("more-notifications"), () => loadNotifications(true)));
$("more-changes").addEventListener("click", () => run($("more-changes"), () => loadChanges(true)));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopRunPolling();
});
// 在商机目录按 / 直接定位到搜索框。正在输入框里打字、或者开着详情对话框时不拦截。
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable]")) return;
  if ($("workspace-screen").hidden || $("discover-view").hidden || $("detail-dialog").open) return;
  event.preventDefault();
  $("keyword").focus();
});
loadSession().catch((error) => {
  if (error.code !== "authentication_required" && !error.discarded) $("login-error").textContent = error.message;
});
