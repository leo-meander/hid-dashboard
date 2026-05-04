/**
 * SyncBadge — renders "· Last synced: DD/MM, HH:mm" (Asia/Ho_Chi_Minh) inline.
 * Used in page headers to surface when underlying daily-synced data was last refreshed.
 *
 * Usage:
 *   <SyncBadge timestamp={data.synced_at} />
 *   <SyncBadge timestamp={ts} prefix="" />          // no leading "· "
 *   <SyncBadge timestamp={ts} className="ml-2" />   // override wrapper classes
 */
export default function SyncBadge({ timestamp, prefix = " · ", className = "" }) {
  if (!timestamp) return null;
  const formatted = new Date(timestamp).toLocaleString("en-GB", {
    timeZone: "Asia/Ho_Chi_Minh",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  return (
    <span className={className}>
      {prefix}Last synced: {formatted}
    </span>
  );
}
