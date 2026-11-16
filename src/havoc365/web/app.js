"use strict";
let user = null;
let csrf = "";
let timer = null;
let session = 0;
let revision = 0;
let adminRevision = 0;
let active = false;
let connected = false;
let sending = false;
let lastSeen = null;
let contacts = [];
let contactSignature = "";
let newestMessage = 0;
let panel = "chat";
let incident = null;
let checkins = [];
let administration = null;
let incidentDirty = false;
let contactsDirty = false;
let checkinDirty = false;
let checkinIncident = null;
let editorSignature = null;
let peopleSignature = "";
let invitesSignature = "";
let auditSignature = "";
let draftAttachments = [];
let attachmentSequence = 0;
const busy = new Set();
const element = id => document.getElementById(id);
const mobile = matchMedia("(max-width: 700px)");

class APIError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (body !== undefined && csrf) headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  return readResponse(response);
}

async function readResponse(response) {
  let data;
  try {
    data = await response.json();
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new APIError("Unexpected server response. Check the lifeboat address or reverse proxy.", response.status);
  }
  if (!response.ok) throw new APIError(data.error || `Request failed (${response.status}).`, response.status);
  return data;
}

function expired(error) {
  if (!(error instanceof APIError) || error.status !== 401) return false;
  leave("Your session expired or was revoked. Ask your incident lead for a new invitation, or join again.");
  return true;
}

function updateSendButton() {
  const unfinished = draftAttachments.some(attachment => attachment.state !== "ready");
  element("send-submit").disabled = sending || !connected || !active || unfinished;
  element("send-submit").textContent = sending ? "Sending..." : "Send";
  element("message").required = !draftAttachments.some(attachment => attachment.state === "ready");
  element("attachments").disabled = sending || !connected || !active;
  for (const button of element("draft-attachments").querySelectorAll("button")) {
    button.disabled = sending || button.dataset.busy === "true";
  }
  element("checkin-submit").disabled = !connected || !active || busy.has("checkin-submit");
}

function updateCounter() {
  element("message-counter").textContent = `${element("message").value.length} / 2000`;
  element("message-counter").hidden = !element("message").value.length;
}

function selectPanel(selected) {
  if (selected === "admin" && user?.role !== "admin") selected = "chat";
  panel = selected;
  for (const name of ["chat", "contacts", "admin"]) {
    const chosen = name === selected;
    element(`tab-${name}`).setAttribute("aria-selected", String(chosen));
    element(`tab-${name}`).tabIndex = chosen ? 0 : -1;
    const target = name === "chat" ? "room-main" : `${name}-panel`;
    element(target).hidden = name === "admin" ? !chosen : selected === "admin" || (mobile.matches && !chosen);
  }
  if (selected === "admin") loadAdmin();
}

function sendFeedback(message, error = false) {
  element("send-feedback").textContent = message;
  element("send-feedback").dataset.error = String(error);
}

function dismissInvite() {
  element("new-invite").hidden = true;
  element("invite-link").value = "";
  element("invite-code").value = "";
  element("invite-copy-feedback").textContent = "";
}

function leave(message = "", focus = true) {
  user = null;
  csrf = "";
  session++;
  revision++;
  adminRevision++;
  clearTimeout(timer);
  active = false;
  connected = false;
  sending = false;
  newestMessage = 0;
  lastSeen = null;
  contacts = [];
  checkins = [];
  incident = null;
  administration = null;
  draftAttachments = [];
  contactSignature = peopleSignature = invitesSignature = auditSignature = "";
  editorSignature = checkinIncident = null;
  incidentDirty = contactsDirty = checkinDirty = false;
  busy.clear();
  element("workspace").hidden = true;
  element("welcome").hidden = false;
  for (const id of ["messages", "contacts", "checkins", "people", "invites", "audit", "contact-rows", "draft-attachments"]) {
    element(id).replaceChildren();
  }
  delete element("checkins").dataset.signature;
  for (const id of ["join", "send", "checkin-form", "incident-form", "invite-form", "contacts-form"]) element(id).reset();
  for (const id of ["token", "author", "message", "contact-search"]) element(id).value = "";
  for (const id of ["contact-feedback", "account-feedback", "admin-feedback", "admin-load-feedback", "checkin-feedback", "incident-feedback", "invite-feedback", "contacts-edit-feedback", "attachment-feedback"]) element(id).textContent = "";
  for (const button of document.querySelectorAll("#admin-panel button")) button.disabled = false;
  element("token").type = "password";
  element("show-token").textContent = "Show";
  element("show-token").setAttribute("aria-label", "Show access code");
  element("show-token").setAttribute("aria-pressed", "false");
  element("error").textContent = message;
  element("connection-banner").hidden = true;
  element("incident-banner").hidden = true;
  element("tab-admin").hidden = true;
  element("room-info").open = false;
  element("leave").disabled = false;
  dismissInvite();
  selectPanel("chat");
  sendFeedback("");
  updateCounter();
  updateSendButton();
  if (focus) element("token").focus();
}

function connection(ok, error) {
  connected = ok;
  element("connection-banner").hidden = ok;
  if (ok) {
    lastSeen = new Date();
    const label = active ? "Chat open" : "Standby";
    if (element("status").textContent !== label) element("status").textContent = label;
    element("status").dataset.state = active ? "open" : "standby";
    element("last-updated").textContent = `Updated ${lastSeen.toLocaleTimeString()}`;
  } else {
    element("status").textContent = "Reconnecting";
    element("status").dataset.state = "disconnected";
    const saved = lastSeen ? ` Last update ${lastSeen.toLocaleTimeString()}.` : "";
    const notice = `Connection lost. Retrying.${saved} Draft kept in this tab.`;
    if (element("connection").textContent !== notice) element("connection").textContent = notice;
    element("connection-reason").textContent = error.message;
  }
  updateSendButton();
}

function contactHref(value) {
  const email = value.replace(/^mailto:/i, "");
  if (/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$/i.test(email)) {
    return `mailto:${encodeURIComponent(email).replace(/%40/g, "@")}`;
  }
  const phone = value.replace(/^tel:/i, "");
  if (/^\+?[0-9][0-9 ()-]{4,24}[0-9]$/.test(phone)) return `tel:${phone.replace(/[ ()-]/g, "")}`;
  return null;
}

async function copyText(value, feedback, success) {
  const joined = session;
  try {
    await navigator.clipboard.writeText(value);
    if (joined === session) element(feedback).textContent = success;
  } catch {
    if (joined === session) element(feedback).textContent = "Clipboard unavailable. Select and copy the value instead.";
  }
}

function renderContacts() {
  const query = element("contact-search").value.trim().toLocaleLowerCase();
  const matches = contacts.filter(contact => `${contact.name} ${contact.contact}`.toLocaleLowerCase().includes(query));
  element("contact-count").textContent = query ? `${matches.length} / ${contacts.length}` : String(contacts.length);
  const items = matches.map(contact => {
    const item = document.createElement("li");
    item.className = "contact";
    const name = document.createElement("strong");
    name.textContent = contact.name;
    const href = contactHref(contact.contact);
    const detail = document.createElement(href ? "a" : "span");
    detail.className = "contact-detail";
    detail.textContent = contact.contact;
    if (href) detail.href = href;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "button-subtle";
    copy.append(element("copy-icon").content.cloneNode(true));
    copy.setAttribute("aria-label", `Copy contact for ${contact.name}`);
    copy.title = `Copy contact for ${contact.name}`;
    copy.addEventListener("click", () => copyText(contact.contact, "contact-feedback", `Copied contact for ${contact.name}.`));
    item.append(name, detail, copy);
    return item;
  });
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "contacts-empty";
    empty.textContent = contacts.length ? "No contacts match." : "No contacts yet. Ask your incident lead to add them.";
    items.push(empty);
  }
  element("contacts").replaceChildren(...items);
}

function jumpToLatest() {
  const list = element("messages");
  list.scrollTop = list.scrollHeight;
  element("jump-latest").hidden = true;
}

function fileSize(size) {
  if (size < 1024) return `${size} B`;
  return size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MiB` : `${Math.ceil(size / 1024)} KiB`;
}

function attachmentURL(attachment) {
  return Number.isSafeInteger(attachment.id) && attachment.id > 0 ? `/api/attachments/${attachment.id}` : null;
}

function renderAttachments(attachments) {
  const list = document.createElement("ul");
  list.className = "message-attachments";
  for (const attachment of attachments) {
    const url = attachmentURL(attachment);
    if (!url) continue;
    const item = document.createElement("li");
    const imageTypes = ["image/png", "image/jpeg", "image/gif", "image/webp"];
    const videoTypes = ["video/mp4", "video/webm", "video/quicktime"];
    if (imageTypes.includes(attachment.content_type)) {
      const image = document.createElement("img");
      image.src = url;
      image.alt = attachment.filename;
      image.loading = "lazy";
      image.decoding = "async";
      item.append(image);
    } else if (videoTypes.includes(attachment.content_type)) {
      const video = document.createElement("video");
      video.src = url;
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.setAttribute("aria-label", attachment.filename);
      item.append(video);
    }
    const link = document.createElement("a");
    link.href = url;
    link.download = attachment.filename;
    link.textContent = `Download ${attachment.filename} (${fileSize(attachment.size)})`;
    const feedback = document.createElement("p");
    feedback.className = "field-help";
    feedback.setAttribute("role", "status");
    let downloading = false;
    link.addEventListener("click", async event => {
      event.preventDefault();
      if (downloading) return;
      downloading = true;
      link.setAttribute("aria-disabled", "true");
      feedback.textContent = "Downloading…";
      const joined = session;
      try {
        const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
        if (!response.ok) await readResponse(response);
        const file = await response.blob();
        if (joined !== session) return;
        download(file, attachment.content_type, attachment.filename);
        feedback.textContent = "Downloaded. Keep the file private.";
      } catch (error) {
        if (joined !== session || expired(error)) return;
        feedback.textContent = `Download failed. Try again when connected. ${error.message}`;
      } finally {
        downloading = false;
        link.removeAttribute("aria-disabled");
      }
    });
    item.append(link, feedback);
    list.append(item);
  }
  return list;
}

function reconcileAttachments(messages) {
  const published = new Set(messages.flatMap(message => (message.attachments || []).map(attachment => attachment.id)));
  let changed = false;
  for (const attachment of draftAttachments) {
    if (published.has(attachment.id) && attachment.state !== "published") {
      attachment.state = "published";
      attachment.error = "Already in chat. Remove from draft to continue.";
      changed = true;
    }
  }
  if (changed) renderDraftAttachments();
}

function renderMessages(messages) {
  const list = element("messages");
  const visible = element("chat").getClientRects().length > 0;
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
  const initial = newestMessage === 0;
  const ids = new Set(messages.map(message => String(message.id)));
  const existing = new Set([...list.children].map(item => item.dataset.id));
  for (const item of [...list.children]) {
    if (!ids.has(item.dataset.id)) item.remove();
  }
  let added = false;
  for (const message of messages) {
    if (existing.has(String(message.id))) continue;
    added = true;
    const item = document.createElement("li");
    item.className = "message";
    item.dataset.id = String(message.id);
    const header = document.createElement("div");
    header.className = "message-header";
    const author = document.createElement("strong");
    author.textContent = message.author;
    const time = document.createElement("time");
    time.dateTime = message.created;
    time.textContent = new Date(message.created).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
    time.title = new Date(message.created).toLocaleString();
    const body = document.createElement("p");
    body.className = "message-body";
    body.textContent = message.body;
    header.append(author, time);
    item.append(header, body);
    if (message.attachments?.length) item.append(renderAttachments(message.attachments));
    list.append(item);
  }
  element("chat-empty").hidden = messages.length > 0;
  list.hidden = messages.length === 0;
  element("message-count").textContent = String(messages.length);
  if (added && visible && (initial || atBottom)) jumpToLatest();
  else if (added) element("jump-latest").hidden = false;
  newestMessage = messages.length ? messages[messages.length - 1].id : 0;
  reconcileAttachments(messages);
}

function displayTime(value) {
  if (!value) return "";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function record(name, detail) {
  const item = document.createElement("li");
  const title = document.createElement("strong");
  title.textContent = name;
  const description = document.createElement("p");
  description.className = "field-help";
  description.textContent = detail;
  item.append(title, description);
  return item;
}

function checkinLabel(entry) {
  return entry.status === "help" ? "Needs help" : "Safe";
}

function renderCheckins() {
  const signature = JSON.stringify(checkins);
  if (element("checkins").dataset.signature !== signature) {
    element("checkins").dataset.signature = signature;
    const items = checkins.map(entry => record(entry.name, `${checkinLabel(entry)}${entry.note ? ` — ${entry.note}` : ""} · ${displayTime(entry.updated)}`));
    element("checkins").replaceChildren(...(items.length ? items : [record("No check-ins yet", "")]));
  }
  element("checkin-count").textContent = String(checkins.length);
  const personal = user.id !== null && !user.legacy;
  element("checkin-form").hidden = !personal;
  element("checkin-help").hidden = personal;
  if (incident?.id !== checkinIncident) {
    checkinDirty = false;
    checkinIncident = incident?.id;
    element("checkin-feedback").textContent = "";
  }
  if (!checkinDirty && !busy.has("checkin-submit")) {
    const mine = checkins.find(entry => entry.person_id === user.id);
    element("checkin-status").value = mine?.status || "safe";
    element("checkin-note").value = mine?.note || "";
  }
}

function updateBulletinSpace() {
  const banner = element("incident-banner");
  const space = banner.hidden ? 0 : banner.getBoundingClientRect().height + parseFloat(getComputedStyle(banner).marginBottom);
  element("workspace").style.setProperty("--bulletin-space", `${space}px`);
}

new ResizeObserver(updateBulletinSpace).observe(element("incident-banner"));

function renderIncident() {
  const bulletin = incident && (incident.title || incident.update || incident.lead || incident.next_update);
  element("incident-banner").hidden = !bulletin;
  element("incident-title").textContent = incident?.title || "Incident update";
  element("incident-update").textContent = incident?.update || "";
  element("incident-update").hidden = !incident?.update;
  element("incident-meta").textContent = [
    incident?.lead ? `Lead: ${incident.lead}` : "",
    incident?.next_update ? `Next update: ${incident.next_update}` : "",
  ].filter(Boolean).join(" · ");
  element("incident-meta").hidden = !element("incident-meta").textContent;
  updateBulletinSpace();
  if (incident && !incidentDirty && !busy.has("incident-save")) {
    element("incident-mode").value = incident.mode;
    element("edit-title").value = incident.title;
    element("edit-update").value = incident.update;
    element("edit-lead").value = incident.lead;
    element("edit-next").value = incident.next_update;
  }
}

function renderRoom(room) {
  active = room.active;
  incident = room.incident;
  checkins = room.checkins || [];
  element("chat").hidden = !active;
  element("standby").hidden = active;
  element("draft-notice").hidden = !element("message").value;
  const signature = JSON.stringify(room.contacts);
  if (signature !== contactSignature) {
    contacts = room.contacts;
    contactSignature = signature;
    renderContacts();
  }
  renderMessages(room.messages);
  renderIncident();
  renderCheckins();
  renderContactEditor();
  if (administration) renderPeople();
}

function scheduleRefresh() {
  clearTimeout(timer);
  timer = setTimeout(refresh, 2000);
}

async function refresh() {
  if (!user) return;
  const current = ++revision;
  const joined = session;
  try {
    const room = await api("/api/room");
    if (!user || joined !== session || current !== revision) return;
    renderRoom(room);
    connection(true);
    if (panel === "admin") loadAdmin();
  } catch (error) {
    if (!user || joined !== session || current !== revision) return;
    if (!expired(error)) connection(false, error);
  } finally {
    if (user && joined === session && current === revision) scheduleRefresh();
  }
}

async function enter(data, focus = true) {
  user = data.user;
  csrf = data.csrf;
  element("token").value = "";
  element("welcome").hidden = true;
  element("workspace").hidden = false;
  element("room-info").open = false;
  element("tab-admin").hidden = user.role !== "admin";
  element("identity").textContent = user.legacy ? "Shared-code names unverified" : `As ${user.name}`;
  element("author").hidden = !user.legacy;
  element("author").required = user.legacy;
  element("author-label").hidden = !user.legacy;
  element("attachment-picker").hidden = user.id === null || user.legacy;
  element("chat").classList.toggle("with-attachments", user.id !== null && !user.legacy);
  element("leave").title = `Session expires ${displayTime(data.expires)}`;
  selectPanel("chat");
  await refresh();
  if (user && focus) element("room-title").focus();
}

async function perform(buttonId, feedbackId, path, body, success) {
  if (!user || busy.has(buttonId)) return;
  const joined = session;
  busy.add(buttonId);
  element(buttonId).disabled = true;
  element(feedbackId).textContent = "";
  try {
    const data = await api(path, body);
    if (joined !== session) return;
    await success(data);
  } catch (error) {
    if (joined !== session || expired(error)) return;
    element(feedbackId).textContent = `Not confirmed. Check current state before retrying. ${error.message}`;
  } finally {
    if (joined === session) {
      busy.delete(buttonId);
      element(buttonId).disabled = false;
      updateSendButton();
    }
  }
}

function addContactRow(contact = { name: "", contact: "" }) {
  const row = document.createElement("div");
  row.className = "contact-edit-row";
  for (const [field, text] of [["name", "Contact name"], ["contact", "Contact detail"]]) {
    const label = document.createElement("label");
    label.textContent = text;
    const input = document.createElement("input");
    input.value = contact[field];
    input.name = field;
    input.maxLength = 200;
    input.required = true;
    label.append(input);
    row.append(label);
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "button-subtle";
  remove.textContent = "Remove";
  remove.setAttribute("aria-label", `Remove contact${contact.name ? ` ${contact.name}` : ""}`);
  remove.addEventListener("click", () => {
    row.remove();
    contactsDirty = true;
    element("contact-add").disabled = false;
  });
  row.append(remove);
  element("contact-rows").append(row);
  element("contact-add").disabled = element("contact-rows").children.length >= 1000;
  return row;
}

function renderContactEditor() {
  if (user?.role !== "admin" || contactsDirty || busy.has("contacts-save") || editorSignature === contactSignature) return;
  element("contact-rows").replaceChildren();
  for (const contact of contacts) addContactRow(contact);
  element("contact-add").disabled = contacts.length >= 1000;
  editorSignature = contactSignature;
}

function revokeButton(item, kind, entry) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button-subtle revoke";
  button.textContent = "Revoke";
  button.setAttribute("aria-label", `Revoke ${kind === "people" ? "access for" : "invitation for"} ${entry.name}`);
  button.disabled = Boolean(entry.revoked || entry.used || (kind === "people" && entry.id === user.id));
  button.addEventListener("click", async () => {
    if (!confirm(`Revoke ${kind === "people" ? "access and sessions" : "this invitation"} for ${entry.name}?`)) return;
    const joined = session;
    button.disabled = true;
    try {
      await api(`/api/admin/${kind}/${entry.id}/revoke`, {});
      if (joined !== session) return;
      element("admin-feedback").textContent = "";
      await loadAdmin();
    } catch (error) {
      if (joined !== session || expired(error)) return;
      element("admin-feedback").textContent = `Revocation not confirmed. Reload administration before retrying. ${error.message}`;
    } finally {
      if (joined === session && button.isConnected) button.disabled = false;
    }
  });
  item.append(button);
}

function renderPeople() {
  const signature = JSON.stringify([administration.people, checkins, incident?.id]);
  if (signature === peopleSignature) return;
  peopleSignature = signature;
  element("people-count").textContent = String(administration.people.filter(person => !person.revoked).length);
  const items = administration.people.map(person => {
    const checkin = checkins.find(entry => entry.person_id === person.id);
    const status = person.revoked ? "Revoked" : checkin ? checkinLabel(checkin) : "Not checked in";
    const item = record(person.name, `${person.role} · ${status} · Joined ${displayTime(person.created)}${person.revoked ? ` · Revoked ${displayTime(person.revoked)}` : ""}`);
    revokeButton(item, "people", person);
    return item;
  });
  element("people").replaceChildren(...(items.length ? items : [record("No enrolled people", "Create a personal invitation to enroll someone.")]));
}

function renderAdmin(data) {
  administration = data;
  renderPeople();
  const signature = JSON.stringify([data.invites, data.invites.map(invite => (typeof invite.expires === "number" ? invite.expires * 1000 : Date.parse(invite.expires)) <= Date.now())]);
  if (signature !== invitesSignature) {
    invitesSignature = signature;
    const items = data.invites.map(invite => {
      const expiry = typeof invite.expires === "number" ? invite.expires * 1000 : Date.parse(invite.expires);
      const status = invite.revoked ? "Revoked" : invite.used ? "Used" : expiry <= Date.now() ? "Expired" : "Pending";
      const item = record(invite.name, `${invite.role} · ${status} · Created ${displayTime(invite.created)} · Expires ${displayTime(invite.expires)}${invite.used ? ` · Used ${displayTime(invite.used)}` : ""}${invite.revoked ? ` · Revoked ${displayTime(invite.revoked)}` : ""}`);
      revokeButton(item, "invites", invite);
      return item;
    });
    element("invites").replaceChildren(...(items.length ? items : [record("No invitations", "Create one above, then share its link or code.")]));
  }
  const audit = JSON.stringify(data.audit);
  if (audit !== auditSignature) {
    auditSignature = audit;
    const items = data.audit.map(entry => record(`${entry.actor} · ${entry.action}`, `${entry.detail || ""} · ${displayTime(entry.created)}`));
    element("audit").replaceChildren(...(items.length ? items : [record("No audit entries", "")]));
  }
}

async function loadAdmin() {
  if (user?.role !== "admin") return;
  const current = ++adminRevision;
  const joined = session;
  try {
    const data = await api("/api/admin");
    if (joined !== session || current !== adminRevision) return;
    renderAdmin(data);
    element("admin-retry").hidden = true;
    element("admin-load-feedback").textContent = "";
  } catch (error) {
    if (joined !== session || current !== adminRevision || expired(error)) return;
    element("admin-load-feedback").textContent = `Could not load administration. ${error.message}`;
    element("admin-retry").hidden = false;
  }
}

function download(content, type, filename) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function escapeHTML(value) {
  return value.replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

element("download-contacts").addEventListener("click", () => {
  const format = element("offline-format").value;
  const entries = contacts.map(({ name, contact }) => ({ name, contact }));
  let content;
  if (format === "html") {
    const rows = entries.map(entry => {
      const href = contactHref(entry.contact);
      const detail = href ? `<a href="${escapeHTML(href)}">${escapeHTML(entry.contact)}</a>` : escapeHTML(entry.contact);
      return `<tr><th scope="row">${escapeHTML(entry.name)}</th><td>${detail}</td></tr>`;
    }).join("\n");
    content = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; form-action 'none'"><meta name="referrer" content="no-referrer"><title>Lifeboat offline contacts</title></head><body><h1>Lifeboat contacts</h1><p>Saved ${escapeHTML(new Date().toLocaleString())}. This copy does not update. Keep it private; delete it when no longer needed.</p><table><caption>Offline contact directory</caption><thead><tr><th scope="col">Name</th><th scope="col">Contact</th></tr></thead><tbody>${rows}</tbody></table></body></html>`;
  } else if (format === "json") content = JSON.stringify({ contacts: entries }, null, 2);
  else content = entries.map(entry => `${entry.name}: ${entry.contact}`).join("\n");
  download(content, { html: "text/html;charset=utf-8", json: "application/json", txt: "text/plain;charset=utf-8" }[format], `lifeboat-contacts.${format}`);
  element("contact-feedback").textContent = "Contacts downloaded. Keep the file private.";
});

element("show-token").addEventListener("click", () => {
  const show = element("token").type === "password";
  element("token").type = show ? "text" : "password";
  element("show-token").textContent = show ? "Hide" : "Show";
  element("show-token").setAttribute("aria-label", show ? "Hide access code" : "Show access code");
  element("show-token").setAttribute("aria-pressed", String(show));
});

element("join").addEventListener("submit", async event => {
  event.preventDefault();
  const joined = ++session;
  element("join-submit").disabled = true;
  element("join-submit").textContent = "Joining...";
  element("error").textContent = "";
  try {
    const request = api("/api/session", { credential: element("token").value });
    element("token").value = "";
    const data = await request;
    if (joined !== session) return;
    await enter(data);
  } catch (error) {
    if (joined === session) leave(error.message);
  } finally {
    element("join-submit").disabled = false;
    element("join-submit").textContent = "Join room";
  }
});

element("leave").addEventListener("click", () => perform("leave", "account-feedback", "/api/logout", {}, () => leave("Signed out.")));
element("retry").addEventListener("click", () => {
  clearTimeout(timer);
  refresh();
});
element("admin-retry").addEventListener("click", loadAdmin);
element("contact-search").addEventListener("input", renderContacts);
element("find-contact").addEventListener("click", () => {
  selectPanel("contacts");
  element("contact-search").scrollIntoView({ block: "center" });
  element("contact-search").focus();
});
for (const name of ["chat", "contacts", "admin"]) {
  const tab = element(`tab-${name}`);
  tab.addEventListener("click", () => selectPanel(name));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const names = user?.role === "admin" ? ["chat", "contacts", "admin"] : ["chat", "contacts"];
    const offset = event.key === "ArrowLeft" ? -1 : 1;
    const index = event.key === "Home" ? 0 : event.key === "End" ? names.length - 1 : (names.indexOf(name) + offset + names.length) % names.length;
    selectPanel(names[index]);
    element(`tab-${names[index]}`).focus();
  });
}
mobile.addEventListener("change", () => selectPanel(panel));
element("jump-latest").addEventListener("click", jumpToLatest);
element("messages").addEventListener("scroll", () => {
  const list = element("messages");
  if (list.scrollHeight - list.scrollTop - list.clientHeight < 48) element("jump-latest").hidden = true;
});
element("message").addEventListener("input", updateCounter);
element("message").addEventListener("keydown", event => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    if (!element("send-submit").disabled) element("send").requestSubmit();
  }
});
function renderDraftAttachments() {
  const items = draftAttachments.map(attachment => {
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = `${attachment.filename} (${fileSize(attachment.size)})`;
    const status = document.createElement("p");
    status.className = "field-help";
    status.textContent = attachment.error || ({
      uploading: "Uploading…", ready: "Ready to send", removing: "Removing…",
    })[attachment.state];
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "button-subtle";
    remove.textContent = attachment.state === "published" ? "Remove from draft" : "Remove";
    remove.setAttribute("aria-label", `Remove attachment ${attachment.filename}`);
    remove.dataset.busy = String(["uploading", "removing"].includes(attachment.state));
    remove.addEventListener("click", () => removeAttachment(attachment));
    item.append(label, status, remove);
    if (attachment.error?.startsWith("Removal not confirmed.")) {
      const discard = document.createElement("button");
      discard.type = "button";
      discard.className = "button-subtle";
      discard.textContent = "Discard from draft";
      discard.addEventListener("click", () => {
        if (!confirm("Discard this attachment from your draft? A private upload may remain on the server for up to 24 hours.")) return;
        draftAttachments = draftAttachments.filter(entry => entry.key !== attachment.key);
        renderDraftAttachments();
      });
      item.append(discard);
    }
    if (attachment.state === "uncertain") {
      const reuse = document.createElement("button");
      reuse.type = "button";
      reuse.className = "button-subtle";
      reuse.textContent = "Reuse upload";
      reuse.setAttribute("aria-label", `Reuse upload ${attachment.filename}`);
      reuse.addEventListener("click", () => {
        if (!confirm("Check chat first: this file may already be published. Prepare the same upload for another send?")) return;
        attachment.state = "ready";
        attachment.error = "";
        renderDraftAttachments();
      });
      item.append(reuse);
    }
    return item;
  });
  element("draft-attachments").replaceChildren(...items);
  updateSendButton();
}

async function uploadAttachment(file) {
  const joined = session;
  const attachment = { key: ++attachmentSequence, filename: file.name, size: file.size, state: "uploading" };
  draftAttachments.push(attachment);
  renderDraftAttachments();
  try {
    const response = await fetch("/api/attachments", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRF-Token": csrf, "X-File-Name": encodeURIComponent(file.name), "Content-Type": file.type || "application/octet-stream" },
      body: file,
      cache: "no-store",
    });
    const data = await readResponse(response);
    if (joined !== session) return;
    Object.assign(attachment, data, { state: "ready", error: "" });
  } catch (error) {
    if (joined !== session || expired(error)) return;
    attachment.state = "failed";
    attachment.error = `Upload not confirmed. Remove it before choosing again. An accepted upload may remain private for 24 hours. ${error.message}`;
  } finally {
    if (joined === session) renderDraftAttachments();
  }
}

async function removeAttachment(attachment) {
  if (sending || ["uploading", "removing"].includes(attachment.state)) return;
  if (!attachment.id || attachment.state === "published") {
    draftAttachments = draftAttachments.filter(entry => entry.key !== attachment.key);
    renderDraftAttachments();
    return;
  }
  const joined = session;
  const state = attachment.state;
  attachment.state = "removing";
  attachment.error = "";
  renderDraftAttachments();
  try {
    const response = await fetch(attachmentURL(attachment), {
      method: "DELETE", credentials: "same-origin", headers: { "X-CSRF-Token": csrf }, cache: "no-store",
    });
    if (response.status !== 204) await readResponse(response);
    if (joined !== session) return;
    draftAttachments = draftAttachments.filter(entry => entry.key !== attachment.key);
  } catch (error) {
    if (joined !== session || expired(error)) return;
    if (attachment.state !== "published") {
      attachment.state = state;
      attachment.error = `Removal not confirmed. Check chat before trying again. ${error.message}`;
    }
  } finally {
    if (joined === session) renderDraftAttachments();
  }
}

element("attachments").addEventListener("change", () => {
  const files = [...element("attachments").files];
  element("attachments").value = "";
  if (!user || user.legacy || sending) return;
  if (files.length + draftAttachments.length > 5) {
    element("attachment-feedback").textContent = "At most 5 files per message. Remove a draft attachment or choose fewer files; this selection wasn't uploaded.";
    return;
  }
  if (files.some(file => file.size > 25 * 1024 * 1024)) {
    element("attachment-feedback").textContent = "Choose files up to 25 MiB each. This selection wasn't uploaded.";
    return;
  }
  element("attachment-feedback").textContent = "";
  for (const file of files) uploadAttachment(file);
});
element("send").addEventListener("submit", async event => {
  event.preventDefault();
  if (element("send-submit").disabled) return;
  const joined = session;
  const message = element("message").value;
  const attachments = [...draftAttachments];
  sending = true;
  updateSendButton();
  sendFeedback("");
  try {
    const body = { author: user.legacy ? element("author").value : user.name, message };
    if (attachments.length) body.attachment_ids = attachments.map(attachment => attachment.id);
    await api("/api/messages", body);
    if (joined !== session) return;
    if (element("message").value === message) element("message").value = "";
    draftAttachments = draftAttachments.filter(attachment => !attachments.includes(attachment));
    renderDraftAttachments();
    updateCounter();
    sendFeedback("Message sent.");
    await refresh();
    if (joined === session) jumpToLatest();
  } catch (error) {
    if (joined !== session || expired(error)) return;
    for (const attachment of attachments) {
      if (attachment.state === "published") continue;
      attachment.state = "uncertain";
      attachment.error = "Send not confirmed. Check chat before reusing this upload.";
    }
    renderDraftAttachments();
    sendFeedback(`Not confirmed. Check chat before retrying. Draft kept. ${error.message}`, true);
  } finally {
    if (joined === session) {
      sending = false;
      updateSendButton();
    }
  }
});

element("checkin-form").addEventListener("input", () => { checkinDirty = true; });
element("checkin-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!active || !connected || user?.legacy || user?.id === null) return;
  const status = element("checkin-status").value;
  const note = element("checkin-note").value;
  perform("checkin-submit", "checkin-feedback", "/api/checkins", { status, note }, async () => {
    checkinDirty = element("checkin-status").value !== status || element("checkin-note").value !== note;
    element("checkin-feedback").textContent = "Check-in saved.";
    await refresh();
  });
});
element("incident-form").addEventListener("input", () => { incidentDirty = true; });
element("incident-form").addEventListener("submit", event => {
  event.preventDefault();
  const mode = element("incident-mode").value;
  if (mode !== incident?.mode && !confirm(`Change room control to ${mode}? ${mode === "auto" ? "The watcher will control chat." : mode === "open" ? "Chat will stay open regardless of the watcher." : "Chat will close."}`)) return;
  const body = { mode, title: element("edit-title").value, update: element("edit-update").value, lead: element("edit-lead").value, next_update: element("edit-next").value };
  perform("incident-save", "incident-feedback", "/api/admin/incident", body, async () => {
    incidentDirty = body.mode !== element("incident-mode").value || body.title !== element("edit-title").value || body.update !== element("edit-update").value || body.lead !== element("edit-lead").value || body.next_update !== element("edit-next").value;
    element("incident-feedback").textContent = "Incident saved.";
    await refresh();
  });
});
element("contacts-form").addEventListener("input", () => { contactsDirty = true; });
element("contact-add").addEventListener("click", () => {
  if (element("contact-rows").children.length >= 1000) return;
  contactsDirty = true;
  addContactRow().querySelector("input").focus();
});
function editedContacts() {
  return [...element("contact-rows").children].map(row => ({ name: row.querySelector("[name=name]").value, contact: row.querySelector("[name=contact]").value }));
}
element("contacts-form").addEventListener("submit", event => {
  event.preventDefault();
  const entries = editedContacts();
  if (!entries.length && contacts.length && !confirm("Remove every contact from this room?")) return;
  perform("contacts-save", "contacts-edit-feedback", "/api/admin/contacts", { contacts: entries }, async () => {
    contactsDirty = JSON.stringify(entries) !== JSON.stringify(editedContacts());
    element("contacts-edit-feedback").textContent = "Contacts saved.";
    await refresh();
  });
});
element("invite-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!element("new-invite").hidden && !confirm("Replace the displayed invitation? Copy its code first; it cannot be shown again.")) return;
  if (element("invite-role").value === "admin" && !confirm("Invite this person as an administrator? They can manage access, contacts and incidents.")) return;
  perform("invite-create", "invite-feedback", "/api/admin/invites", {
    name: element("invite-name").value, role: element("invite-role").value, hours: Number(element("invite-hours").value),
  }, async data => {
    element("invite-link").value = `${location.origin}/#invite=${encodeURIComponent(data.credential)}`;
    element("invite-code").value = data.credential;
    element("new-invite-title").textContent = `Invitation for ${data.name}`;
    element("new-invite-expiry").textContent = `${data.role} · Expires ${displayTime(data.expires)}`;
    element("new-invite").hidden = false;
    element("invite-copy-feedback").textContent = "";
    element("invite-feedback").textContent = "Invitation created. Copy its link or code below.";
    await loadAdmin();
  });
});
element("copy-invite-link").addEventListener("click", () => copyText(element("invite-link").value, "invite-copy-feedback", "Invitation link copied."));
element("copy-invite-code").addEventListener("click", () => copyText(element("invite-code").value, "invite-copy-feedback", "Access code copied."));
element("dismiss-invite").addEventListener("click", dismissInvite);
element("export-incident").addEventListener("click", () => perform("export-incident", "admin-feedback", "/api/admin/export", undefined, data => {
  download(JSON.stringify(data, null, 2), "application/json", "lifeboat-incident.json");
}));

function readInvitation() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (!fragment.has("invite")) return false;
  history.replaceState(null, "", location.pathname + location.search);
  leave("", false);
  element("token").value = fragment.get("invite");
  return true;
}

window.addEventListener("hashchange", () => {
  if (readInvitation()) element("token").focus();
});

async function restore() {
  // An invitation must be accepted explicitly, even on a previously signed-in device.
  if (readInvitation()) return;
  const joined = session;
  element("join-submit").disabled = true;
  try {
    const data = await api("/api/session");
    if (joined === session) await enter(data, false);
  } catch (error) {
    if (joined === session && (!(error instanceof APIError) || error.status !== 401)) {
      element("error").textContent = `Could not restore your session. Check the connection or join again. ${error.message}`;
    }
  } finally {
    element("join-submit").disabled = false;
  }
}
restore();
