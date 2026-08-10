// The wiring half: a real WebSocket, a real xterm.js, and the inventory the page lists.
//
// Every protocol DECISION lives in `protocol.js`, and the reasoning for all of them lives in
// `packages/shellbox-app/src/shellbox_app/client.py`. This file performs actions and owns no
// rules -- if you find yourself adding an `if` about the protocol here, it belongs there, where
// `tests/unit/test_client_protocol.py` can fail on it.
//
// CRITICAL: nothing in this file uses `innerHTML`. Inventory rows carry an `owner_email`, a
// `cwd` and a `tmux_name` that a SANDBOX wrote, so they are untrusted text from this page's
// point of view. They are rendered through `textContent` and `createElement`, which cannot
// execute markup. A single `innerHTML` with a template literal is how that stops being true.

import { NOTICE_NO_PUBLISHER, SubscriberClient } from "./protocol.js";

// One clock for both the frame stamps and the deadlines, matching the Python twin, which takes
// a single `now`.
//
// It is the WALL clock rather than `performance.now()`, and the trade is worth stating.
// `Frame.t` is documented as "wall-clock Unix seconds, for display and diagnostics only, never
// for ordering", so a monotonic value would put seconds-since-page-load in a field an operator
// reads as a timestamp. The cost is that a clock step during a session can expire one deadline
// early or late, which costs a reconnect -- an event this client already handles four times an
// hour by design.
const now = () => Date.now() / 1000;

// How often the deadlines are advanced. The shortest one is 5 s, so a 1 s tick is finer than
// anything it resolves and costs nothing.
const TICK_MS = 1000;

// How often the inventory is re-read while a terminal is attached. It supplies the DETAIL in
// `R51`'s banner -- which sandbox to go and start -- so it has to be fresher than the failure
// it describes, and it is one indexed read against a database the App already holds open.
const INVENTORY_REFRESH_MS = 30000;

const el = (id) => document.getElementById(id);

/** One attached terminal: its socket, its protocol state, and its xterm. */
class Attachment {
  constructor(sessionId, term, fit) {
    this.sessionId = sessionId;
    this.term = term;
    this.fit = fit;
    this.client = new SubscriberClient(sessionId);
    this.socket = null;
    this.timer = null;
    this.stopped = false;
  }

  start() {
    this.timer = setInterval(() => this.#drive(this.client.ticked(now())), TICK_MS);
    this.term.onData((text) => this.#drive(this.client.typed(new TextEncoder().encode(text), now())));
    this.term.onResize(({ cols, rows }) => this.#drive(this.client.resized(cols, rows, now())));
    this.#open();
  }

  stop() {
    this.stopped = true;
    if (this.timer !== null) clearInterval(this.timer);
    this.#drop();
  }

  /** The inventory row for this session's host, for `R51`'s banner. Detail, never a gate. */
  noteHost(host) {
    this.client.noteHost(host);
  }

  #open() {
    if (this.stopped) return;
    // Same origin, so the Apps edge applies the same authentication it applied to this page.
    // A `wss://` URL built from `location` cannot point anywhere else, which is also why no
    // token is attached here: the browser's session cookie is the credential.
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${scheme}//${location.host}/subscribe/${encodeURIComponent(this.sessionId)}`;
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    this.socket = socket;

    socket.onopen = () => this.#drive(this.client.opened(now()));
    socket.onmessage = (event) => this.#drive(this.client.received(event.data, now()));
    // `onclose` and `onerror` are the same event to this client: the edge kills sockets with NO
    // close frame, so there is no reason to distinguish them, and `closed()` is idempotent per
    // socket anyway.
    socket.onclose = () => this.#closed(socket);
    socket.onerror = () => this.#closed(socket);
  }

  #closed(socket) {
    // Ignore a late event from a socket this attachment has already moved on from, or a
    // reconnect would be scheduled twice.
    if (socket !== this.socket) return;
    this.socket = null;
    this.#drive(this.client.closed());
  }

  #drop() {
    const socket = this.socket;
    this.socket = null;
    if (socket !== null) {
      socket.onclose = null;
      socket.onerror = null;
      socket.close();
    }
  }

  #drive(actions) {
    for (const action of actions) {
      switch (action.type) {
        case "send":
          // A socket can die between the decision and the write. The state machine finds out
          // through `onclose`, which is already the path a dead socket takes.
          if (this.socket !== null && this.socket.readyState === WebSocket.OPEN) {
            this.socket.send(action.payload);
          }
          break;
        case "write":
          this.term.write(action.data);
          // Output arrived, so whatever the banner was saying about not receiving any is over.
          // Retraction is driven by `write` and `reset` ALONE -- the two actions that prove a
          // publisher answered. Clearing on every non-notice action instead would let an
          // outbound `send` (a resize under a stale epoch) wipe a standing "no publisher
          // attached" message while the state it names is still true.
          clearBanner();
          break;
        case "reset":
          // CRITICAL: a reset, then the repaint. NEVER `write` alone -- appending a repaint
          // duplicates every visible line and leaves the parser mid-escape, and that failure
          // gets diagnosed as "xterm.js is buggy". `protocol.js` gives this its own action type
          // so the two cannot be collapsed by accident.
          this.term.reset();
          this.term.write(action.repaint);
          clearBanner();
          break;
        case "notice":
          setBanner(action.text, action.code === NOTICE_NO_PUBLISHER ? "warn" : "info");
          break;
        case "redial":
          this.#drop();
          setBanner(`reconnecting in ${action.delay.toFixed(1)}s...`, "info");
          setTimeout(() => this.#open(), action.delay * 1000);
          break;
        case "stop":
          this.stop();
          setBanner(action.text, "error");
          break;
      }
    }
  }
}

function setBanner(text, level) {
  const banner = el("banner");
  banner.textContent = text;
  banner.className = `banner ${level}`;
  banner.hidden = false;
}

function clearBanner() {
  const banner = el("banner");
  banner.hidden = true;
  banner.textContent = "";
}

/** Both inventory routes, as one object. A failure degrades the page; it never blanks it. */
async function loadInventory() {
  const read = async (path) => {
    const response = await fetch(path, { headers: { accept: "application/json" } });
    if (!response.ok) throw new Error(`${path} answered ${response.status}`);
    return response.json();
  };
  const [hosts, sessions] = await Promise.all([read("/api/hosts"), read("/api/sessions")]);
  return { hosts, sessions };
}

function renderInventory(inventory) {
  const { hosts, sessions } = inventory;
  el("viewer").textContent = sessions.viewer_email ?? "unknown viewer";

  // `stale` is why an empty list is not enough on its own: a `NullRegistry` returns `[]` and
  // never raises, so "the App cannot see anything" and "there is nothing to show" arrive
  // looking identical. The flag is what separates them, and it is reported rather than hidden.
  const degraded = hosts.stale || sessions.stale;
  const note = el("inventory-note");
  note.hidden = !degraded;
  if (degraded) {
    const reason = hosts.reason ?? sessions.reason ?? "unknown";
    note.textContent = `the inventory is stale (${reason}); terminals still work.`;
  }

  const byHost = new Map(hosts.hosts.map((host) => [host.host_id, host]));
  const body = el("sessions");
  body.replaceChildren();

  if (sessions.sessions.length === 0) {
    const row = body.insertRow();
    const cell = row.insertCell();
    cell.colSpan = 5;
    cell.className = "empty";
    cell.textContent = degraded
      ? "the App cannot read its registry, so it has nothing to list."
      : "no sessions are registered yet.";
    return;
  }

  for (const session of sessions.sessions) {
    const host = byHost.get(session.host_id);
    const row = body.insertRow();

    const name = row.insertCell();
    const link = document.createElement("a");
    link.href = `#session=${encodeURIComponent(session.session_id)}`;
    link.textContent = session.tmux_name;
    name.append(link);

    // The NULL `sandbox_id` label, taken from the server rather than derived here. It is the
    // single most informative thing a row can carry -- the primary failure mode is a stopped
    // sandbox a human must go start -- and `inventory.py` owns the rule so that two places
    // cannot disagree about it.
    text(row, host ? host.sandbox_label : "unknown host");
    text(row, session.owner_email + (session.mine ? " (you)" : ""));
    text(row, session.status);
    text(row, `${session.cols}x${session.rows}`);
  }
}

function text(row, value) {
  row.insertCell().textContent = value;
}

/** The `#session=<id>` fragment, or null for the inventory view. */
function selectedSession() {
  const match = /^#session=(.+)$/.exec(location.hash);
  return match === null ? null : decodeURIComponent(match[1]);
}

let attachment = null;

async function route() {
  if (attachment !== null) {
    attachment.stop();
    attachment = null;
  }
  clearBanner();

  const sessionId = selectedSession();
  el("inventory-view").hidden = sessionId !== null;
  el("terminal-view").hidden = sessionId === null;

  let inventory = null;
  try {
    inventory = await loadInventory();
    renderInventory(inventory);
  } catch (error) {
    // ADR-3's contract, applied to the page: a registry failure degrades the inventory and
    // never the relay. A terminal must still attach when this read fails.
    console.warn("shellbox: could not read the inventory", error);
    el("inventory-note").hidden = false;
    el("inventory-note").textContent = "could not read the inventory; terminals still work.";
  }

  if (sessionId === null) return;

  el("session-name").textContent = sessionId;
  const term = new window.Terminal({
    // The pane's bytes are already a terminal stream: `\n` means what the pane meant by it, and
    // rewriting it would corrupt output this transport is careful to carry byte-exact.
    convertEol: false,
    cursorBlink: true,
    fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
    fontSize: 13,
    scrollback: 5000,
    theme: { background: "#11151c", foreground: "#d7dde5", cursor: "#7aa2f7" },
  });
  const fit = new window.FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el("terminal"));
  fit.fit();

  attachment = new Attachment(sessionId, term, fit);
  applyHost(attachment, inventory, sessionId);
  attachment.start();
  term.focus();

  // Re-read the inventory on a timer so the banner's detail keeps up with the sandbox it names.
  //
  // `mine` is captured, and the identity check against the module-level `attachment` is what
  // makes this safe across navigation. Reading the global instead would let a timer left over
  // from a previous session apply ITS host row to whatever terminal is open now.
  const mine = attachment;
  const refresh = setInterval(async () => {
    if (attachment !== mine) {
      clearInterval(refresh);
      return;
    }
    try {
      applyHost(mine, await loadInventory(), sessionId);
    } catch {
      // A failed refresh costs the banner its detail, never its correctness.
    }
  }, INVENTORY_REFRESH_MS);
}

function applyHost(target, inventory, sessionId) {
  if (inventory === null) return;
  const session = inventory.sessions.sessions.find((row) => row.session_id === sessionId);
  if (session === undefined) return;
  const host = inventory.hosts.hosts.find((row) => row.host_id === session.host_id);
  if (host === undefined) return;
  target.noteHost({
    hostId: host.host_id,
    sandboxLabel: host.sandbox_label,
    status: host.status,
  });
}

window.addEventListener("hashchange", () => {
  void route();
});
window.addEventListener("resize", () => {
  if (attachment !== null) attachment.fit.fit();
});

void route();
