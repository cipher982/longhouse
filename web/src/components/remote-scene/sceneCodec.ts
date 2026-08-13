const FRAME_SEPARATOR = "|";
const RUN_SEPARATOR = ",";

export function encodeFrame(frame: Uint8Array): string {
  const runs: string[] = [];
  let start = 0;

  while (start < frame.length) {
    const value = frame[start];
    let end = start + 1;
    while (end < frame.length && frame[end] === value) end += 1;
    runs.push(`${(end - start).toString(36)}${value.toString(36)}`);
    start = end;
  }

  return runs.join(RUN_SEPARATOR);
}

export function encodeFrames(frames: Uint8Array[]): string {
  return frames.map(encodeFrame).join(FRAME_SEPARATOR);
}

export function decodeFrame(encoded: string, expectedLength: number): Uint8Array {
  const frame = new Uint8Array(expectedLength);
  let cursor = 0;

  for (const run of encoded.split(RUN_SEPARATOR)) {
    const value = Number.parseInt(run.slice(-1), 36);
    const length = Number.parseInt(run.slice(0, -1), 36);
    if (!Number.isInteger(length) || length < 1 || !Number.isInteger(value) || value > 15) {
      throw new Error(`Invalid scene run: ${run}`);
    }
    frame.fill(value, cursor, cursor + length);
    cursor += length;
  }

  if (cursor !== expectedLength) {
    throw new Error(`Scene frame decoded to ${cursor} cells; expected ${expectedLength}`);
  }
  return frame;
}

export function decodeFrames(encoded: string, expectedLength: number): Uint8Array[] {
  return encoded.split(FRAME_SEPARATOR).map((frame) => decodeFrame(frame, expectedLength));
}
