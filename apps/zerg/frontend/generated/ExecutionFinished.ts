import AnonymousSchema_46 from './AnonymousSchema_46';
class ExecutionFinished {
  private _reservedType: 'execution_finished' = 'execution_finished';
  private _executionId: number;
  private _reservedStatus: AnonymousSchema_46;
  private _error?: string;
  private _durationMs?: number;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    executionId: number,
    reservedStatus: AnonymousSchema_46,
    error?: string,
    durationMs?: number,
    additionalProperties?: Map<string, any>,
  }) {
    this._executionId = input.executionId;
    this._reservedStatus = input.reservedStatus;
    this._error = input.error;
    this._durationMs = input.durationMs;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'execution_finished' { return this._reservedType; }

  get executionId(): number { return this._executionId; }
  set executionId(executionId: number) { this._executionId = executionId; }

  get reservedStatus(): AnonymousSchema_46 { return this._reservedStatus; }
  set reservedStatus(reservedStatus: AnonymousSchema_46) { this._reservedStatus = reservedStatus; }

  get error(): string | undefined { return this._error; }
  set error(error: string | undefined) { this._error = error; }

  get durationMs(): number | undefined { return this._durationMs; }
  set durationMs(durationMs: number | undefined) { this._durationMs = durationMs; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default ExecutionFinished;
