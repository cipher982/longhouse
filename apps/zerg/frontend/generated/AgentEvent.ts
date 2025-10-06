
class AgentEvent {
  private _reservedType: 'agent_event' = 'agent_event';
  private _data: Map<string, any>;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    data: Map<string, any>,
    additionalProperties?: Map<string, any>,
  }) {
    this._data = input.data;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'agent_event' { return this._reservedType; }

  get data(): Map<string, any> { return this._data; }
  set data(data: Map<string, any>) { this._data = data; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default AgentEvent;
