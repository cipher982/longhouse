
class SendMessage {
  private _reservedType: 'send_message' = 'send_message';
  private _threadId: number;
  private _content: string;
  private _metadata?: Map<string, any>;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    threadId: number,
    content: string,
    metadata?: Map<string, any>,
    additionalProperties?: Map<string, any>,
  }) {
    this._threadId = input.threadId;
    this._content = input.content;
    this._metadata = input.metadata;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'send_message' { return this._reservedType; }

  get threadId(): number { return this._threadId; }
  set threadId(threadId: number) { this._threadId = threadId; }

  get content(): string { return this._content; }
  set content(content: string) { this._content = content; }

  get metadata(): Map<string, any> | undefined { return this._metadata; }
  set metadata(metadata: Map<string, any> | undefined) { this._metadata = metadata; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default SendMessage;
