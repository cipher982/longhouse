
class ThreadHistory {
  private _reservedType: 'thread_history' = 'thread_history';
  private _threadId: number;
  private _messages: Map<string, any>[];
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    threadId: number,
    messages: Map<string, any>[],
    additionalProperties?: Map<string, any>,
  }) {
    this._threadId = input.threadId;
    this._messages = input.messages;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'thread_history' { return this._reservedType; }

  get threadId(): number { return this._threadId; }
  set threadId(threadId: number) { this._threadId = threadId; }

  get messages(): Map<string, any>[] { return this._messages; }
  set messages(messages: Map<string, any>[]) { this._messages = messages; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default ThreadHistory;
