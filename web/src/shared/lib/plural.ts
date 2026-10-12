/** A count with its noun: "1 result", "2 results", "0 results". */
export function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}
