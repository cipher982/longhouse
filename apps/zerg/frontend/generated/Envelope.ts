
class Envelope {
  private _v: number;
  private _reservedType: string;
  private _topic: string;
  private _ts: number;
  private _reqId?: string;
  private _data: Map<string, any>;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    reservedType: string,
    topic: string,
    ts: number,
    reqId?: string,
    data: Map<string, any>,
    additionalProperties?: Map<string, any>,
  }) {
    this._reservedType = input.reservedType;
    this._topic = input.topic;
    this._ts = input.ts;
    this._reqId = input.reqId;
    this._data = input.data;
    this._additionalProperties = input.additionalProperties;
  }

  get v(): number { return this._v; }
  set v(v: number) { this._v = v; }

  get reservedType(): string { return this._reservedType; }
  set reservedType(reservedType: string) { this._reservedType = reservedType; }

  get topic(): string { return this._topic; }
  set topic(topic: string) { this._topic = topic; }

  get ts(): number { return this._ts; }
  set ts(ts: number) { this._ts = ts; }

  get reqId(): string | undefined { return this._reqId; }
  set reqId(reqId: string | undefined) { this._reqId = reqId; }

  get data(): Map<string, any> { return this._data; }
  set data(data: Map<string, any>) { this._data = data; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default Envelope;
