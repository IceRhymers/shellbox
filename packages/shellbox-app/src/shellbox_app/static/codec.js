// The wire form, in the browser. A transcription of
// `packages/shellbox-transport/src/shellbox_transport/codec.py`, which is the specification.
//
// WARNING: this is a SECOND implementation of a format the Python half already implements, and
// nothing executes both. That is ADR-23's stated cost. `tests/unit/test_codec_parity.py`
// compares the constants below against the Python module's, so a magic, a header size, a field
// name or a message kind cannot move on one side alone. It cannot compare the parsing itself.
//
// Read the Python module's docstring before changing anything here. The two shapes below are
// deliberately different, and both differences are load-bearing:
//
//   * A FRAME is a fixed binary header followed by its payload, untouched. Not JSON: a JSON
//     frame would have to base64 the payload (a third of the bandwidth on the hot path) or
//     text-decode it, which cannot work -- a multi-byte UTF-8 character split across two frames
//     is invalid in both halves, and a pty splits wherever the read boundary falls. So the
//     payload is never decoded, and `decodeFrame` reaches it by slicing.
//   * A CONTROL MESSAGE is a single-line JSON header, a newline, then optional raw bytes. The
//     delimiter is unambiguous only because the header is ASCII with no raw newline in it.

export class CodecError extends Error {}

// "SBX1". The magic carries the version: anything else is a frame from a future release or a
// byte offset we did not expect, and both must fail rather than be parsed on a best-effort
// basis. A misparsed header yields a plausible frame with a wrong `seq`, which is worse than no
// frame at all.
export const MAGIC = "SBX1";

// magic, stream, seq, t, session id length, payload length -- `>4sBQdHI`, big-endian and
// unaligned, so the size does not depend on any platform's struct padding.
export const HEADER_SIZE = 27;

export const STREAM_STDOUT = 1;
export const STREAM_STDERR = 2;
export const STREAM_CONTROL = 3;

// The `seq` a sender that allocates NO ordinals puts on a frame. A subscriber holds no attach
// and no allocator, so it must not appear to hold a position in the publisher's sequence space
// -- a client that read its own echoed frame as a data ordinal would infer a gap and repaint
// for it. 0 is safe because `FIRST_SEQ` is 1.
export const UNORDERED_SEQ = 0;

export const CONTROL_HELLO = "hello";
export const CONTROL_RESYNC = "resync";
export const CONTROL_RESIZE = "resize";
export const CONTROL_ERROR = "error";
export const CONTROL_INPUT = "input";
export const CONTROL_RESUME = "resume";
export const CONTROL_CLOSED = "closed";

// Why the pane's stream ended, as a closed set of two. Collapsing them is a session-destroying
// bug: `terminal_gone` means the process exited, `detached` means only that attach client went
// away and the session is still running.
export const CLOSED_TERMINAL_GONE = "terminal_gone";
export const CLOSED_DETACHED = "detached";

export const FIELD_SESSION_ID = "session_id";
export const FIELD_VIEWER_EMAIL = "viewer_email";
export const FIELD_ASKED_SEQ = "asked_seq";
export const FIELD_BASE_SEQ = "base_seq";
export const FIELD_REASON = "reason";
export const FIELD_COLS = "cols";
export const FIELD_ROWS = "rows";
export const FIELD_CODE = "code";
export const FIELD_MESSAGE = "message";

const KIND = "kind";
const EPOCH = "epoch";
const ASCII = new TextEncoder();

/**
 * Decode one frame, or throw `CodecError`.
 *
 * The declared lengths must account for the buffer EXACTLY. A trailing byte means the sender
 * and this reader disagree about the format; a short buffer means truncation. Accepting either
 * delivers a payload that is not the payload that was sent, which is the one thing this codec
 * exists to prevent.
 */
export function decodeFrame(buffer) {
  const raw = new Uint8Array(buffer);
  if (raw.length < HEADER_SIZE) {
    throw new CodecError(`frame is ${raw.length} bytes; the header alone needs ${HEADER_SIZE}`);
  }
  const view = new DataView(raw.buffer, raw.byteOffset, raw.byteLength);
  const magic = String.fromCharCode(raw[0], raw[1], raw[2], raw[3]);
  if (magic !== MAGIC) {
    throw new CodecError(`magic ${JSON.stringify(magic)} is not ${MAGIC}`);
  }
  const stream = view.getUint8(4);
  if (stream !== STREAM_STDOUT && stream !== STREAM_STDERR && stream !== STREAM_CONTROL) {
    // Reached by a zeroed or misaligned header, which is why the stream values start at 1.
    throw new CodecError(`stream ${stream} is not a known stream`);
  }
  // `seq` is a uint64 on the wire and arrives as a BigInt. It is narrowed to a Number here
  // because every consumer compares it, and no session reaches 2^53 frames -- at the ring's
  // 512-frame depth that is more output than a pane can emit in a human lifetime.
  const seq = Number(view.getBigUint64(5));
  const t = view.getFloat64(13);
  const idLength = view.getUint16(21);
  const dataLength = view.getUint32(23);
  const expected = HEADER_SIZE + idLength + dataLength;
  if (raw.length !== expected) {
    throw new CodecError(`frame declares ${expected} bytes and carries ${raw.length}`);
  }
  let sessionId;
  try {
    sessionId = new TextDecoder("utf-8", { fatal: true }).decode(
      raw.subarray(HEADER_SIZE, HEADER_SIZE + idLength),
    );
  } catch {
    throw new CodecError("session_id is not valid UTF-8");
  }
  return { sessionId, seq, t, stream, data: raw.subarray(HEADER_SIZE + idLength) };
}

/** Encode one frame. The payload is copied verbatim and never inspected. */
export function encodeFrame(frame) {
  const sessionId = ASCII.encode(frame.sessionId);
  if (sessionId.length > 0xffff) {
    throw new CodecError(`session_id is ${sessionId.length} bytes; the header allows 65535`);
  }
  const out = new Uint8Array(HEADER_SIZE + sessionId.length + frame.data.length);
  const view = new DataView(out.buffer);
  out.set(ASCII.encode(MAGIC), 0);
  view.setUint8(4, frame.stream);
  view.setBigUint64(5, BigInt(frame.seq));
  view.setFloat64(13, frame.t);
  view.setUint16(21, sessionId.length);
  view.setUint32(23, frame.data.length);
  out.set(sessionId, HEADER_SIZE);
  out.set(frame.data, HEADER_SIZE + sessionId.length);
  return out;
}

/**
 * Decode a control frame's payload, or throw `CodecError`.
 *
 * Splits on the FIRST newline only, so a repaint full of newlines -- which every repaint is --
 * cannot be mistaken for header structure.
 */
export function decodeControl(raw) {
  const newline = raw.indexOf(0x0a);
  if (newline <= 0) {
    throw new CodecError("control payload has no JSON header");
  }
  let record;
  try {
    record = JSON.parse(new TextDecoder().decode(raw.subarray(0, newline)));
  } catch {
    throw new CodecError("control header is not valid JSON");
  }
  if (record === null || typeof record !== "object" || Array.isArray(record)) {
    throw new CodecError("control header is not an object");
  }
  // Membership, NOT a truthiness check, because an ABSENT epoch and an explicit `null` must not
  // arrive here as the same value. A sender that omitted the field never decided about the
  // epoch; a sender that wrote `null` decided it holds no attach. Only the first is malformed.
  if (!Object.hasOwn(record, EPOCH)) {
    throw new CodecError("control header has no 'epoch'; null is how a sender says it has none");
  }
  const kind = record[KIND];
  const epoch = record[EPOCH];
  if (typeof kind !== "string") {
    throw new CodecError("control header needs a string 'kind'");
  }
  if (epoch !== null && typeof epoch !== "string") {
    throw new CodecError("control header 'epoch' is not a string or null");
  }
  const fields = { ...record };
  delete fields[KIND];
  delete fields[EPOCH];
  return { kind, epoch, fields, payload: raw.subarray(newline + 1) };
}

/**
 * Encode a control message to a frame payload.
 *
 * NORMATIVE: compact separators, ASCII, SORTED KEYS, and no indentation. The newline is the only
 * delimiter, so a header that could contain one would make the payload boundary depend on the
 * message's contents. The sort and the ASCII escaping both exist to match
 * `json.dumps(..., separators=(",", ":"), sort_keys=True).encode("ascii")` byte for byte.
 */
export function encodeControl(message) {
  const record = { [KIND]: message.kind, [EPOCH]: message.epoch ?? null, ...message.fields };
  const header = ASCII.encode(asciiJson(record));
  const payload = message.payload ?? new Uint8Array(0);
  const out = new Uint8Array(header.length + 1 + payload.length);
  out.set(header, 0);
  out[header.length] = 0x0a;
  out.set(payload, header.length + 1);
  return out;
}

/** `JSON.stringify` with sorted keys and every non-ASCII character escaped as `\uXXXX`. */
function asciiJson(record) {
  const sorted = Object.keys(record).sort();
  const body = sorted
    .map((key) => `${escapeAscii(JSON.stringify(key))}:${escapeAscii(JSON.stringify(record[key]))}`)
    .join(",");
  return `{${body}}`;
}

function escapeAscii(text) {
  // Indexed over UTF-16 CODE UNITS, not code points, and that is deliberate. Python's
  // `ensure_ascii` emits an astral character as a surrogate PAIR, and a `for...of` loop here
  // would yield one code point and emit a single five-digit escape -- a different byte string
  // for the same input. Nothing in this protocol carries astral text today; the loop is written
  // to match anyway, because the day something does is not the day to discover that the two
  // encoders disagree.
  let out = "";
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index);
    out += code < 0x80 ? text[index] : "\\u" + code.toString(16).padStart(4, "0");
  }
  return out;
}

/** One `control`-stream frame carrying `message`, ready to send. */
export function controlFrame(sessionId, seq, t, message) {
  return encodeFrame({ sessionId, seq, t, stream: STREAM_CONTROL, data: encodeControl(message) });
}

// The message constructors a subscriber needs. The server's own messages are only ever decoded
// here, so `hello`, `resync`, `error` and `closed` have no constructor in this file.

/** Keystrokes travelling to the pane, byte-exact: no encoding, no key names, no allowlist. */
export function inputMessage(data) {
  // The epoch is null: a subscriber holds no attach.
  return { kind: CONTROL_INPUT, epoch: null, fields: {}, payload: data };
}

/** A viewer resize, in-band so it stays ordered against the output it reflows. */
export function resizeMessage(epoch, cols, rows) {
  if (cols < 1 || rows < 1) {
    throw new CodecError(`cols=${cols} rows=${rows}: both must be at least 1`);
  }
  return { kind: CONTROL_RESIZE, epoch, fields: { [FIELD_COLS]: cols, [FIELD_ROWS]: rows } };
}

/**
 * Asking to pick the stream back up at `fromSeq`, under `epoch`.
 *
 * `fromSeq = 0` with a null epoch means "I hold nothing", which `plan_resume` resolves to the
 * honest branch -- the right answer for a viewer opening a tab, and the question that makes
 * silence afterwards evidence about the publisher rather than about an idle pane.
 */
export function resumeMessage(fromSeq, epoch) {
  return { kind: CONTROL_RESUME, epoch: epoch ?? null, fields: { [FIELD_ASKED_SEQ]: fromSeq } };
}
