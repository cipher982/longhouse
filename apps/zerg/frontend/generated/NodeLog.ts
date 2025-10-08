import AnonymousSchema_52 from './AnonymousSchema_52';
class NodeLog {
  private _reservedType: 'node_log' = 'node_log';
  private _executionId: number;
  private _nodeId: string;
  private _stream: AnonymousSchema_52;
  private _reservedText: string;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    executionId: number,
    nodeId: string,
    stream: AnonymousSchema_52,
    reservedText: string,
    additionalProperties?: Map<string, any>,
  }) {
    this._executionId = input.executionId;
    this._nodeId = input.nodeId;
    this._stream = input.stream;
    this._reservedText = input.reservedText;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'node_log' { return this._reservedType; }

  get executionId(): number { return this._executionId; }
  set executionId(executionId: number) { this._executionId = executionId; }

  get nodeId(): string { return this._nodeId; }
  set nodeId(nodeId: string) { this._nodeId = nodeId; }

  get stream(): AnonymousSchema_52 { return this._stream; }
  set stream(stream: AnonymousSchema_52) { this._stream = stream; }

  get reservedText(): string { return this._reservedText; }
  set reservedText(reservedText: string) { this._reservedText = reservedText; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default NodeLog;
