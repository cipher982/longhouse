/**
 * The commands that connect a machine's Machine Agent to this Runtime Host.
 *
 * Both carry this host's own address, so the installer stores it and nothing
 * on the machine (the installer, `longhouse auth`, Longhouse.app) has to ask.
 */

const INSTALLER = "curl -fsSL https://get.longhouse.ai/install.sh";

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function thisOrigin(): string {
  return window.location.origin;
}

/**
 * The primary path: install, then approve this machine in the browser.
 * Without a device token the installer runs `longhouse auth`'s browser flow.
 */
export function connectMachineCommand(origin: string = thisOrigin()): string {
  return `${INSTALLER} | LONGHOUSE_URL=${shellQuote(origin)} bash`;
}

/** A machine with no browser: install and connect headlessly as `deviceId`. */
export function connectServerCommand(deviceId: string, token: string, origin: string = thisOrigin()): string {
  return (
    `${INSTALLER} | ` +
    `LONGHOUSE_URL=${shellQuote(origin)} LONGHOUSE_DEVICE_TOKEN=${shellQuote(token)} ` +
    `LONGHOUSE_MACHINE_NAME=${shellQuote(deviceId)} bash`
  );
}
