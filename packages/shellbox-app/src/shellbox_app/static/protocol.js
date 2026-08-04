// The subscriber's protocol state machine, in the browser.
//
// A transcription of `packages/shellbox-app/src/shellbox_app/client.py`, which is the
// SPECIFICATION and carries the full reasoning for every rule below. Read that file first. This
// one repeats only what a reader needs in order not to break it.
//
// WARNING: nothing executes both implementations. `tests/unit/test_client_parity.py` compares
// the constants declared here against the Python module's, so a deadline or a code cannot move
// on one side alone -- it cannot compare logic. ADR-23 accepted that cost deliberately: the
// alternative was a browser test lane and the whole JavaScript toolchain that comes with it.
// If you change a BEHAVIOUR here, change `client.py` in the same commit and extend
// `tests/unit/test_client_protocol.py`, which is where the behaviour is actually asserted.
//
// Like its Python twin this file holds no socket, no terminal, no clock and no randomness it
// was not given. Events go in, actions come out, and `terminal.js` performs them.

import {
  CLOSED_DETACHED,
  CLOSED_TERMINAL_GONE,
  CONTROL_CLOSED,
  CONTROL_ERROR,
  CONTROL_HELLO,
  CONTROL_RESYNC,
  CodecError,
  FIELD_BASE_SEQ,
  FIELD_CODE,
  FIELD_MESSAGE,
  FIELD_REASON,
  FIELD_SESSION_ID,
  FIELD_VIEWER_EMAIL,
  STREAM_CONTROL,
  UNORDERED_SEQ,
  controlFrame,
  decodeControl,
  decodeFrame,
  inputMessage,
  resizeMessage,
  resumeMessage,
} from "./codec.js";

// How long a 101 has to become a `hello`. A 101 is NOT proof of a working transport: the Apps
// edge answers an unauthenticated upgrade with a 302 and an unauthenticated POST with an HTML
// login page under a 200, both measured.
export const HELLO_DEADLINE_SECONDS = 5.0;

// How long after `hello` with NO frame at all before `R51`'s state is named. Derived from the
// publisher's `backoff_cap` of 5 s plus dial headroom. It errs SHORT on purpose: the banner
// retracts itself on the first frame, so a false one costs a message that vanishes while a
// deadline set too long costs a blank terminal on the failure this exists to name.
export const NO_PUBLISHER_DEADLINE_SECONDS = 8.0;

// How long `subscriber_conflict` is retried before it is reported. ADR-20.
//
// NORMATIVE: this must exceed the App's `ws_ping_interval + ws_ping_timeout`, which is 40 s.
// That sum is the worst-case time a silently-dead subscriber holds the session's one slot --
// the App has no reaper of its own, so what frees the slot is uvicorn failing its own ping. A
// browser that gave up sooner would report a conflict that was about to clear itself, and the
// most likely holder of that slot is this viewer's OWN previous socket, killed moments earlier.
export const SUBSCRIBER_CONFLICT_BOUND_SECONDS = 45.0;

// Full jitter, re-drawn per attempt and NOT widened. The edge kill is a SYNCHRONIZED global
// event, so a fixed delay would keep every client in lockstep with it. The floor is nonzero
// because a socket can die seconds after opening, so a zero-delay retry can hot-loop straight
// into an imminent kill.
export const BACKOFF_FLOOR_SECONDS = 0.5;
export const BACKOFF_CAP_SECONDS = 5.0;

// The stable prefix of `R51`'s message. `terminal.js` branches on `notice.code`, never on this
// text, so the text is free to gain detail and must not lose this opening.
export const NO_PUBLISHER_MESSAGE = "no publisher attached";

// Advisory: the terminal stays usable and the socket stays up.
export const NOTICE_NO_PUBLISHER = "no_publisher";
export const NOTICE_DETACHED = "detached";
export const NOTICE_STREAM_GAP = "stream_gap";

// Terminal for this viewer, and nothing is re-dialled.
export const CODE_SUBSCRIBER_CONFLICT = "subscriber_conflict";
export const CODE_PUBLISHER_CONFLICT = "publisher_conflict";
export const CODE_TERMINAL_GONE = CLOSED_TERMINAL_GONE;
export const CODE_SESSION_MISMATCH = "session_mismatch";

export const PHASE_DIALING = "dialing";
export const PHASE_AWAITING_HELLO = "awaiting_hello";
export const PHASE_LIVE = "live";
export const PHASE_STOPPED = "stopped";

// A uuid4, shape-checked rather than merely tested for emptiness. A value that can be truncated
// or padded into looking equal to another defeats the detection epochs exist for.
const EPOCH_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const send = (payload) => ({ type: "send", payload });
const write = (data) => ({ type: "write", data });
const reset = (repaint) => ({ type: "reset", repaint });
const notice = (code, text) => ({ type: "notice", code, text });

/**
 * One viewer's protocol state, across every socket it will hold for one session.
 *
 * It OUTLIVES its sockets, which is the point: the epoch and the last rendered `seq` are what a
 * reconnect resumes from, so they cannot live in the connection.
 */
export class SubscriberClient {
  constructor(sessionId, options = {}) {
    this.sessionId = sessionId;
    this.helloDeadline = options.helloDeadline ?? HELLO_DEADLINE_SECONDS;
    this.noPublisherDeadline = options.noPublisherDeadline ?? NO_PUBLISHER_DEADLINE_SECONDS;
    this.conflictBound = options.conflictBound ?? SUBSCRIBER_CONFLICT_BOUND_SECONDS;
    this.backoffFloor = options.backoffFloor ?? BACKOFF_FLOOR_SECONDS;
    this.backoffCap = options.backoffCap ?? BACKOFF_CAP_SECONDS;
    this.random = options.random ?? Math.random;

    this.phase = PHASE_DIALING;
    this.epoch = null;
    this.lastSeq = 0;
    this.viewerEmail = null;
    this.decodeFailures = 0;

    this.host = null;
    this.size = null;
    this.openedAt = null;
    this.helloAt = null;
    this.conflictSince = null;
    this.seenFrame = false;
    this.reportedNoPublisher = false;
  }

  /** The inventory row for this session's host, for `R51`'s banner detail. Never a gate. */
  noteHost(host) {
    this.host = host;
  }

  /** The socket completed its upgrade. Arms the `hello` deadline. NOT "connected". */
  opened(now) {
    if (this.phase === PHASE_STOPPED) return [];
    this.phase = PHASE_AWAITING_HELLO;
    this.openedAt = now;
    this.helloAt = null;
    this.seenFrame = false;
    this.reportedNoPublisher = false;
    return [];
  }

  /** One inbound WebSocket message. The whole of the render decision. */
  received(raw, now) {
    if (this.phase === PHASE_STOPPED) return [];
    if (typeof raw === "string") {
      // The protocol is binary end to end. Rendering a text message would put a login page's
      // HTML into a terminal.
      return this.#dropped("a text message arrived; frames are binary");
    }
    let frame;
    try {
      frame = decodeFrame(raw);
    } catch (error) {
      if (!(error instanceof CodecError)) throw error;
      return this.#dropped(`undecodable frame: ${error.message}`);
    }
    if (frame.stream !== STREAM_CONTROL) {
      return this.#data(frame.seq, frame.data);
    }
    let message;
    try {
      message = decodeControl(frame.data);
    } catch (error) {
      if (!(error instanceof CodecError)) throw error;
      return this.#dropped(`undecodable control frame: ${error.message}`);
    }

    // CHECKED BEFORE THE PHASE. A refusal arrives INSTEAD of `hello`, not after it: the App
    // refuses, sends the reason and closes without ever binding. Routed by phase, a refusal
    // reaches the "first frame was not a hello" branch and is retried as a plain transient with
    // no bound -- so `subscriber_conflict` never reaches the 45 s window and never surfaces.
    if (message.kind === CONTROL_ERROR) return this.#refused(message, now);
    if (this.phase === PHASE_AWAITING_HELLO) return this.#first(message, now);
    return this.#control(message, now);
  }

  /** Advance the deadlines. Called on a timer; reads no clock of its own. */
  ticked(now) {
    if (this.phase === PHASE_AWAITING_HELLO) {
      if (this.openedAt !== null && now - this.openedAt >= this.helloDeadline) {
        return [this.#redial()];
      }
      return [];
    }
    if (this.phase !== PHASE_LIVE) return [];
    if (this.seenFrame || this.reportedNoPublisher || this.helloAt === null) return [];
    if (now - this.helloAt < this.noPublisherDeadline) return [];
    this.reportedNoPublisher = true;
    return [notice(NOTICE_NO_PUBLISHER, this.#noPublisherText())];
  }

  /**
   * The socket died. Routine, roughly every 10 to 18 minutes.
   *
   * Returns nothing when a redial or a stop was already emitted for this socket, so a refusal
   * followed by the server's close does not schedule two dials.
   */
  closed() {
    if (this.phase === PHASE_STOPPED || this.phase === PHASE_DIALING) return [];
    return [this.#redial()];
  }

  /** A keystroke. Byte-exact to the pty, with no allowlist. */
  typed(data, now) {
    if (this.phase !== PHASE_LIVE || data.length === 0) return [];
    return [send(this.#encode(inputMessage(data), now))];
  }

  /**
   * The viewport changed. Remembered, and sent when there is an epoch to send it under.
   *
   * A subscriber holds no attach of its own, so the only honest epoch is the publisher's last
   * stated one. Before one arrives the size is remembered and nothing is sent; it goes out on
   * the next `hello`, and again whenever the epoch changes -- which is what re-asserts the
   * viewer's size across a publisher restart.
   */
  resized(cols, rows, now) {
    if (cols < 1 || rows < 1) return [];
    if (this.size !== null && this.size[0] === cols && this.size[1] === rows) return [];
    this.size = [cols, rows];
    return this.#resize(now);
  }

  /** Full jitter: uniform(floor, cap), drawn fresh for every attempt. */
  nextDelay() {
    return this.backoffFloor + this.random() * (this.backoffCap - this.backoffFloor);
  }

  // ------------------------------------------------------------------ internals

  #first(message, now) {
    if (message.kind !== CONTROL_HELLO) return [this.#redial()];

    const bound = message.fields[FIELD_SESSION_ID];
    if (bound !== this.sessionId) {
      // CRITICAL: an ERROR, never a warning. The server bound a session this viewer did not
      // dial, so rendering the stream would put another agent's terminal on this page. No retry
      // can make that right.
      return [
        this.#stop(
          CODE_SESSION_MISMATCH,
          `the server bound session "${bound}", but this page opened "${this.sessionId}"; ` +
            "refusing to render another session's terminal",
        ),
      ];
    }

    const viewer = message.fields[FIELD_VIEWER_EMAIL];
    this.viewerEmail = typeof viewer === "string" ? viewer : null;
    this.phase = PHASE_LIVE;
    this.helloAt = now;
    // A hello is proof the slot was free, so the conflict window starts fresh next time.
    this.conflictSince = null;

    // The question `R51`'s detector rests on: a live publisher MUST answer a fresh resume with
    // a resync, so silence afterwards is evidence about the publisher and not about the pane.
    const actions = [send(this.#encode(resumeMessage(this.lastSeq, this.#parsedEpoch()), now))];
    return actions.concat(this.#resize(now));
  }

  #control(message, now) {
    if (message.kind === CONTROL_RESYNC) return this.#resync(message, now);
    if (message.kind === CONTROL_CLOSED) return this.#closedMessage(message);
    if (message.kind === CONTROL_HELLO) return [];
    // `input` and `resize` travel the other way. Anything else is from a release this client
    // does not know, and the safe response to both is to ignore it.
    return [];
  }

  #refused(message, now) {
    const code = message.fields[FIELD_CODE];
    const text = message.fields[FIELD_MESSAGE];
    const reason = typeof text === "string" ? text : "the server refused this socket";

    if (code !== CODE_SUBSCRIBER_CONFLICT) {
      // `publisher_conflict` cannot reach a subscriber's route, and an unknown code is not
      // something to retry blindly. Both stop and surface what the server said.
      return [this.#stop(typeof code === "string" ? code : "refused", reason)];
    }
    if (this.conflictSince === null) this.conflictSince = now;
    const waited = now - this.conflictSince;
    if (waited >= this.conflictBound) {
      return [
        this.#stop(
          CODE_SUBSCRIBER_CONFLICT,
          `${reason} (still held after ${Math.round(waited)}s). Close the other tab or window ` +
            "viewing this session, then reload.",
        ),
      ];
    }
    return [this.#redial()];
  }

  /**
   * The declared discontinuity. A full RESET, never appended.
   *
   * D7 rules out a frame log, so resume repaints rather than gap-filling. Appending a repaint
   * duplicates every visible line and leaves the terminal's parser mid-escape, and that failure
   * gets diagnosed as "xterm.js is buggy".
   */
  #resync(message, now) {
    this.#noteEpoch(message.epoch);
    const base = message.fields[FIELD_BASE_SEQ];
    if (Number.isInteger(base)) this.lastSeq = base;
    this.seenFrame = true;
    this.reportedNoPublisher = false;
    // The epoch just changed under us, so the viewer's size has to be re-asserted against it.
    return [reset(message.payload)].concat(this.#resize(now));
  }

  /**
   * The pane's stream ended, and which of the two ways.
   *
   * CRITICAL: the reasons must not be collapsed, and the cost is asymmetric. `terminal_gone`
   * means the process exited and there is nothing left to watch. `detached` means only that
   * attach client went away, and a viewer that read it as `terminal_gone` would tear down a
   * session that is still running.
   */
  #closedMessage(message) {
    const reason = message.fields[FIELD_REASON];
    if (reason === CLOSED_TERMINAL_GONE) {
      return [
        this.#stop(
          CODE_TERMINAL_GONE,
          "the terminal's process exited. Its final output is above; there is nothing further " +
            "to stream.",
        ),
      ];
    }
    if (reason === CLOSED_DETACHED) {
      return [
        notice(
          NOTICE_DETACHED,
          "the publisher detached from this session. The session is still running, and output " +
            "resumes when it re-attaches.",
        ),
      ];
    }
    return [];
  }

  #data(seq, data) {
    if (this.phase !== PHASE_LIVE) {
      // The gate exists precisely so that bytes from an unverified peer do not reach the
      // terminal.
      return this.#dropped("a data frame arrived before hello");
    }
    const actions = [];
    this.reportedNoPublisher = false;
    if (seq > this.lastSeq + 1 && this.lastSeq > 0) {
      // Gap-free is a construction property of the publisher's allocator, so a jump inside one
      // epoch is a publisher bug rather than a lossy network. Surfaced, and the bytes still
      // render: there is no resync-request path to take instead, and dropping them would turn a
      // reportable defect into a silent one.
      actions.push(
        notice(
          NOTICE_STREAM_GAP,
          `the stream skipped from ${this.lastSeq} to ${seq}; output may be missing.`,
        ),
      );
    }
    this.seenFrame = true;
    this.lastSeq = Math.max(this.lastSeq, seq);
    actions.push(write(data));
    return actions;
  }

  /**
   * Adopt an epoch seen on a control frame, resetting the ordinal space if it changed.
   *
   * CRITICAL: `seq` restarts in every epoch, so carrying `lastSeq` across a change would make
   * the next resume name a position in a stream that no longer exists.
   */
  #noteEpoch(epoch) {
    if (epoch === null || epoch === undefined || epoch === this.epoch) return;
    this.epoch = epoch;
    this.lastSeq = 0;
  }

  /** `this.epoch`, or null when it is absent or not shaped like a uuid4. */
  #parsedEpoch() {
    if (this.epoch === null) return null;
    // Pessimistic by rule: what cannot be read is treated as not held, so the resume resolves
    // to the honest branch and repaints.
    return EPOCH_RE.test(this.epoch) ? this.epoch : null;
  }

  #resize(now) {
    const epoch = this.#parsedEpoch();
    if (this.size === null || epoch === null || this.phase !== PHASE_LIVE) return [];
    return [send(this.#encode(resizeMessage(epoch, this.size[0], this.size[1]), now))];
  }

  /**
   * One frame this client originates.
   *
   * `seq` is UNORDERED_SEQ: a subscriber allocates no ordinals and must not appear to hold a
   * position in the publisher's sequence space.
   */
  #encode(message, now) {
    return controlFrame(this.sessionId, UNORDERED_SEQ, now, message);
  }

  #noPublisherText() {
    if (this.host === null) {
      return (
        `${NO_PUBLISHER_MESSAGE}: this session is bound, but nothing is streaming to it. The ` +
        "sandbox that owns it is probably stopped. Reload the host list to see which one."
      );
    }
    return (
      `${NO_PUBLISHER_MESSAGE}: host ${this.host.hostId} (${this.host.sandboxLabel}) is ` +
      `${this.host.status}, and nothing is streaming to this session. Start that sandbox, ` +
      "then reload."
    );
  }

  #dropped(why) {
    this.decodeFailures += 1;
    console.warn(`shellbox: dropped an inbound message for ${this.sessionId}: ${why}`);
    return [];
  }

  #redial() {
    this.phase = PHASE_DIALING;
    this.openedAt = null;
    this.helloAt = null;
    return { type: "redial", delay: this.nextDelay() };
  }

  #stop(code, text) {
    this.phase = PHASE_STOPPED;
    return { type: "stop", code, text };
  }
}
