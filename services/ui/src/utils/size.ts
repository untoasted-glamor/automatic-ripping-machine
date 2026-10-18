// Decimal gigabytes, one decimal place: how disc and backup sizes are quoted
// (a 46.5 GB Blu-ray backup).
export function formatSizeGb(bytes: number): string {
  return `${(bytes / 1_000_000_000).toFixed(1)} GB`
}
