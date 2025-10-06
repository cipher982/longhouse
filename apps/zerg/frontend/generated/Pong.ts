
class Pong {
  private _reservedType: 'pong' = 'pong';
  private _pingId: string;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    pingId: string,
    additionalProperties?: Map<string, any>,
  }) {
    this._pingId = input.pingId;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'pong' { return this._reservedType; }

  get pingId(): string { return this._pingId; }
  set pingId(pingId: string) { this._pingId = pingId; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default Pong;
