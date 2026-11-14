"use strict";

const webApp = window.Telegram && window.Telegram.WebApp;
const statusNode = document.getElementById("status");
const transcriptNode = document.getElementById("transcript");
const timerNode = document.getElementById("timer");
const micNode = document.getElementById("micState");
const playback = document.getElementById("playback");
const startButton = document.getElementById("start");
const interruptButton = document.getElementById("interrupt");
const finishButton = document.getElementById("finish");
const reconnectButton = document.getElementById("reconnect");
const durationNote = document.getElementById("durationNote");

const history = [];
let peer = null;
let channel = null;
let microphone = null;
let startedAt = 0;
let timer = null;
let active = false;
let finishing = false;
let closing = false;
let reconnectAvailable = false;
let userPartial = "";
let assistantPartial = "";
let finishTimer = null;
let connectTimer = null;
let responseInProgress = false;
let finishPromptSent = false;
let maxSessionSeconds = 300;
let sessionId = null;
let sessionHistoryStart = 0;
const contextPromise = (async () => {
  if (!webApp || !webApp.initData) return [];
  try {
    const response = await fetch("/voice/api/context", {
      cache: "no-store",
      headers: { "X-Telegram-Init-Data": webApp.initData },
    });
    if (!response.ok) return [];
    const value = await response.json();
    return Array.isArray(value.history) ? value.history.filter((turn) =>
      turn && ["user", "assistant"].includes(turn.role) && typeof turn.text === "string") : [];
  } catch {
    return [];
  }
})();

if (webApp) {
  webApp.ready();
  webApp.expand();
}

function setStatus(value) {
  statusNode.textContent = value;
}

function setButtons() {
  startButton.disabled = active;
  interruptButton.disabled = !active || finishing || !channel || channel.readyState !== "open";
  finishButton.disabled = !active;
  reconnectButton.hidden = active || !reconnectAvailable;
  startButton.hidden = active && history.length > 0;
}

function renderHistory() {
  transcriptNode.replaceChildren();
  if (!history.length && !userPartial && !assistantPartial) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "Suhbat boshlanganidan keyin gaplar shu yerda ko‘rinadi.";
    transcriptNode.append(empty);
    return;
  }
  for (const entry of history) {
    const row = document.createElement("p");
    row.className = "turn";
    const label = document.createElement("strong");
    label.textContent = entry.role === "user" ? "Siz" : "Saida";
    row.append(label, document.createTextNode(entry.text));
    transcriptNode.append(row);
  }
  for (const [role, text] of [["user", userPartial], ["assistant", assistantPartial]]) {
    if (!text) continue;
    const row = document.createElement("p");
    row.className = "turn partial";
    const label = document.createElement("strong");
    label.textContent = role === "user" ? "Siz · eshitilyapti" : "Saida · gapiryapti";
    row.append(label, document.createTextNode(text));
    transcriptNode.append(row);
  }
  transcriptNode.scrollTop = transcriptNode.scrollHeight;
}

function remember(role, text) {
  const clean = String(text || "").trim().slice(0, 1800);
  if (!clean) return;
  history.push({ role, text: clean });
  let chars = history.reduce((sum, item) => sum + item.text.length, 0);
  while (history.length > 16 || chars > 6000) {
    const removed = history.shift();
    chars -= removed ? removed.text.length : 0;
  }
}

function finalizePartial(role, finalText) {
  const text = String(finalText || (role === "user" ? userPartial : assistantPartial)).trim();
  if (role === "user") userPartial = "";
  else assistantPartial = "";
  if (text) remember(role, text);
  renderHistory();
}

function onServerEvent(raw) {
  let event;
  try { event = JSON.parse(raw); } catch { return; }
  switch (event.type) {
    case "input_audio_buffer.speech_started":
      micNode.textContent = "Gapiryapsiz";
      if (finishing) {
        finishing = false;
        if (finishTimer) clearTimeout(finishTimer);
        finishTimer = null;
      }
      setStatus("Sizni tinglayapman");
      break;
    case "input_audio_buffer.speech_stopped":
      micNode.textContent = "Mikrofon faol";
      setStatus("Javob tayyorlayapman");
      break;
    case "conversation.item.input_audio_transcription.delta":
      userPartial += String(event.delta || "");
      renderHistory();
      break;
    case "conversation.item.input_audio_transcription.completed":
      finalizePartial("user", event.transcript);
      break;
    case "response.output_audio_transcript.delta":
      assistantPartial += String(event.delta || "");
      renderHistory();
      break;
    case "response.output_audio_transcript.done":
      finalizePartial("assistant", event.transcript);
      break;
    case "response.created":
      responseInProgress = true;
      setStatus(finishing ? "Suhbat xulosasini tayyorlayapman" : "Saida javob beryapti");
      break;
    case "response.done":
      responseInProgress = false;
      if (finishing && !finishPromptSent) {
        requestFinishSummary();
      } else if (finishing) {
        if (finishTimer) clearTimeout(finishTimer);
        setStatus("Xulosa tayyor. Sessiya yakunlanmoqda");
        finishTimer = setTimeout(closeSession, 3500);
      } else if (active) {
        setStatus("Suhbat davom etmoqda");
      }
      break;
    case "error":
      setStatus("Ovoz sessiyasida xatolik. Qayta ulanishni sinab ko‘ring.");
      break;
    default:
      break;
  }
}

function stopMedia() {
  if (sessionId) {
    const endingSessionId = sessionId;
    sessionId = null;
    void endRealtimeSession(endingSessionId, history.slice(sessionHistoryStart));
  }
  stopMicrophone();
  if (peer) {
    peer.ontrack = null;
    peer.onconnectionstatechange = null;
    peer.close();
    peer = null;
  }
  if (channel) {
    channel.onopen = null;
    channel.onmessage = null;
    channel.close();
    channel = null;
  }
  playback.srcObject = null;
  micNode.textContent = "Mikrofon o‘chiq";
}

function stopMicrophone() {
  if (microphone) {
    for (const track of microphone.getTracks()) track.stop();
    microphone = null;
  }
  micNode.textContent = "Mikrofon o‘chiq";
}

function closeSession(message = "Sessiya tugadi. Xulosa shu oynada qoldi.") {
  closing = true;
  active = false;
  finishing = false;
  responseInProgress = false;
  finishPromptSent = false;
  const endingSessionId = sessionId;
  sessionId = null;
  if (endingSessionId) void endRealtimeSession(endingSessionId, history.slice(sessionHistoryStart));
  stopMedia();
  if (timer) clearInterval(timer);
  timer = null;
  if (finishTimer) clearTimeout(finishTimer);
  finishTimer = null;
  if (connectTimer) clearTimeout(connectTimer);
  connectTimer = null;
  setStatus(message);
  setButtons();
  closing = false;
}

async function endRealtimeSession(id, turns = []) {
  if (!webApp || !webApp.initData) return;
  try {
    const response = await fetch("/voice/api/session/end", {
      method: "POST",
      cache: "no-store",
      keepalive: true,
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": webApp.initData },
      body: JSON.stringify({ session_id: id, turns }),
    });
    if (response.ok) {
      const result = await response.json();
      if (!active && result.archive_status === "saved") {
        setStatus("Suhbat tugadi. Transkript shifrlangan yordamchi xotirasida saqlandi.");
      } else if (!active && result.archive_status === "disabled" && turns.length) {
        setStatus("Suhbat tugadi. Transkript arxivga saqlanmadi; buni /privacy orqali alohida yoqishingiz mumkin.");
      } else if (!active && result.archive_status === "failed") {
        setStatus("Sessiya tugadi, ammo transkriptni saqlab bo‘lmadi.");
      }
    }
  } catch {
    // The server-side duration limit will close the session if this request is lost.
  }
}

function updateTimer() {
  const remaining = Math.max(0, maxSessionSeconds - Math.floor((Date.now() - startedAt) / 1000));
  const minutes = String(Math.floor(remaining / 60)).padStart(2, "0");
  const seconds = String(remaining % 60).padStart(2, "0");
  timerNode.textContent = `${minutes}:${seconds}`;
  if (remaining === 0 && active) {
    closeSession("Sessiya vaqti tugadi. Suhbat matni shu oynada qoldi.");
  }
}

async function startSession() {
  if (!webApp || !webApp.initData) {
    setStatus("Sahifani Telegram bot ichidagi tugma orqali oching.");
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setStatus("Bu Telegram ilovasida mikrofon ochilmadi; yangilab qayta urinib ko‘ring.");
    return;
  }
  closing = false;
  sessionHistoryStart = history.length;
  if (connectTimer) clearTimeout(connectTimer);
  startButton.disabled = true;
  reconnectButton.hidden = true;
  setStatus("Mikrofonga ruxsat so‘ralmoqda");
  try {
    microphone = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    peer = new RTCPeerConnection();
    connectTimer = setTimeout(() => {
      if (!active && !closing) {
        stopMedia();
        setStatus("Ulanish vaqti tugadi. Qayta ulanishni sinab ko‘ring.");
        reconnectAvailable = reconnectAvailable || history.length > 0;
        setButtons();
      }
    }, 30000);
    peer.ontrack = (event) => {
      playback.srcObject = event.streams[0];
      playback.play().catch(() => setStatus("Ovoz chiqishi uchun ekranga teging."));
    };
    peer.onconnectionstatechange = () => {
      if (["failed", "disconnected", "closed"].includes(peer?.connectionState) && active && !closing) {
        active = false;
        reconnectAvailable = true;
        stopMedia();
        setStatus("Ulanish uzildi. Qayta ulanish tugmasini bosing.");
        setButtons();
      }
    };
    for (const track of microphone.getTracks()) peer.addTrack(track, microphone);
    channel = peer.createDataChannel("oai-events");
    channel.onopen = () => {
      if (connectTimer) clearTimeout(connectTimer);
      connectTimer = null;
      active = true;
      reconnectAvailable = true;
      responseInProgress = false;
      finishPromptSent = false;
      startedAt = Date.now();
      timerNode.textContent = `${String(Math.floor(maxSessionSeconds / 60)).padStart(2, "0")}:${String(maxSessionSeconds % 60).padStart(2, "0")}`;
      micNode.textContent = "Mikrofon faol";
      setStatus("Suhbat boshlandi. Gapirishingiz mumkin.");
      setButtons();
      if (timer) clearInterval(timer);
      timer = setInterval(updateTimer, 1000);
    };
    channel.onmessage = (event) => onServerEvent(event.data);
    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    const savedContext = await contextPromise;
    const sessionResponse = await fetch("/voice/api/session", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Telegram-Init-Data": webApp.initData },
      body: JSON.stringify({ sdp: offer.sdp, history: [...savedContext, ...history] }),
    });
    if (!sessionResponse.ok) throw new Error(`session-${sessionResponse.status}`);
    const session = await sessionResponse.json();
    if (typeof session.session_id !== "string" || typeof session.sdp !== "string") {
      throw new Error("session-invalid");
    }
    sessionId = session.session_id;
    maxSessionSeconds = Math.max(30, Math.min(900, Number(session.max_seconds) || 300));
    const maxMinutes = Math.ceil(maxSessionSeconds / 60);
    durationNote.textContent = `Suhbat ${maxMinutes} daqiqadan keyin server tomonidan yakunlanadi.`;
    await peer.setRemoteDescription({ type: "answer", sdp: session.sdp });
  } catch (error) {
    if (connectTimer) clearTimeout(connectTimer);
    connectTimer = null;
    if (sessionId) {
      const failedSessionId = sessionId;
      sessionId = null;
      void endRealtimeSession(failedSessionId, history.slice(sessionHistoryStart));
    }
    stopMedia();
    active = false;
    const code = String(error && error.message || "");
    setStatus(code === "NotAllowedError" ? "Mikrofon ruxsati berilmadi." :
      "Ulanib bo‘lmadi. HTTPS, Telegram Mini App ruxsati va OpenAI sozlamasini tekshiring.");
    reconnectAvailable = reconnectAvailable || history.length > 0;
    setButtons();
  }
}

function sendEvent(value) {
  if (channel && channel.readyState === "open") channel.send(JSON.stringify(value));
}

function finishSession() {
  if (!active || finishing) return;
  if (!channel || channel.readyState !== "open") {
    closeSession();
    return;
  }
  finishing = true;
  stopMicrophone();
  if (timer) clearInterval(timer);
  timer = null;
  finishPromptSent = false;
  if (responseInProgress) sendEvent({ type: "response.cancel" });
  else requestFinishSummary();
  setStatus("Suhbat xulosasini tayyorlayapman");
  setButtons();
  finishTimer = setTimeout(closeSession, 20000);
}

function requestFinishSummary() {
  if (!finishing || finishPromptSent) return;
  finishPromptSent = true;
  sendEvent({
    type: "conversation.item.create",
    item: { type: "message", role: "user", content: [{
      type: "input_text",
      text: "Hozirgi suhbatimizni o‘zbek tilida uch jumladan oshmaydigan aniq xulosa qilib ayt. Yangi savolga javob berma.",
    }] },
  });
  sendEvent({ type: "response.create" });
  setStatus("Suhbat xulosasini tayyorlayapman");
}

function interruptResponse() {
  sendEvent({ type: "response.cancel" });
  setStatus("Javob to‘xtatildi. Gapirishingiz mumkin.");
}

startButton.addEventListener("click", () => startSession());
reconnectButton.addEventListener("click", () => startSession());
interruptButton.addEventListener("click", interruptResponse);
finishButton.addEventListener("click", finishSession);

if (webApp) {
  webApp.onEvent("deactivated", () => {
    if (active) closeSession();
  });
}
window.addEventListener("pagehide", closeSession);
setButtons();
renderHistory();
