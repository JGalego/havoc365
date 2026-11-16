"use strict";
let token = "";
let timer = null;
let rendered = "";
let revision = 0;
const element = id => document.getElementById(id);

async function api(path, body) {
  const session = token;
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    if (response.status === 401 && session === token) leave();
    const data = await response.json();
    throw new Error(data.error || `Request failed (${response.status}).`);
  }
  return response.json();
}

function leave() {
  token = "";
  revision++;
  clearTimeout(timer);
  element("workspace").hidden = true;
  element("join").hidden = false;
  element("messages").replaceChildren();
  element("contacts").replaceChildren();
  element("author").value = "";
  element("message").value = "";
  element("connection").textContent = "";
  rendered = "";
}

async function refresh() {
  const current = ++revision;
  try {
    const room = await api("/api/room");
    if (!token || current !== revision) return;
    element("status").textContent = room.active ? "OUTAGE / lifeboat open" : "STANDBY / contacts available";
    element("chat").hidden = !room.active;
    element("standby").hidden = room.active;
    const contacts = room.contacts.map(contact => {
      const item = document.createElement("li");
      item.textContent = `${contact.name}: ${contact.contact}`;
      return item;
    });
    element("contacts").replaceChildren(...contacts);
    if (!contacts.length) {
      const empty = document.createElement("li");
      empty.textContent = "No contacts imported. Ask your administrator to add them.";
      element("contacts").append(empty);
    }
    const signature = JSON.stringify(room.messages);
    if (signature !== rendered) {
      const messages = room.messages.map(message => {
        const item = document.createElement("li");
        const author = document.createElement("strong");
        author.textContent = `${message.author}\n`;
        const body = document.createTextNode(message.body);
        const time = document.createElement("time");
        time.dateTime = message.created;
        time.textContent = new Date(message.created).toLocaleString();
        item.append(author, body, time);
        return item;
      });
      element("messages").replaceChildren(...messages);
      rendered = signature;
    }
    element("connection").textContent = "";
  } catch (error) {
    element("connection").textContent = token
      ? `Connection problem: ${error.message} Retrying while this tab is open.`
      : error.message;
  } finally {
    if (token && current === revision) {
      clearTimeout(timer);
      timer = setTimeout(refresh, 2000);
    }
  }
}

element("join").addEventListener("submit", async event => {
  event.preventDefault();
  token = element("token").value;
  element("token").value = "";
  try {
    await api("/api/room");
    element("join").hidden = true;
    element("workspace").hidden = false;
    element("error").textContent = "";
    clearTimeout(timer);
    await refresh();
  } catch (error) {
    leave();
    element("error").textContent = error.message;
  }
});
element("leave").addEventListener("click", leave);
element("send").addEventListener("submit", async event => {
  event.preventDefault();
  const button = element("send").querySelector("button");
  button.disabled = true;
  try {
    await api("/api/messages", {
      author: element("author").value,
      message: element("message").value,
    });
    element("message").value = "";
    element("error").textContent = "";
    clearTimeout(timer);
    await refresh();
  } catch (error) {
    element("error").textContent = `Message not confirmed. Check the chat before retrying. ${error.message}`;
  } finally {
    button.disabled = false;
  }
});
