import AnonymousSchema_25 from './AnonymousSchema_25';
class StreamChunk {
  private _reservedType: 'stream_chunk' = 'stream_chunk';
  private _threadId: number;
  private _content: string;
  private _chunkType: AnonymousSchema_25;
  private _toolName?: string;
  private _toolCallId?: string;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    threadId: number,
    content: string,
    chunkType: AnonymousSchema_25,
    toolName?: string,
    toolCallId?: string,
    additionalProperties?: Map<string, any>,
  }) {
    this._threadId = input.threadId;
    this._content = input.content;
    this._chunkType = input.chunkType;
    this._toolName = input.toolName;
    this._toolCallId = input.toolCallId;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'stream_chunk' { return this._reservedType; }

  get threadId(): number { return this._threadId; }
  set threadId(threadId: number) { this._threadId = threadId; }

  get content(): string { return this._content; }
  set content(content: string) { this._content = content; }

  get chunkType(): AnonymousSchema_25 { return this._chunkType; }
  set chunkType(chunkType: AnonymousSchema_25) { this._chunkType = chunkType; }

  get toolName(): string | undefined { return this._toolName; }
  set toolName(toolName: string | undefined) { this._toolName = toolName; }

  get toolCallId(): string | undefined { return this._toolCallId; }
  set toolCallId(toolCallId: string | undefined) { this._toolCallId = toolCallId; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default StreamChunk;
