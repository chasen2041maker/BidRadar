"use strict";
// 页面只提交稳定ID和版本；可信标题/权限/档案版本由服务端再次核对。
// 所有来源与企业文字用textContent展示，禁止把采购原文当可执行HTML。
const $ = (id) => document.getElementById(id);
const state = {session: null, workspace: null, profile: null, view: "discover", cart: new Map(), cursor: null, query: null, epoch: 0, searchVersion: 0};
const roleNames = {admin: "公司管理员", member: "协作成员", viewer: "只读成员"};
const kindNames = {procurement: "采购公告", correction: "更正公告", award: "中标 / 成交", termination: "终止公告", intention: "采购意向", unknown: "类型待核实"};
const statusNames = {known: "已提取", missing: "尚未取得依据", unparsed: "待核对原文", conflicting: "存在冲突", context_required: "需结合金额口径", unknown: "尚未确认", deadline_passed: "原截止时间已过", deadline_not_reached: "原截止时间未到", not_opening_notice: "非采购报名公告"};
const errors = {unauthenticated: "登录已失效，请重新登录。", invalid_credentials: "账号或密码不正确。", login_limited: "登录尝试过多，请稍后重试。", forbidden: "你目前没有执行此操作的权限，请刷新公司空间。", invalid_csrf: "登录状态已变化，请刷新页面后重试。", profile_version_conflict: "企业档案已有新版本，请重新读取后提交。", profile_revision_conflict: "企业档案已有新版本，请重新读取后提交。", stale_profile: "企业档案已有新版本，请重新准备选择。", catalog_version_changed: "公告已有新版本，请刷新目录，查看新内容后重新选择。", catalog_unavailable: "目录服务暂时不可用，已保存的档案和选择仍保留。", idempotency_conflict: "相同操作标识对应了不同内容，请刷新后重新操作。", version_conflict: "记录已变化，请刷新后重新确认。", last_admin: "公司需要保留至少一位管理员。", last_admin_required: "公司需要保留至少一位管理员。", profile_required: "请先由管理员确认一版企业档案。", not_found: "记录不存在或当前无权访问。"};
const profileFields = [["company_name", "公司名称 / 简称", "例如：山岚软件（虚构）"], ["city", "所在城市", "城市不限定可承接的地区"], ["project_types", "希望承接的项目", "软件定制、业务系统、AI应用；也可注明不接的类型"], ["capabilities", "技术与交付能力", "能负责哪些工作，有哪些技术与交付经验"], ["delivery_constraints", "交付与地域限制", "区分优先远程、可出差与明确不接受的驻场要求"], ["cases", "相关案例", "工作范围、团队角色、时间；尚不接收证明文件"], ["qualifications", "资质与证明声明", "确认有 / 没有 / 尚未确认；文字声明不是核验结果"], ["staffing", "人力与排期", "可投入角色、人数、最早时间与已知冲突"], ["commercial_constraints", "商务偏好与硬限制", "请分别写清偏好和明确不能接受的条件"]];

function el(tag, text, cls) {const node = document.createElement(tag); if (text !== undefined && text !== null) node.textContent = String(text); if (cls) node.className = cls; return node;}
function badge(text, type = "") {return el("span", text, "badge " + type);}
function notice(message, failure = false) {$("notice").textContent = message; $("notice").className = "notice" + (failure ? " failure" : ""); $("notice").hidden = false;}
function clearPrivateView() {
  for (const id of ["profile-form", "proposals", "profile-history", "selection-list", "member-list", "notice-list", "detail-content"]) $(id).replaceChildren();
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
  if (!response.ok) {const code = value.error?.code || "request_failed"; const error = new Error(errors[code] || (response.status === 401 ? "登录已失效，请重新登录。" : response.status === 503 ? "目录服务暂时不可用，请稍后重试。" : response.status === 409 ? "记录或版本已变化，请刷新后重新核对。" : "操作未完成，请检查输入和当前权限。")); error.code = code; error.status = response.status; if (response.status === 401 && path !== "/api/login") {const hadSession = Boolean(state.session); showLogin(); if (hadSession) $("login-error").textContent = error.message;} throw error;}
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
  try {const result = await api(endpoint, {...data, key: commandKey}); sessionStorage.removeItem(storageKey); return result;}
  catch (error) {if (error.status >= 400 && error.status < 500) sessionStorage.removeItem(storageKey); throw error;}
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
  if (!state.session.workspaces.length) {state.workspace = null; state.profile = null; state.cart.clear(); clearPrivateView(); $("role-badge").textContent = "没有可访问的公司空间"; for (const name of ["discover", "profile", "selections", "members"]) $(name + "-view").hidden = true; notice("当前账号没有有效公司成员关系。", true); return;}
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
  state.view = view;
  const titles = {discover: "商机目录", profile: "企业档案", selections: "已保存选择", members: "公司成员"};
  $("page-title").textContent = titles[view];
  for (const name of Object.keys(titles)) $(name + "-view").hidden = name !== view;
  for (const button of document.querySelectorAll("[data-view]")) button.classList.toggle("active", button.dataset.view === view);
  if (!state.workspace) return;
  if (view === "discover") await search(false);
  if (view === "profile") await loadProfile();
  if (view === "selections") await loadSelections();
  if (view === "members") await loadMembers();
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
  const names = {awaiting_decision: "待明确确认", selected: "已选定 · 尚未分析", cancelled: "已取消"};
  for (const selection of result.items) {const record = el("article", null, "record"); const tags = el("div", null, "tags"); tags.append(badge(names[selection.state] || selection.state, selection.state === "awaiting_decision" ? "warning" : "neutral"), el("span", `档案第 ${selection.profile_revision} 版 · ${selection.items.length} 个项目`, "muted")); record.append(tags);
    for (const item of selection.items) {const row = el("p", item.title); record.append(row);} record.append(el("p", "保存于 " + localTime(selection.created_at) + " · 目录核对于 " + localTime(selection.catalog_checked_at), "muted"));
    if (selection.state === "awaiting_decision" && canWrite()) {const buttons = el("div", null, "record-actions"); buttons.append(actionButton("确认这些项目", async () => {await command(`selections/${selection.id}/confirm`, {expected_version: selection.version}); notice("项目已明确选定。当前没有创建分析任务，也没有模型费用。"); await loadSelections();}, "primary"), actionButton("取消这次选择", async () => {await command(`selections/${selection.id}/cancel`, {expected_version: selection.version}); notice("选择已取消，历史记录保留。"); await loadSelections();})); record.append(buttons);} list.append(record);}
}
async function loadMembers() {
  const result = await api(path("members")); const list = $("member-list"); list.replaceChildren();
  for (const member of result.items) {const row = el("div", null, "member-row"), info = el("div"); info.append(el("strong", member.display_name), el("p", member.username + (member.active ? " · 可访问" : " · 已撤权"), "muted")); row.append(info);
    if (state.workspace.role === "admin") {const select = el("select"); select.setAttribute("aria-label", member.display_name + "的角色"); for (const [role, label] of Object.entries(roleNames)) {const option = el("option", label); option.value = role; select.append(option);} select.value = member.role; row.append(select, actionButton("保存角色", async () => {await api(path("members/change"), {target_user_id: member.user_id, role: select.value, active: member.active, expected_version: member.version}); notice("成员角色已更新，下一次操作按新权限检查。"); await loadSession();}), actionButton(member.active ? "撤销访问" : "恢复访问", async () => {await api(path("members/change"), {target_user_id: member.user_id, role: member.role, active: !member.active, expected_version: member.version}); notice("成员访问状态已更新。"); await loadSession();}));} else row.append(badge(roleNames[member.role], "neutral")); list.append(row);}
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
loadSession().catch(error => {if (!["unauthenticated", "authentication_required"].includes(error.code) && !error.discarded) $("login-error").textContent = error.message;});
