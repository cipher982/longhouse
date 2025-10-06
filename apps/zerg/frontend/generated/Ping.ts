
class Ping {
  private _reservedType: 'ping' = 'ping';
  private _pingId: string;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    pingId: string,
    additionalProperties?: Map<string, any>,
  }) {
    this._pingId = input.pingId;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'ping' { return this._reservedType; }

  get pingId(): string { return this._pingId; }
  set pingId(pingId: string) { this._pingId = pingId; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default Ping;
