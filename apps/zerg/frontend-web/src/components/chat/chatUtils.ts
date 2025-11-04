export function formatTimestamp(sentAt?: string | null): string {
  if (!sentAt) return "";
  try {
    const date = new Date(sentAt);
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return "";
  }
}

export function truncateText(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength) + "...";
}
