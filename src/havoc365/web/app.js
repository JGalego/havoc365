"use strict";
let token = "";
let timer = null;
let session = 0;
let revision = 0;
let active = false;
let connected = false;
let sending = false;
let lastSeen = null;
let contacts = [];
let contactSignature = "";
let newestMessage = 0;
let panel = "chat";
const element = id => document.getElementById(id);
const mobile = matchMedia("(max-width: 700px)");

class APIError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
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

function updateSendButton() {
  element("send-submit").disabled = sending || !connected || !active;
  element("send-submit").textContent = sending ? "Sending..." : "Send";
}

function updateCounter() {
  element("message-counter").textContent = `${element("message").value.length} / 2000`;
  element("message-counter").hidden = !element("message").value.length;
}

function selectPanel(selected) {
  panel = selected;
  for (const name of ["chat", "contacts"]) {
    const chosen = name === selected;
    element(`tab-${name}`).setAttribute("aria-selected", String(chosen));
    element(`tab-${name}`).tabIndex = chosen ? 0 : -1;
    element(name === "chat" ? "room-main" : "contacts-panel").hidden = mobile.matches && !chosen;
  }
}

function sendFeedback(message, error = false) {
  element("send-feedback").textContent = message;
  element("send-feedback").dataset.error = String(error);
}

function leave(message = "") {
  token = "";
  session++;
  revision++;
  clearTimeout(timer);
  active = false;
  connected = false;
  sending = false;
  newestMessage = 0;
  lastSeen = null;
  contacts = [];
  contactSignature = "";
  element("workspace").hidden = true;
  element("welcome").hidden = false;
  element("messages").replaceChildren();
  element("contacts").replaceChildren();
  for (const id of ["token", "author", "message", "contact-search"]) element(id).value = "";
  element("token").type = "password";
  element("show-token").textContent = "Show";
  element("show-token").setAttribute("aria-label", "Show member token");
  element("show-token").setAttribute("aria-pressed", "false");
  element("error").textContent = message;
  element("contact-feedback").textContent = "";
  element("connection-banner").hidden = true;
  element("room-info").open = false;
  selectPanel("chat");
  sendFeedback("");
  updateCounter();
  updateSendButton();
  element("token").focus();
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
    if (element("status").textContent !== "Reconnecting") element("status").textContent = "Reconnecting";
    element("status").dataset.state = "disconnected";
    const saved = lastSeen ? ` Last update ${lastSeen.toLocaleTimeString()}.` : "";
    const notice = `Connection lost. Retrying.${saved} Draft kept in this tab.`;
    if (element("connection").textContent !== notice) element("connection").textContent = notice;
    element("connection-reason").textContent = error.message;
  }
  updateSendButton();
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
    const detail = document.createElement("span");
    detail.className = "contact-detail";
    detail.textContent = contact.contact;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "button-subtle";
    copy.append(element("copy-icon").content.cloneNode(true));
    copy.setAttribute("aria-label", `Copy contact for ${contact.name}`);
    copy.title = `Copy contact for ${contact.name}`;
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(contact.contact);
        element("contact-feedback").textContent = `Copied contact for ${contact.name}.`;
      } catch {
        element("contact-feedback").textContent = "Clipboard unavailable. Select and copy the contact detail instead.";
      }
    });
    item.append(name, detail, copy);
    return item;
  });
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "contacts-empty";
    empty.textContent = contacts.length
      ? "No contacts match."
      : "No contacts yet. Ask your incident lead to add them.";
    items.push(empty);
  }
  element("contacts").replaceChildren(...items);
}

function jumpToLatest() {
  const list = element("messages");
  list.scrollTop = list.scrollHeight;
  element("jump-latest").hidden = true;
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
    list.append(item);
  }
  element("chat-empty").hidden = messages.length > 0;
  list.hidden = messages.length === 0;
  element("message-count").textContent = String(messages.length);
  if (added && visible && (initial || atBottom)) jumpToLatest();
  else if (added) element("jump-latest").hidden = false;
  newestMessage = messages.length ? messages[messages.length - 1].id : 0;
}

function renderRoom(room) {
  active = room.active;
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
}

function scheduleRefresh() {
  clearTimeout(timer);
  timer = setTimeout(refresh, 2000);
}

async function refresh() {
  const current = ++revision;
  const joined = session;
  try {
    const room = await api("/api/room");
    if (!token || joined !== session || current !== revision) return;
    renderRoom(room);
    connection(true);
  } catch (error) {
    if (!token || joined !== session || current !== revision) return;
    if (error instanceof APIError && error.status === 401) {
      leave("Your member token no longer works. Ask your incident lead for the current token.");
      return;
    }
    connection(false, error);
  } finally {
    if (token && joined === session && current === revision) scheduleRefresh();
  }
}

element("show-token").addEventListener("click", () => {
  const show = element("token").type === "password";
  element("token").type = show ? "text" : "password";
  element("show-token").textContent = show ? "Hide" : "Show";
  element("show-token").setAttribute("aria-label", show ? "Hide member token" : "Show member token");
  element("show-token").setAttribute("aria-pressed", String(show));
});

element("join").addEventListener("submit", async event => {
  event.preventDefault();
  const joined = ++session;
  token = element("token").value;
  element("token").value = "";
  element("join-submit").disabled = true;
  element("join-submit").textContent = "Joining...";
  element("error").textContent = "";
  try {
    const room = await api("/api/room");
    if (joined !== session) return;
    element("welcome").hidden = true;
    element("workspace").hidden = false;
    element("room-info").open = false;
    selectPanel("chat");
    renderRoom(room);
    connection(true);
    scheduleRefresh();
    element("room-title").focus();
  } catch (error) {
    if (joined === session) leave(error.message);
  } finally {
    element("join-submit").disabled = false;
    element("join-submit").textContent = "Join room";
  }
});

element("leave").addEventListener("click", () => leave());
element("retry").addEventListener("click", () => {
  clearTimeout(timer);
  refresh();
});
element("contact-search").addEventListener("input", renderContacts);
function focusContacts() {
  selectPanel("contacts");
  element("contact-search").scrollIntoView({ block: "center" });
  element("contact-search").focus();
}
element("find-contact").addEventListener("click", focusContacts);
for (const name of ["chat", "contacts"]) {
  const tab = element(`tab-${name}`);
  tab.addEventListener("click", () => selectPanel(name));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? "chat" : event.key === "End" ? "contacts" : name === "chat" ? "contacts" : "chat";
    selectPanel(next);
    element(`tab-${next}`).focus();
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
element("send").addEventListener("submit", async event => {
  event.preventDefault();
  if (sending || !connected || !active) return;
  const joined = session;
  const message = element("message").value;
  sending = true;
  updateSendButton();
  sendFeedback("");
  try {
    await api("/api/messages", { author: element("author").value, message });
    if (joined !== session) return;
    if (element("message").value === message) element("message").value = "";
    updateCounter();
    sendFeedback("Check-in sent.");
    await refresh();
    if (joined === session) jumpToLatest();
  } catch (error) {
    if (joined !== session) return;
    if (error instanceof APIError && error.status === 401) {
      leave("Your member token no longer works. Ask your incident lead for the current token.");
      return;
    }
    sendFeedback(`Not confirmed. Check chat before retrying. Draft kept. ${error.message}`, true);
  } finally {
    if (joined === session) {
      sending = false;
      updateSendButton();
    }
  }
});
