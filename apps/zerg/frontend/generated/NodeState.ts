import AnonymousSchema_41 from './AnonymousSchema_41';
class NodeState {
  private _reservedType: 'node_state' = 'node_state';
  private _executionId: number;
  private _nodeId: string;
  private _reservedStatus: AnonymousSchema_41;
  private _output?: Map<string, any>;
  private _error?: string;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    executionId: number,
    nodeId: string,
    reservedStatus: AnonymousSchema_41,
    output?: Map<string, any>,
    error?: string,
    additionalProperties?: Map<string, any>,
  }) {
    this._executionId = input.executionId;
    this._nodeId = input.nodeId;
    this._reservedStatus = input.reservedStatus;
    this._output = input.output;
    this._error = input.error;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'node_state' { return this._reservedType; }

  get executionId(): number { return this._executionId; }
  set executionId(executionId: number) { this._executionId = executionId; }

  get nodeId(): string { return this._nodeId; }
  set nodeId(nodeId: string) { this._nodeId = nodeId; }

  get reservedStatus(): AnonymousSchema_41 { return this._reservedStatus; }
  set reservedStatus(reservedStatus: AnonymousSchema_41) { this._reservedStatus = reservedStatus; }

  get output(): Map<string, any> | undefined { return this._output; }
  set output(output: Map<string, any> | undefined) { this._output = output; }

  get error(): string | undefined { return this._error; }
  set error(error: string | undefined) { this._error = error; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default NodeState;
