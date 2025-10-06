
class Error {
  private _reservedType: 'error' = 'error';
  private _code: number;
  private _message: string;
  private _details?: Map<string, any>;
  private _additionalProperties?: Map<string, any>;

  constructor(input: {
    code: number,
    message: string,
    details?: Map<string, any>,
    additionalProperties?: Map<string, any>,
  }) {
    this._code = input.code;
    this._message = input.message;
    this._details = input.details;
    this._additionalProperties = input.additionalProperties;
  }

  get reservedType(): 'error' { return this._reservedType; }

  get code(): number { return this._code; }
  set code(code: number) { this._code = code; }

  get message(): string { return this._message; }
  set message(message: string) { this._message = message; }

  get details(): Map<string, any> | undefined { return this._details; }
  set details(details: Map<string, any> | undefined) { this._details = details; }

  get additionalProperties(): Map<string, any> | undefined { return this._additionalProperties; }
  set additionalProperties(additionalProperties: Map<string, any> | undefined) { this._additionalProperties = additionalProperties; }
}
export default Error;
