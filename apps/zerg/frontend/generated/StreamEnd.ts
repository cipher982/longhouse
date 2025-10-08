
class StreamEnd {
  private _reservedType: 'stream_end' = 'stream_end';
  private _threadId: number;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    threadId: number,
    additionalProperties?: Map<string, any>,
  }) {
    this._threadId = input.threadId;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'stream_end' { return this._reservedType; }

  get threadId(): number { return this._threadId; }
  set threadId(threadId: number) { this._threadId = threadId; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default StreamEnd;
