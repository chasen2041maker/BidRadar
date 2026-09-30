"use strict";
// 页面只提交稳定ID和版本；可信标题/权限/档案版本由服务端再次核对。
// 所有来源与企业文字用textContent展示，禁止把采购原文当可执行HTML。
const $ = (id) => document.getElementById(id);
const state = {session: null, workspace: null, profile: null, view: "discover", cart: new Map(), cursor: null, query: null, epoch: 0, searchVersion: 0,
  runId: null, runVersion: 0, runListVersion: 0, runBefore: null, runStatuses: new Map(), pollTimer: null, trackedNotice: null, trackingVersion: 0, notificationAfter: 0, changeAfter: 0, changeTargets: new Map()};
const roleNames = {admin: "公司管理员", member: "协作成员", viewer: "只读成员"};
const kindNames = {procurement: "采购公告", correction: "更正公告", award: "中标 / 成交", termination: "终止公告", intention: "采购意向", unknown: "类型待核实"};
const statusNames = {known: "已提取", missing: "尚未取得依据", unparsed: "待核对原文", conflicting: "存在冲突", context_required: "需结合金额口径", unknown: "尚未确认", deadline_passed: "原截止时间已过", deadline_not_reached: "原截止时间未到", not_opening_notice: "非采购报名公告"};
const errors = {unauthenticated: "登录已失效，请重新登录。", invalid_credentials: "账号或密码不正确。", login_limited: "登录尝试过多，请稍后重试。", forbidden: "你目前没有执行此操作的权限，请刷新公司空间。", invalid_csrf: "登录状态已变化，请刷新页面后重试。", profile_version_conflict: "企业档案已有新版本，请重新读取后提交。", profile_revision_conflict: "企业档案已有新版本，请重新读取后提交。", stale_profile: "企业档案已有新版本，请重新准备选择。", catalog_version_changed: "公告已有新版本，请刷新目录，查看新内容后重新选择。", catalog_unavailable: "目录服务暂时不可用，已保存的档案和选择仍保留。", idempotency_conflict: "相同操作标识对应了不同内容，请刷新后重新操作。", version_conflict: "记录已变化，请刷新后重新确认。", last_admin: "公司需要保留至少一位管理员。", last_admin_required: "公司需要保留至少一位管理员。", profile_required: "请先由管理员确认一版企业档案。", not_found: "记录不存在或当前无权访问。"};
const profileFields = [["company_name", "公司名称 / 简称", "例如：山岚软件（虚构）"], ["city", "所在城市", "城市不限定可承接的地区"], ["project_types", "希望承接的项目", "软件定制、业务系统、AI应用；也可注明不接的类型"], ["capabilities", "技术与交付能力", "能负责哪些工作，有哪些技术与交付经验"], ["delivery_constraints", "交付与地域限制", "区分优先远程、可出差与明确不接受的驻场要求"], ["cases", "相关案例", "工作范围、团队角色、时间；尚不接收证明文件"], ["qualifications", "资质与证明声明", "确认有 / 没有 / 尚未确认；文字声明不是核验结果"], ["staffing", "人力与排期", "可投入角色、人数、最早时间与已知冲突"], ["commercial_constraints", "商务偏好与硬限制", "请分别写清偏好和明确不能接受的条件"]];
Object.assign(errors, {
  dependency_authentication_failed: "内部服务连接配置失效，暂时无法完成操作。你的登录仍有效，请由维护者检查服务配置。",
  billing_unknown: "本次调用计费结果未知，已停止自动执行。请先人工核对供应商账单与用量，不能盲目重试。",
  configuration_changed: "研究配置已升级，这个旧任务已停止。核对固定输入后，可明确新建任务。",
  selection_input_stale: "这次选择的公告或档案输入已有变化。请重新查看并确认选择，再明确发起新任务。",
  profile_revision_changed: "企业正式档案已有新版本，旧任务已停止。请按新档案重新确认选择与分析。",
  workspace_queue_limit: "公司正在处理的研究任务已达上限。请等待已有任务结束，或明确取消不再需要的任务后重试。"
});

function el(tag, text, cls) {const node = document.createElement(tag); if (text !== undefined && text !== null) node.textContent = String(text); if (cls) node.className = cls; return node;}
function badge(text, type = "") {return el("span", text, "badge " + type);}
function notice(message, failure = false) {$("notice").textContent = message; $("notice").className = "notice" + (failure ? " failure" : ""); $("notice").hidden = false;}
function clearPrivateView() {
  stopRunPolling(); state.runId = null; state.runVersion += 1; state.runListVersion += 1; state.trackedNotice = null; state.trackingVersion += 1; state.changeTargets.clear();
  state.runStatuses.clear();
  state.runBefore = null; state.notificationAfter = 0; state.changeAfter = 0;
  for (const id of ["more-research", "more-notifications", "more-changes"]) $(id).hidden = true;
  for (const id of ["profile-form", "proposals", "profile-history", "selection-list", "member-list", "notice-list", "detail-content", "research-list", "research-detail", "research-budget", "tracking-context", "tracking-notifications", "tracking-changes", "tracking-reassessments"]) $(id).replaceChildren();
  $("detail-dialog").close(); $("profile-label").textContent = "正在读取"; $("profile-version").textContent = "正在读取"; $("source-status").textContent = ""; $("as-of").textContent = ""; $("total-count").textContent = "—";
}
function showLogin() {state.epoch += 1; state.session = null; state.workspace = null; state.profile = null; state.cart.clear(); clearPrivateView(); $("workspace-screen").hidden = true; $("login-screen").hidden = false; updateCart();}
function csrf() {return document.cookie.split(";").map(x => x.trim()).find(x => x.startsWith("br_csrf="))?.slice(8) || "";}
async function api(path, body) {
  const epoch = state.epoch;
  let response;
  try {response = await fetch(path, {method: body === undefined ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers: body === undefined ? {} : {"Content-Type": "application/json", "X-CSRF-Token": csrf()}, body: body === undefined ? undefined : JSON.stringify(body)});}
  catch (_) {throw new Error("连接中断。尚未确认操作结果；恢复后可重试，服务端会核对重复请求。");}
  const value = await response.json();
  // 空间切换/退出后的迟到响应必须丢弃，不能把上一家公司资料画到当前空间。
  if (epoch !== state.epoch) {const stale = new Error("界面范围已变化。"); stale.discarded = true; throw stale;}
  if (!response.ok) {const code = value.error?.code || "request_failed"; const error = new Error(errors[code] || (response.status === 401 ? "登录已失效，请重新登录。" : response.status === 503 ? "服务暂时不可用，已有记录仍保留，请稍后重试。" : response.status === 409 ? "记录或版本已变化，请刷新后重新核对。" : "操作未完成，请检查输入和当前权限。")); error.code = code; error.status = response.status; if (response.status === 401 && path !== "/api/login") {const hadSession = Boolean(state.session); showLogin(); if (hadSession) $("login-error").textContent = error.message;} throw error;}
  return value;
}
function path(resource) {return `/api/workspaces/${encodeURIComponent(state.workspace.workspace_id)}/${resource}`;}
function canWrite() {return state.workspace && ["admin", "member"].includes(state.workspace.role);}
async function run(button, action) {if (button) button.disabled = true; try {await action();} catch (error) {if (!error.discarded) notice(error.message, true);} finally {if (button) button.disabled = false; updateCart();}}
function actionButton(text, action, cls) {const button = el("button", text, cls); button.type = "button"; button.addEventListener("click", () => run(button, action)); return button;}
function key() {return crypto.randomUUID();}
// 网络结果未知时保留同一请求的key，刷新后也可重放；这里只存请求指纹与随机key，
// 不把企业正文、密码或会话令牌放进Web Storage。成功才移除该待定命令。
async function command(resource, data) {
  const epoch = state.epoch;
  const endpoint = path(resource);
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify([state.session.user.id, endpoint, data]))))).map(b => b.toString(16).padStart(2, "0")).join("");
  if (epoch !== state.epoch) {const stale = new Error("界面范围已变化。"); stale.discarded = true; throw stale;}
  const storageKey = "bidradar-command-" + hash;
  let commandKey = sessionStorage.getItem(storageKey);
  if (!commandKey) {commandKey = key(); sessionStorage.setItem(storageKey, commandKey);}
  // 失败不换key；改过的请求内容自然具有不同指纹，成功才清理。
  const result = await api(endpoint, {...data, key: commandKey}); sessionStorage.removeItem(storageKey); return result;
}
function empty(container, title, text) {const box = el("div", null, "empty"); box.append(el("strong", title), el("p", text)); container.append(box);}
function dateText(value) {if (!value) return "尚未确认"; return value.local.replace("T", " ") + (value.timezone === "+08:00" ? "（北京时间）" : "（原文时区未确认）");}
// 金额在契约中是十进制字符串；展示也不经过浮点数，避免大金额的小数被舍入。
function factText(fact) {if (!fact || fact.status !== "known") return statusNames[fact?.status] || "尚未确认"; if (fact.value?.amount !== undefined) {const [whole, fraction] = fact.value.amount.split("."); return whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (fraction ? "." + fraction : "") + " 元";} if (fact.value?.local) return dateText(fact.value); return String(fact.value);}
function localTime(value) {if (!value) return "—"; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", {hour12: false});}
function safeLink(label, value) {try {const url = new URL(value); if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return el("span", "来源链接待核对"); const a = el("a", label); a.href = url.href; a.target = "_blank"; a.rel = "noopener noreferrer"; return a;} catch (_) {return el("span", "来源链接待核对");}}

async function loadSession() {
  const previousWorkspace = state.workspace?.workspace_id;
  state.session = await api("/api/session");
  $("login-screen").hidden = true; $("workspace-screen").hidden = false;
  $("account-name").textContent = state.session.user.display_name;
  const select = $("workspace-select"); select.replaceChildren();
  for (const workspace of state.session.workspaces) {const option = el("option", workspace.name); option.value = workspace.workspace_id; select.append(option);}
  if (!state.session.workspaces.length) {state.workspace = null; state.profile = null; state.cart.clear(); clearPrivateView(); $("role-badge").textContent = "没有可访问的公司空间"; for (const name of ["discover", "profile", "selections", "members", "research", "tracking"]) $(name + "-view").hidden = true; notice("当前账号没有有效公司成员关系。", true); return;}
  if (state.session.workspaces.some(w => w.workspace_id === previousWorkspace)) select.value = previousWorkspace;
  await switchWorkspace(select.value);
}
async function switchWorkspace(id) {
  state.epoch += 1; $("detail-dialog").close();
  clearPrivateView();
  state.workspace = state.session.workspaces.find(w => w.workspace_id === id); state.cart.clear(); state.profile = null;
  $("role-badge").textContent = roleNames[state.workspace.role]; $("notice").hidden = true; updateCart();
  await loadProfile(); await showView(state.view);
}
async function showView(view) {
  stopRunPolling(); state.runVersion += 1;
  state.view = view;
  const titles = {discover: "商机目录", profile: "企业档案", selections: "已保存选择", members: "公司成员", research: "AI 研究与追问", tracking: "决定与持续跟踪"};
  $("page-title").textContent = titles[view];
  for (const name of Object.keys(titles)) $(name + "-view").hidden = name !== view;
  for (const button of document.querySelectorAll("[data-view]")) button.classList.toggle("active", button.dataset.view === view);
  if (!state.workspace) return;
  if (view === "discover") await search(false);
  if (view === "profile") await loadProfile();
  if (view === "selections") await loadSelections();
  if (view === "members") await loadMembers();
  if (view === "research") await loadResearch();
  if (view === "tracking") await loadTracking();
}
async function search(next) {
  const searchVersion = ++state.searchVersion;
  if (!next) {state.cursor = null; state.query = {keyword: $("keyword").value.trim(), kind: $("kind").value, simulation: $("data-mode").value, page_size: "8"};}
  const params = new URLSearchParams(state.query); if (next && state.cursor) params.set("cursor", state.cursor);
  const list = $("notice-list"); list.replaceChildren(el("p", "正在读取目录…", "muted"));
  try {
    const result = await api(path("notices") + "?" + params);
    if (searchVersion !== state.searchVersion) return;
    list.replaceChildren(); state.cursor = result.next_cursor; $("next-page").hidden = !state.cursor;
    $("total-count").textContent = result.total; $("data-label").textContent = result.simulation ? "虚构演示样本" : "已归档公开公告";
    $("as-of").textContent = "状态核对时间：" + localTime(result.as_of);
    $("source-status").textContent = result.recent_runs.length ? "最近导入情况：" + result.recent_runs.slice(0, 3).map(r => `${r.source_id === "cn_ccgp" ? "中国政府采购网" : r.source_id === "cn_hainan" ? "海南公共资源交易" : r.source_id} · ${r.observations} 条公告${r.failures ? " · 有获取缺口" : ""}`).join("；") + "。这是本地归档范围，不表示实时或全国覆盖。" : "当前范围尚无来源导入记录。";
    if (!result.items.length) empty(list, "当前筛选下没有记录", "可以调整关键词或公告类型；这不表示全网没有商机。");
    for (const item of result.items) {
      const card = el("article", null, "opportunity"), body = el("div"), tags = el("div", null, "tags");
      tags.append(badge(kindNames[item.notice_type]), badge(item.simulation ? "虚构样例" : "真实公开公告", item.simulation ? "warning" : "neutral"), badge(statusNames[item.currentness.status] || "状态待核实", "neutral"));
      body.append(tags, el("h3", item.title || "未取得标题"));
      const meta = el("div", null, "card-meta"); meta.append(el("span", "预算：" + factText(item.facts.budget)), el("span", "响应截止：" + factText(item.facts.response_deadline))); body.append(meta);
      const actions = el("div", null, "card-actions"); actions.append(actionButton("查看证据", () => showDetail(item.notice_id)));
      const choose = actionButton(state.cart.has(item.notice_id) ? "移出选择" : "加入选择", async () => {if (state.cart.has(item.notice_id)) state.cart.delete(item.notice_id); else if (state.cart.size >= 20) throw new Error("一次最多选择 20 个项目。"); else state.cart.set(item.notice_id, {notice_id: item.notice_id, observation_id: item.observation_id}); choose.textContent = state.cart.has(item.notice_id) ? "移出选择" : "加入选择"; updateCart();}); choose.disabled = !canWrite(); actions.append(choose); card.append(body, actions); list.append(card);
    }
  } catch (error) {if (error.discarded || searchVersion !== state.searchVersion) return; list.replaceChildren(); empty(list, "目录暂时无法读取", error.message); $("next-page").hidden = true; $("total-count").textContent = "—"; throw error;}
}
function updateCart() {$("cart-label").textContent = state.cart.size ? `待保存 ${state.cart.size} 个项目` : "尚未选择项目"; $("prepare-selection").disabled = !canWrite() || !state.cart.size;}
function evidenceBlock(title, evidence) {const section = el("details"); section.append(el("summary", `${title}（${evidence.length} 条依据）`)); if (!evidence.length) section.append(el("p", "尚未取得可用于这一项的原文依据。", "muted")); for (const entry of evidence) {const quote = el("div", entry.text, "quote"); quote.append(el("small", `${entry.label || "原文"} · 位置：${entry.locator}`)); section.append(quote);} return section;}
async function showDetail(id) {
  const result = await api(path("notices/" + id)), item = result.current, container = $("detail-content"); container.replaceChildren();
  container.append(badge(kindNames[item.notice_type]), el("h2", item.title), safeLink("打开来源原文 ↗", item.source_url), el("p", "材料获取时间：" + localTime(item.observed_at) + " · " + (item.simulation ? "虚构演示资料" : "真实公开资料"), "muted"));
  const facts = el("dl", null, "facts");
  for (const [name, title] of [["project_number", "项目编号"], ["buyer", "采购人"], ["budget", "预算与口径"], ["response_deadline", "响应截止"], ["published_at", "公告日期"], ["lot_identifier", "标段标识"]]) {const box = el("div"); box.append(el("dt", title), el("dd", factText(item.facts[name]))); facts.append(box);} container.append(facts);
  container.append(el("p", (statusNames[result.currentness.status] || "当前状态待核实") + "。公告日期和原截止时间不代表项目现在一定可报名；资格与交付条件需要逐项核查。", "note"));
  for (const [name, title] of [["budget", "金额原文与冲突依据"], ["response_deadline", "截止时间依据"]]) container.append(evidenceBlock(title, item.facts[name]?.evidence || []));
  const evidence = item.evidence_fields;
  if (evidence) {
    for (const [name, title] of [["technical_evidence", "技术与需求"], ["qualification_evidence", "资格条件"], ["delivery_evidence", "交付要求"], ["category_evidence", "分类依据"]]) container.append(evidenceBlock(title, evidence[name] || []));
    container.append(evidenceBlock("采购文件获取时间", evidence.acquisition_window?.evidence || []));
    container.append(evidenceBlock("登录、申请或付费等获取门槛", (evidence.access_conditions || []).flatMap(x => x.evidence)));
  }
  const materials = el("details"); materials.open = true; materials.append(el("summary", "材料清单与缺口"));
  const references = evidence?.material_availability?.references || [];
  if (!references.length) materials.append(el("p", "尚未取得可下载材料的有效清单，不能据此认为标书齐全。", "muted"));
  for (const ref of references) {const row = el("div", null, "record"); row.append(el("p", ref.name || "未命名材料"), badge(ref.status === "fetched" ? "已归档此文件" : "尚未取得", ref.status === "fetched" ? "" : "warning")); if (ref.url) row.append(el("span", " "), safeLink("来源链接 ↗", ref.url)); materials.append(row);} container.append(materials);
  const history = el("details"); history.append(el("summary", `历史观察与关联（${result.history.length} 个版本）`)); for (const observation of result.history) history.append(el("p", localTime(observation.observed_at) + " · " + observation.observation_id.slice(0, 12), "muted"));
  history.append(el("p", result.relationships.length ? `发现 ${result.relationships.length} 条待核对的公告关系；更正和结果各自保留身份，不覆盖原公告。` : "尚无已建立的关联证据。", "muted")); container.append(history);
  if (analysisEnabled()) container.append(actionButton("记录决定 / 设置关注", async () => {$("detail-dialog").close(); await openTracking(item.notice_id, item.title, item.simulation);}));
  $("detail-dialog").showModal();
}

async function loadProfile() {
  state.profile = await api(path("profile")); const current = state.profile.current; const revision = current?.revision || 0;
  $("profile-label").textContent = revision ? `已确认 · 第 ${revision} 版` : "尚未确认"; $("profile-version").textContent = revision ? `当前第 ${revision} 版` : "尚未确认";
  const form = $("profile-form"); form.replaceChildren();
  for (const [name, title, placeholder] of profileFields) {const label = el("label", title); const input = el(name === "company_name" || name === "city" ? "input" : "textarea"); input.name = name; input.id = "profile-" + name; input.placeholder = placeholder; input.value = current?.payload?.[name] || ""; input.maxLength = name === "company_name" || name === "city" ? 200 : 4000; input.disabled = !canWrite(); if (name === "company_name") input.required = true; label.append(input); form.append(label);}
  const submit = el("button", "提交档案修改建议", "primary full"); submit.type = "submit"; submit.disabled = !canWrite(); form.append(submit);
  const proposals = $("proposals"); proposals.replaceChildren(); const pending = state.profile.proposals.filter(p => p.status === "proposed");
  if (!pending.length) proposals.append(el("p", "暂无待确认建议。", "muted"));
  for (const proposal of pending) {const record = el("div", null, "record"); record.append(el("p", `${proposal.payload.company_name} · 基于第 ${proposal.base_revision} 版`), el("p", "提交于 " + localTime(proposal.created_at), "muted")); const details = el("details"); details.append(el("summary", "查看建议内容")); for (const [name, title] of profileFields) details.append(el("p", `${title}：${proposal.payload[name] || "尚未确认"}`, "long-text")); record.append(details);
    if (state.workspace.role === "admin") {const confirm = actionButton("确认并生成正式版本", async () => {await command("profile/confirm", {proposal_id: proposal.id, expected_revision: revision}); notice("正式档案已生成新版本，历史内容保持不变。"); await loadProfile();}, "primary"); confirm.disabled = proposal.base_revision !== revision; record.append(confirm); if (proposal.base_revision !== revision) record.append(el("p", "这条建议基于旧档案，请重新提出建议。", "muted"));} else record.append(el("p", "等待公司管理员确认。", "muted")); proposals.append(record);}
  const history = $("profile-history"); history.replaceChildren();
  if (!state.profile.history.length) history.append(el("p", "尚无正式档案版本。", "muted"));
  for (const version of state.profile.history) {const details = el("details"); details.append(el("summary", `第 ${version.revision} 版 · ${localTime(version.confirmed_at || version.created_at)}`)); for (const [name, title] of profileFields) details.append(el("p", `${title}：${version.payload[name] || "尚未确认"}`, "long-text")); details.append(el("p", "证明未提供 · 读取未尝试 · 有效性未核查", "muted")); history.append(details);}
}
async function loadSelections() {
  const result = await api(path("selections")); const list = $("selection-list"); list.replaceChildren();
  if (!result.items.length) empty(list, "还没有保存选择", "到商机目录加入项目，保存后再明确确认。");
  const names = {awaiting_decision: "待明确确认", selected: "已明确选定", cancelled: "已取消"};
  for (const selection of result.items) {const record = el("article", null, "record"); const tags = el("div", null, "tags"); tags.append(badge(names[selection.state] || selection.state, selection.state === "awaiting_decision" ? "warning" : "neutral"), el("span", `档案第 ${selection.profile_revision} 版 · ${selection.items.length} 个项目`, "muted")); record.append(tags);
    for (const item of selection.items) {
      const row = el("div", null, "selection-project"); row.append(el("p", item.title || item.notice_id), badge(item.simulation ? "虚构演示资料" : "真实公开样本", item.simulation ? "warning" : "neutral"));
      if (selection.state === "selected" && analysisEnabled()) {
        const actions = el("div", null, "record-actions");
        const agent = actionButton("新建 AI 研究", () => createAnalysis(selection, item, "agent"), "primary");
        const baseline = actionButton("生成规则基线", () => createAnalysis(selection, item, "baseline"));
        agent.disabled = baseline.disabled = !canWrite();
        actions.append(agent, baseline, actionButton("人工决定 / 关注", () => openTracking(item.notice_id, item.title, item.simulation))); row.append(actions);
      }
      record.append(row);
    } record.append(el("p", "保存于 " + localTime(selection.created_at) + " · 目录核对于 " + localTime(selection.catalog_checked_at), "muted"));
    if (selection.state === "awaiting_decision" && canWrite()) {const buttons = el("div", null, "record-actions"); buttons.append(actionButton("确认这些项目", async () => {await command(`selections/${selection.id}/confirm`, {expected_version: selection.version}); notice("项目已明确选定。当前没有创建分析任务，也没有模型费用。"); await loadSelections();}, "primary"), actionButton("取消这次选择", async () => {await command(`selections/${selection.id}/cancel`, {expected_version: selection.version}); notice("选择已取消，历史记录保留。"); await loadSelections();})); record.append(buttons);} list.append(record);}
}
async function loadMembers() {
  const result = await api(path("members")); const list = $("member-list"); list.replaceChildren();
  for (const member of result.items) {const row = el("div", null, "member-row"), info = el("div"); info.append(el("strong", member.display_name), el("p", member.username + (member.active ? " · 可访问" : " · 已撤权"), "muted")); row.append(info);
    if (state.workspace.role === "admin") {const select = el("select"); select.setAttribute("aria-label", member.display_name + "的角色"); for (const [role, label] of Object.entries(roleNames)) {const option = el("option", label); option.value = role; select.append(option);} select.value = member.role; row.append(select, actionButton("保存角色", async () => {await api(path("members/change"), {target_user_id: member.user_id, role: select.value, active: member.active, expected_version: member.version}); notice("成员角色已更新，下一次操作按新权限检查。"); await loadSession();}), actionButton(member.active ? "撤销访问" : "恢复访问", async () => {await api(path("members/change"), {target_user_id: member.user_id, role: member.role, active: !member.active, expected_version: member.version}); notice("成员访问状态已更新。"); await loadSession();}));} else row.append(badge(roleNames[member.role], "neutral")); list.append(row);}
}

// R1/R2只通过当前公司的网关路径读取；来源文字、模型输出和diff都当作数据展示。
const runStates = {queued: "已保存 · 等待执行", running: "正在研究", waiting_input: "等待人工处理 · 自动执行已停止", retry_wait: "等待受控恢复", succeeded: "研究已完成", partial: "部分完成 · 仍有缺口", failed: "研究失败", cancelled: "已取消", pending: "等待派送", accepted: "研究服务已接受", deferred: "依赖失败 · 等待人工重试", rejected: "复核未获接受"};
const findingStates = {met: "证据支持满足", unmet: "证据支持不满足", unknown: "尚未确认", conflicting: "依据存在冲突", not_applicable: "不适用"};
const decisionStates = {needs_review: "需要进一步核查", follow_up: "人工决定跟进", dismissed: "人工决定放弃"};
Object.assign(errors, {dependency_unavailable: "依赖服务暂时不可用，已保存记录仍保留，请稍后重试。", research_unavailable: "研究服务尚不可用，未确认任务结果，请保留当前请求重试。", tracking_unavailable: "跟踪服务尚不可用，请稍后重试。", quota_exceeded: "可用额度不足，未继续调用模型；已有报告仍可查看。", budget_exceeded: "达到当前费用保护值，未继续调用模型。", delegation_invalid: "自动复核委托已失效，请核对公司权限和关注设置后重新授权。", watch_version_conflict: "关注设置已被修改，请刷新后重新确认。", decision_version_conflict: "人工决定已被修改，请刷新后重新确认。", reassessment_version_conflict: "复核状态已变化，请刷新后再操作。"});
function analysisEnabled() {return state.session?.analysis_enabled === true;}
function valueText(value) {if (value === null || value === undefined) return "尚未记录"; return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);}
function jsonDetails(title, value) {const box = el("details"); box.append(el("summary", title), el("pre", valueText(value), "json-evidence")); return box;}
function modeBadge(mode) {return badge(mode === "baseline" ? "规则基线 · 非 AI" : mode === "agent" ? "AI Agent" : "模式待核对", mode === "baseline" ? "neutral" : "");}
function sampleBadge(simulation) {return badge(simulation === true ? "虚构演示资料" : simulation === false ? "真实公开样本" : "资料范围待核对", simulation === false ? "neutral" : "warning");}
function yuan(value) {
  if (!(typeof value === "number" && Number.isSafeInteger(value) && value >= 0) && !(typeof value === "string" && /^\d+$/.test(value))) return "未知";
  const amount = BigInt(value); return "¥ " + (amount / 1000000n).toString() + "." + (amount % 1000000n).toString().padStart(6, "0");
}
function stopRunPolling() {if (state.pollTimer !== null) clearTimeout(state.pollTimer); state.pollTimer = null;}
async function createAnalysis(selection, item, mode) {
  if (!analysisEnabled() || !canWrite()) throw new Error("当前不能新建研究任务。");
  const result = await command("research/analyses", {selection_id: selection.id, notice_id: item.notice_id, mode});
  state.runId = result.id;
  notice(`研究任务已持久保存：${result.id}。刷新或退出页面不会重复新建任务。`);
  await showView("research");
}
async function loadBudget() {
  const container = $("research-budget");
  try {
    const result = await api(path("research/budget")); container.replaceChildren();
    const values = [["本轮费用保护值", yuan(result.cap)], ["公司保护值", yuan(result.workspace_cap)], ["单任务保护值", yuan(result.run_cap)], ["已计入 / 已预留", yuan(result.charged_or_reserved)], ["调用记录 / 费用未知", `${valueText(result.attempts)} / ${valueText(result.unknown_attempts)}`], ["输入 / 输出 tokens", `${valueText(result.input_tokens)} / ${valueText(result.output_tokens)}`]];
    for (const [title, value] of values) {const box = el("div"); box.append(el("span", title), el("strong", value)); container.append(box);}
    container.append(el("p", "费用口径：" + valueText(result.cost_basis) + "。这里包含必要预留，不代表供应商已结算账单。", "muted budget-basis"));
  } catch (error) {if (error.discarded) return; container.replaceChildren(el("p", "费用信息暂时不可用；不能把未知费用视为零。" + error.message, "error"));}
}
function latestRunStatus(run) {
  // 同公司内只记状态/版本；详情先完成时，晚到的旧列表不能把终态降回queued。
  const previous = state.runStatuses.get(run.id), version = Number.isInteger(run.version) ? run.version : 0;
  if (!previous || version >= previous.version) state.runStatuses.set(run.id, {state: run.state, version});
  return state.runStatuses.get(run.id).state;
}
function runStatusBadge(status) {return badge(runStates[status] || status, "run-state " + (["failed", "partial", "waiting_input"].includes(status) ? "warning" : "neutral"));}
function syncRunListStatus(run) {
  const status = latestRunStatus(run);
  for (const card of $("research-list").querySelectorAll("[data-run-id]")) {
    if (card.dataset.runId === run.id) card.querySelector(".run-state")?.replaceWith(runStatusBadge(status));
  }
}
function waitingInputMessage(run) {
  return run.reason === "billing_unknown" ? "本次调用是否计费尚未核清，已停止自动执行。请先人工核对供应商账单与用量；系统不会为此换键重试收费调用。" : "此任务已停止自动执行，需要人工确认原因与下一步；刷新不会恢复或新建调用。";
}
async function loadRunList(more = false) {
  const version = ++state.runListVersion, list = $("research-list");
  const params = new URLSearchParams({limit: "30"}); if (more && state.runBefore !== null) params.set("before", state.runBefore);
  const result = await api(path("research/runs") + "?" + params);
  if (version !== state.runListVersion) return;
  if (!more) list.replaceChildren();
  state.runBefore = result.next_before ?? null; $("more-research").hidden = state.runBefore === null;
  if (!result.items?.length && !more) empty(list, "尚无研究任务", "先确认候选选择，再逐个项目明确发起研究。");
  for (const item of result.items || []) {
    const card = el("article", null, "run-card"); card.dataset.runId = item.id;
    card.append(modeBadge(item.mode), sampleBadge(item.simulation), el("h3", item.title || item.notice_id || item.id), runStatusBadge(latestRunStatus(item)));
    card.append(el("p", `${item.kind === "question" ? "同项目追问" : item.kind === "reassessment" ? "变化复核" : "项目研究"} · 档案第 ${item.profile_revision ?? "待核对"} 版`, "muted"), el("p", "任务 " + item.id, "identifier"));
    card.append(actionButton("查看报告与进度", () => viewRun(item.id))); list.append(card);
  }
}
async function loadResearch() {
  if (!analysisEnabled()) {$("research-list").replaceChildren(); empty($("research-list"), "研究服务尚未启用", "当前仍可核查公告、确认档案和保存选择。"); return;}
  // 用量失败不覆盖已保存报告；两个独立只读请求各自呈现结果。
  const results = await Promise.allSettled([loadRunList(), loadBudget()]);
  for (const result of results) if (result.status === "rejected" && !result.reason.discarded) notice(result.reason.message, true);
  if (state.runId && state.view === "research") await viewRun(state.runId);
}
async function viewRun(id, remaining = 20) {
  stopRunPolling(); const version = ++state.runVersion; state.runId = id;
  const run = await api(path("research/runs/" + encodeURIComponent(id)));
  if (version !== state.runVersion || state.view !== "research") return;
  renderRun(run, remaining);
  syncRunListStatus(run);
  // 只轮询已存在的任务；最多20次、每次完成后隔3秒，绝不通过POST重新创建。
  if (["queued", "running", "retry_wait"].includes(run.state) && remaining > 0) {
    state.pollTimer = setTimeout(async () => {
      if (state.view !== "research" || state.runId !== id || version !== state.runVersion) return;
      try {await viewRun(id, remaining - 1);} catch (error) {if (!error.discarded) notice("进度刷新中断，任务仍保留。" + error.message, true);}
    }, 3000);
  }
}
function renderReferences(container, references, wanted) {
  const refs = Array.isArray(references) ? references : [];
  for (const evidenceId of Array.isArray(wanted) ? wanted : []) {
    const reference = refs.find(ref => ref.evidence_id === evidenceId);
    if (!reference) {container.append(el("p", "引用 " + evidenceId + "：当前响应未附原文片段，请回核固定版本。", "muted identifier")); continue;}
    const quote = el("div", reference.text || "当前未返回原文片段", "quote");
    quote.append(el("small", `${evidenceId} · 位置：${reference.locator || "未提供"} · 观察：${reference.observation_id || "未提供"}`));
    if (reference.source_url || reference.url) quote.append(safeLink("打开引用来源 ↗", reference.source_url || reference.url));
    container.append(quote);
  }
}
function renderReport(run, container) {
  const report = run.report; if (!report || typeof report !== "object") {container.append(el("p", "尚无已发布报告。任务状态不能替代业务结论。", "muted")); return;}
  container.append(el("h3", run.kind === "question" ? "同项目回答" : "证据核查报告"));
  if (report.answer) container.append(el("p", valueText(report.answer), "report-summary"));
  if (report.summary) container.append(el("p", valueText(report.summary), "report-summary"));
  container.append(el("p", "当前分析使用已取得的规范证据片段，并未读完完整标书。企业未填信息表示未知；管理员确认不等于资质认证。", "note"));
  for (const finding of Array.isArray(report.findings) ? report.findings : []) {
    const card = el("article", null, "finding");
    card.append(badge(findingStates[finding.status] || "状态待核对", ["unknown", "conflicting", "unmet"].includes(finding.status) ? "warning" : "neutral"), el("h3", finding.requirement || finding.category || "待核查条件"));
    card.append(el("p", valueText(finding.reason), "long-text"));
    if (finding.unknown_reason) card.append(el("p", "未决原因：" + valueText(finding.unknown_reason), "note"));
    if (finding.profile_fields?.length) card.append(el("p", "对照的企业声明字段：" + finding.profile_fields.join("、"), "muted"));
    renderReferences(card, report.references, finding.evidence_ids);
    container.append(card);
  }
  for (const [field, title] of [["questions", "仍需人工确认"], ["coverage", "实际读取范围"], ["limitations", "未取得材料与分析限制"], ["scope", "报告固定范围"]]) {
    if (report[field] !== undefined && report[field] !== null) container.append(jsonDetails(title, report[field]));
  }
  if (Array.isArray(report.references) && report.references.length) {
    const all = el("details"); all.append(el("summary", `全部固定引用（${report.references.length}）`));
    renderReferences(all, report.references, report.references.map(ref => ref.evidence_id)); container.append(all);
  }
}
function renderRun(run, remaining) {
  const container = $("research-detail"); container.replaceChildren();
  const tags = el("div", null, "tags"); tags.append(modeBadge(run.mode), sampleBadge(run.simulation), badge(runStates[run.state] || run.state, ["failed", "partial", "waiting_input"].includes(run.state) ? "warning" : "neutral"));
  container.append(tags, el("h2", run.title || run.notice_id || "研究任务"), el("p", "任务编号：" + run.id, "identifier"));
  container.append(el("p", `档案第 ${run.profile_revision ?? "待核对"} 版 · ${run.kind === "question" ? "同项目追问" : run.kind === "reassessment" ? "变化复核" : "固定输入研究"}`, "muted"));
  if (run.reason) container.append(el("p", "当前原因：" + (errors[run.reason] || valueText(run.reason)), "note"));
  if (run.state === "waiting_input") container.append(el("p", waitingInputMessage(run), "note"));
  // 当前性单独核对；冻结报告正文永远不随新档案或新公告静默改变。
  const freshnessNames = {current: "与当前输入一致", changed: "输入已有变化", unavailable: "暂无法核对", not_checked: "尚未核对"};
  const freshness = run.freshness || {};
  container.append(el("p", `报告当前性：企业档案 ${freshnessNames[freshness.profile] || "尚未核对"}；公告 ${freshnessNames[freshness.catalog] || "尚未核对"}。`, "muted"));
  if (Object.values(freshness).includes("changed")) container.append(el("p", "这份报告固定使用旧输入。请明确发起新分析或查看变化复核，旧报告内容保持原样。", "note"));
  if (Object.values(freshness).includes("unavailable")) container.append(el("p", "当前依赖暂不可用，无法确认输入是否仍是当前版本。", "note"));
  if (run.parent_run_id) container.append(actionButton("查看父报告", () => viewRun(run.parent_run_id)));
  const actions = el("div", null, "record-actions"); actions.append(actionButton("刷新此任务", () => viewRun(run.id)));
  if (run.notice_id) actions.append(actionButton("人工决定 / 关注", () => openTracking(run.notice_id, run.title, run.simulation)));
  if (canWrite() && !["succeeded", "partial", "failed", "cancelled", "waiting_input"].includes(run.state)) actions.append(actionButton("明确取消任务", async () => {await command(`research/runs/${run.id}/cancel`, {}); notice("取消已登记，将阻止后续动作；在途调用及费用不保证撤回。"); await viewRun(run.id); }));
  container.append(actions, el("p", ["queued", "running", "retry_wait"].includes(run.state) ? (remaining > 0 ? `只读刷新中，剩余最多 ${remaining} 次；退出页面不会取消任务。` : "本轮自动刷新已停止，可手动继续刷新。后台任务仍按已保存状态执行。") : "此页只读取已保存结果；重新加载不会新建分析。", "muted"));
  renderReport(run, container);
  if (run.scope) container.append(jsonDetails("输入版本与公告范围", run.scope));
  if (run.quality) container.append(jsonDetails("引用与质量核验", run.quality));
  if (run.cost || run.usage || run.budget) container.append(jsonDetails("本任务用量与费用记录", {cost: run.cost ?? null, usage: run.usage ?? null, budget: run.budget ?? null}));
  const trace = el("details"); trace.append(el("summary", "查看 Agent 工具轨迹"));
  const steps = Array.isArray(run.trace) ? run.trace : Array.isArray(run.trace?.steps) ? run.trace.steps : [];
  if (!steps.length) trace.append(el("p", run.mode === "baseline" ? "此任务是规则基线，没有模型工具选择轨迹。" : "尚无可展示的已保存工具动作。", "muted"));
  steps.forEach((step, index) => {
    // 只展示工具审计字段，原始模型思维不是产品内容，不能整份渲染供应商响应。
    const audit = {}; for (const name of ["tool", "name", "tool_name", "arguments", "input", "status", "result", "result_summary", "evidence_ids", "step", "event", "kind"]) if (step && Object.hasOwn(step, name)) audit[name] = step[name];
    trace.append(jsonDetails(`动作 ${index + 1} · ${step?.tool || step?.name || step?.tool_name || step?.event || "已记录步骤"}`, audit));
  }); container.append(trace);
  if (run.report && ["succeeded", "partial"].includes(run.state)) {
    const form = el("form", null, "question-form"), label = el("label", "围绕这份固定报告继续追问"), input = el("textarea"); input.required = true; input.maxLength = 2000; input.placeholder = "例如：哪些交付条件还需要我们补充确认？"; input.disabled = !canWrite(); label.append(input);
    const submit = el("button", "提交同项目追问", "primary"); submit.type = "submit"; submit.disabled = !canWrite();
    form.append(label, el("p", "追问会新建持久任务并可能产生模型费用，沿用这份报告的项目与输入版本。", "muted"), submit);
    form.addEventListener("submit", event => {event.preventDefault(); runActionQuestion(run, input.value, submit);}); container.append(form);
  }
}
function runActionQuestion(parent, question, button) {
  run(button, async () => {const clean = question.trim(); if (!clean) throw new Error("请先填写追问内容。"); const result = await command("research/questions", {parent_run_id: parent.id, question: clean}); notice("追问任务已保存：" + result.id); await viewRun(result.id); await loadRunList(); await loadBudget();});
}
async function openTracking(noticeId, title, simulation) {
  state.trackedNotice = {notice_id: noticeId, title: title || noticeId, simulation}; await showView("tracking");
}
async function loadTrackingContext(version) {
  const container = $("tracking-context"), target = state.trackedNotice;
  if (!target) {container.replaceChildren(); empty(container, "先指定一个公告", "可从公告详情、已选项目或研究报告进入。下面仍可查看本公司的提醒与变化。"); return;}
  container.replaceChildren(el("p", "正在读取这条公告的决定与关注…", "muted"));
  const [decision, watched] = await Promise.all([api(path("tracking/decisions/" + target.notice_id)), api(path("tracking/watches/" + target.notice_id))]);
  if (version !== state.trackingVersion) return;
  container.replaceChildren(sampleBadge(target.simulation), el("h3", target.title), el("p", "公告 " + target.notice_id, "identifier"));
  const current = decision.current, watch = watched.watch, layout = el("div", null, "tracking-layout");
  const decisionForm = el("form", null, "decision-form"), selectLabel = el("label", "人工决定"), select = el("select");
  for (const [value, label] of Object.entries(decisionStates)) {const option = el("option", label); option.value = value; select.append(option);} select.value = current?.state || "needs_review"; select.disabled = !canWrite(); selectLabel.append(select);
  const reasonLabel = el("label", "决定原因"), reason = el("textarea"); reason.value = current?.reason || ""; reason.maxLength = 4000; reason.disabled = !canWrite(); reasonLabel.append(reason);
  const save = el("button", "保存人工决定", "primary"); save.type = "submit"; save.disabled = !canWrite();
  decisionForm.append(el("h3", `人工记录 · 第 ${current?.version || 0} 版`), selectLabel, reasonLabel, el("p", "人工决定不是资质认证，也不会自动开启或关闭关注。", "muted"), save);
  decisionForm.addEventListener("submit", event => {event.preventDefault(); run(save, async () => {await command("tracking/decisions/" + target.notice_id, {state: select.value, reason: reason.value.trim(), expected_version: current?.version || 0}); notice("人工决定已保存，关注开关未改变。"); await loadTracking();});});
  if (decision.history?.length) decisionForm.append(jsonDetails("人工决定历史", decision.history));
  const watchForm = el("form", null, "watch-form"), activeLabel = el("label", null, "check-label"), active = el("input"); active.type = "checkbox"; active.checked = watch?.active === true; active.disabled = !canWrite(); activeLabel.append(active, el("span", "开启公告与档案变化跟踪"));
  const autoLabel = el("label", null, "check-label"), auto = el("input"); auto.type = "checkbox"; auto.checked = false; auto.disabled = !canWrite() || !active.checked; autoLabel.append(auto, el("span", "我明确授权：发现变化后自动发起 AI 复核"));
  active.addEventListener("change", () => {auto.disabled = !canWrite() || !active.checked; if (!active.checked) auto.checked = false;});
  const saveWatch = el("button", "保存关注设置", "primary"); saveWatch.type = "submit"; saveWatch.disabled = !canWrite();
  watchForm.append(el("h3", `关注设置 · 第 ${watch?.version || 0} 版`), badge(watch?.active ? "当前正在跟踪" : "当前未跟踪", "neutral"), el("p", watch?.auto_reassess ? "现有自动复核委托到期：" + localTime(watch.expires_at) : "当前没有自动复核委托。", "muted"), activeLabel, autoLabel,
    el("p", "本次授权最多 7 天，每次复核仍检查成员权限、固定输入和剩余额度，可能产生模型费用。默认不勾选；保存时会按本次勾选重新设置委托。可随时停止关注，历史记录保留。", "note"), saveWatch);
  watchForm.addEventListener("submit", event => {event.preventDefault(); run(saveWatch, async () => {await command("tracking/watches/" + target.notice_id, {active: active.checked, auto_reassess: active.checked && auto.checked, expected_version: watch?.version || 0, expires_at: null, scope: null}); notice("关注设置已保存，人工决定未改变。"); await loadTracking();});});
  if (watch?.active && canWrite()) watchForm.append(actionButton("立即停止关注", async () => {await command("tracking/watches/" + target.notice_id, {active: false, auto_reassess: false, expected_version: watch.version, expires_at: null, scope: null}); notice("关注已停止，旧委托失效；已在途的外部调用及费用不保证撤回。"); await loadTracking();}));
  if (watch?.warnings?.length) watchForm.append(el("p", "跟踪限制：" + watch.warnings.map(value => value === "unstable_content_snapshot_identity" ? "来源使用内容快照身份，无法保证连续版本归为同一公告" : value).join("；"), "note"));
  layout.append(decisionForm, watchForm); container.append(layout);
}
async function loadChanges(more = false, version = state.trackingVersion) {
  const after = more ? state.changeAfter : 0, result = await api(path(`tracking/changes?after=${after}&limit=20`));
  if (version !== state.trackingVersion) return;
  const container = $("tracking-changes"); if (!more) {container.replaceChildren(); state.changeTargets.clear();}
  state.changeAfter = result.next_after; $("more-changes").hidden = result.next_after >= result.high_watermark;
  if (!result.items?.length && !more) empty(container, "暂无已记录变化", "只表示当前已消费的归档版本没有新变化，不保证上游实时同步。");
  for (const change of result.items || []) {
    state.changeTargets.set(change.watch_id, change.notice_id);
    const row = el("article", null, "record"), names = {material_change: "公告事实或证据发生变化", unclassified_content_change: "原文变化 · 尚未分类", profile_change: "企业档案有新版本", no_business_change: "已比较字段未变"};
    row.append(badge(change.replay ? "历史回放" : "归档版本变化", "neutral"), el("h3", names[change.diff?.kind] || "有待核查的变化"), el("p", `${localTime(change.created_at)} · 档案第 ${change.profile_revision} 版 · 目录快照 ${change.catalog_snapshot}`, "muted"));
    row.append(el("p", "旧报告可能已陈旧，请核对新的证据与输入范围；原报告不会被覆盖。", "note"));
    if (change.diff?.kind === "unclassified_content_change") row.append(el("p", "原文指纹已经变化；现有证据片段不足以证明只是排版变化。", "muted"));
    row.append(jsonDetails("新旧差异与固定观察引用", change.diff));
    row.append(actionButton("查看这条公告的决定与关注", () => openTracking(change.notice_id, change.notice_id, undefined))); container.append(row);
  }
}
async function loadNotifications(more = false, version = state.trackingVersion) {
  const after = more ? state.notificationAfter : 0, result = await api(path(`tracking/notifications?after=${after}&limit=20`));
  if (version !== state.trackingVersion) return;
  const container = $("tracking-notifications"); if (!more) container.replaceChildren();
  state.notificationAfter = result.next_after; $("more-notifications").hidden = result.next_after >= result.high_watermark;
  if (!result.items?.length && !more) empty(container, "暂无站内提醒", "明确开启关注后，已归档的新变化会在这里留下记录。");
  for (const notification of result.items || []) {
    const row = el("article", null, "record"); row.append(badge(notification.read_at ? "你已读" : "你未读", notification.read_at ? "neutral" : "warning"), el("h3", notification.kind === "reassessment_completed" ? "变化复核已有可读结果" : "关注的资料有新变化"), el("p", localTime(notification.created_at), "muted"));
    if (!notification.read_at) row.append(actionButton("标记我已读", async () => {await api(path(`tracking/notifications/${notification.id}/read`), {}); await loadNotifications();}));
    const noticeId = state.changeTargets.get(notification.watch_id); if (noticeId) row.append(actionButton("查看对应关注", () => openTracking(noticeId, noticeId, undefined)));
    row.append(el("p", "变化记录 " + notification.change_id, "identifier")); container.append(row);
  }
}
async function loadReassessments(version = state.trackingVersion) {
  const result = await api(path("tracking/reassessments")); if (version !== state.trackingVersion) return;
  const container = $("tracking-reassessments"); container.replaceChildren();
  if (!result.items?.length) empty(container, "暂无自动复核任务", "开启关注不等于授权调用模型；需要单独明确授权自动复核。");
  for (const item of result.items || []) {
    const row = el("article", null, "record"); row.append(badge(runStates[item.state] || item.state, "neutral"), el("p", "复核编号 " + item.id, "identifier"));
    if (item.reason) row.append(el("p", "原因：" + (errors[item.reason] || item.reason), "muted"));
    if (item.state === "waiting_input") row.append(el("p", waitingInputMessage(item), "note"));
    if (item.run_id) row.append(actionButton("查看研究任务", async () => {state.runId = item.run_id; await showView("research");}));
    if (canWrite() && item.state === "deferred") row.append(actionButton("用原命令重新对账", async () => {await command(`tracking/reassessments/${item.id}/retry`, {expected_version: item.version}); notice("已用原命令恢复对账，未换键重复创建研究。"); await loadReassessments();}));
    if (canWrite() && ["pending", "accepted", "deferred"].includes(item.state)) row.append(actionButton("取消这次复核", async () => {await command(`tracking/reassessments/${item.id}/cancel`, {expected_version: item.version}); notice("复核取消已保存；在途调用及费用不保证撤回。"); await loadReassessments();}));
    container.append(row);
  }
}
async function loadTracking() {
  if (!analysisEnabled()) {$("tracking-context").replaceChildren(el("p", "研究与跟踪服务尚未启用。", "muted")); return;}
  const version = ++state.trackingVersion;
  const results = await Promise.allSettled([loadTrackingContext(version), loadChanges(false, version), loadReassessments(version)]);
  for (const result of results) if (result.status === "rejected" && !result.reason.discarded) notice(result.reason.message, true);
  if (version === state.trackingVersion) await loadNotifications(false, version);
}

$("login-form").addEventListener("submit", async event => {event.preventDefault(); const button = event.submitter; button.disabled = true; $("login-error").textContent = ""; try {await api("/api/login", {username: $("username").value, password: $("password").value}); $("password").value = ""; await loadSession();} catch (error) {$("login-error").textContent = error.message;} finally {button.disabled = false;}});
$("logout").addEventListener("click", () => run($("logout"), async () => {await api("/api/logout", {}); showLogin(); $("login-error").textContent = "";}));
$("workspace-select").addEventListener("change", () => run(null, () => switchWorkspace($("workspace-select").value)));
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => run(button, () => showView(button.dataset.view)));
$("search-form").addEventListener("submit", event => {event.preventDefault(); run(event.submitter, () => search(false));});
$("next-page").addEventListener("click", () => run($("next-page"), () => search(true)));
$("close-detail").addEventListener("click", () => $("detail-dialog").close());
$("prepare-selection").addEventListener("click", () => run($("prepare-selection"), async () => {if (!state.profile?.current) throw new Error("请先到企业档案提交建议，并由管理员确认正式版本。"); await command("selections", {profile_revision: state.profile.current.revision, items: [...state.cart.values()]}); state.cart.clear(); updateCart(); notice("已保存待确认选择，请核对项目后明确确认。"); await showView("selections");}));
$("profile-form").addEventListener("submit", event => {event.preventDefault(); run(event.submitter, async () => {const payload = {}; for (const [name] of profileFields) payload[name] = $("profile-" + name).value.trim() || null; await command("profile/proposals", {payload, base_revision: state.profile.current?.revision || 0}); notice("建议已保存，等待公司管理员确认。"); await loadProfile();});});
$("refresh-research").addEventListener("click", () => run($("refresh-research"), loadResearch));
$("more-research").addEventListener("click", () => run($("more-research"), () => loadRunList(true)));
$("refresh-tracking").addEventListener("click", () => run($("refresh-tracking"), loadTracking));
$("more-notifications").addEventListener("click", () => run($("more-notifications"), () => loadNotifications(true)));
$("more-changes").addEventListener("click", () => run($("more-changes"), () => loadChanges(true)));
document.addEventListener("visibilitychange", () => {if (document.hidden) stopRunPolling();});
loadSession().catch(error => {if (!["unauthenticated", "authentication_required"].includes(error.code) && !error.discarded) $("login-error").textContent = error.message;});
