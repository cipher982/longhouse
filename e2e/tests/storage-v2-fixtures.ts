import { createHash, randomUUID } from "crypto";
import type { APIRequestContext } from "@playwright/test";

export type StorageV2Event = {
  role: string;
  content_text?: string | null;
  tool_name?: string | null;
  tool_input_json?: Record<string, unknown> | null;
  tool_output_text?: string | null;
  tool_call_id?: string | null;
  timestamp: string;
  source_offset?: number;
};

type SessionOptions = {
  sessionId: string;
  provider: string;
  project: string;
  environment: string;
  cwd: string;
  providerSessionId?: string;
  startedAt: string;
  endedAt: string | null;
  events: StorageV2Event[];
  agentsToken?: string;
};

function lengthPrefixed(value: Buffer, width: number): Buffer {
  const prefix = Buffer.alloc(width);
  prefix.writeUIntBE(value.length, 0, width);
  return Buffer.concat([prefix, value]);
}

function uuidBytes(value: string): Buffer {
  return Buffer.from(value.replaceAll("-", ""), "hex");
}

function envelopeId(options: {
  tenantId: string;
  machineId: string;
  provider: string;
  opaqueSourceId: string;
  sourceEpoch: string;
  rangeStart: number;
  rangeEnd: number;
  records: Buffer[];
}): string {
  const range = Buffer.alloc(21);
  range.writeUInt8(2, 0); // record_ordinal
  range.writeBigUInt64BE(BigInt(options.rangeStart), 1);
  range.writeBigUInt64BE(BigInt(options.rangeEnd), 9);
  range.writeUInt32BE(options.records.length, 17);
  const preimage = Buffer.concat([
    Buffer.from("longhouse-envelope-v2\0"),
    lengthPrefixed(Buffer.from(options.tenantId), 4),
    lengthPrefixed(Buffer.from(options.machineId), 4),
    lengthPrefixed(Buffer.from(options.provider, "ascii"), 4),
    lengthPrefixed(Buffer.from(options.opaqueSourceId), 4),
    uuidBytes(options.sourceEpoch),
    range,
    ...options.records.map((record) =>
      createHash("sha256").update(record).digest(),
    ),
  ]);
  return createHash("sha256").update(preimage).digest("hex");
}

type SourceState = {
  sourceEpoch: string;
  generationId: string;
  epochOpenedAt: string;
  nextPosition: number;
};

const sourceStates = new Map<string, SourceState>();

export async function ingestStorageV2Session(
  request: APIRequestContext,
  options: SessionOptions,
): Promise<void> {
  const headers = options.agentsToken
    ? { "X-Agents-Token": options.agentsToken }
    : undefined;
  const capabilities = await request.get(
    "/api/agents/storage/v2/capabilities",
    {
      headers,
    },
  );
  if (!capabilities.ok()) {
    throw new Error(
      `storage-v2 capabilities failed: ${capabilities.status()} ${await capabilities.text()}`,
    );
  }
  const authority = await capabilities.json();
  const tenantId = String(authority.tenant_id);
  const machineId = String(authority.machine_id);
  const opaqueSourceId = `${options.provider}-${options.sessionId}.jsonl`;
  const sourceKey = `${tenantId}\0${machineId}\0${options.provider}\0${opaqueSourceId}`;
  const sourceState = sourceStates.get(sourceKey) ?? {
    sourceEpoch: randomUUID(),
    generationId: randomUUID(),
    epochOpenedAt: options.startedAt,
    nextPosition: 0,
  };
  sourceStates.set(sourceKey, sourceState);
  const rangeStart = sourceState.nextPosition;
  const rangeEnd = rangeStart + options.events.length;
  const records = options.events.map((event) =>
    Buffer.from(`${JSON.stringify(event)}\n`, "utf8"),
  );
  const expectedEnvelopeId = envelopeId({
    tenantId,
    machineId,
    provider: options.provider,
    opaqueSourceId,
    sourceEpoch: sourceState.sourceEpoch,
    rangeStart,
    rangeEnd,
    records,
  });
  const response = await request.post("/api/agents/storage/v2/envelopes", {
    headers: {
      ...(headers ?? {}),
      "X-Longhouse-Storage-Lane": "live",
    },
    data: {
      protocol_version: 2,
      tenant_id: tenantId,
      machine_id: machineId,
      session_id: options.sessionId,
      provider: options.provider,
      opaque_source_id: opaqueSourceId,
      source_epoch: sourceState.sourceEpoch,
      predecessor_source_epoch: null,
      epoch_opened_at: sourceState.epochOpenedAt,
      range_kind: "record_ordinal",
      range_start: rangeStart,
      range_end: rangeEnd,
      render: {
        generation_id: sourceState.generationId,
        parser_revision: "e2e-storage-v2",
        ordering_revision: "semantic-order-v2",
        records: options.events.map((event, index) => ({
          event_id: `${options.sessionId}:${index}`,
          order_time_us: Date.parse(event.timestamp) * 1000 + index,
          source_position: rangeStart + index,
          event_subordinal: 0,
          role: event.role,
          content_text: event.content_text ?? null,
          tool_name: event.tool_name ?? null,
          tool_input_json: event.tool_input_json ?? null,
          tool_output_text: event.tool_output_text ?? null,
          tool_call_id: event.tool_call_id ?? null,
          thread_id: null,
          branch_kind: null,
          raw_record_ordinal: index,
          interaction_kind: null,
        })),
      },
      media: [],
      session: {
        environment: options.environment,
        project: options.project,
        cwd: options.cwd,
        git_repo: null,
        git_branch: null,
        started_at: options.startedAt,
        last_activity_at: options.events.at(-1)?.timestamp ?? options.startedAt,
        ended_at: options.endedAt,
        origin_kind: "shadow",
        hidden_from_default_timeline: false,
        launch_actor: null,
        launch_surface: null,
        provider_session_id: options.providerSessionId ?? null,
      },
      records: records.map((record, index) => ({
        source_position: rangeStart + index,
        data_b64: record.toString("base64"),
      })),
      expected_envelope_id: expectedEnvelopeId,
    },
  });
  if (!response.ok()) {
    throw new Error(
      `storage-v2 ingest failed: ${response.status()} ${await response.text()}`,
    );
  }
  sourceState.nextPosition = rangeEnd;
}
