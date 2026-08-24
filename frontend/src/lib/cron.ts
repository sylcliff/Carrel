// Cron-string formatting helpers shared by the SyncStatus / ScheduledJobs
// card and the Settings page. No behaviour change vs the original copy in
// ScheduledJobsCard.tsx — just extracted so both surfaces stay in sync.

const WEEKDAYS = [
  "Sunday", "Monday", "Tuesday", "Wednesday",
  "Thursday", "Friday", "Saturday",
];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// Format a 24h "H:M" pair as 12h with am/pm (e.g. "8:00" → "8:00 AM").
export function fmtHM(h: number, m: number): string {
  if (!Number.isFinite(h) || !Number.isFinite(m)) return "";
  const period = h < 12 ? "AM" : "PM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  const mm = m.toString().padStart(2, "0");
  return `${h12}:${mm} ${period}`;
}

// Turn a 5-field crontab (minute hour dom month dow) into a one-line plain
// English description of the cadence. Covers the common patterns we document;
// anything outside that envelope falls back to "custom schedule". This is a
// helper, not a validator — invalid strings return "" (the input itself shows
// the raw cron).
export function humanizeCron(raw: string): string {
  const parts = raw.trim().split(/\s+/);
  if (parts.length !== 5) return "";
  const [minStr, hourStr, domStr, monStr, dowStr] = parts;
  const isNum = (x: string) => /^\d+$/.test(x);
  const isStar = (x: string) => x === "*";
  const isList = (x: string) => x.includes(",");
  const isRange = (x: string) => x.includes("-") && !x.includes("/");
  const isStep = (x: string) => x.startsWith("*/");

  // Parse a numeric field; returns NaN for non-numeric.
  const num = (x: string) => parseInt(x, 10);

  // */N * * * * — every N minutes
  if (isStep(minStr) && isStar(hourStr) && isStar(domStr) && isStar(monStr) && isStar(dowStr)) {
    const n = num(minStr.slice(2));
    if (Number.isFinite(n) && n > 0) return `every ${n} minutes`;
  }

  // M H * * * — daily at H:MM (and the * */N hour family)
  if (isNum(minStr) && isNum(hourStr) && isStar(domStr) && isStar(monStr) && isStar(dowStr)) {
    const h = num(hourStr);
    const m = num(minStr);
    if (h >= 0 && h <= 23 && m >= 0 && m <= 59) return `daily at ${fmtHM(h, m)}`;
  }

  // M H * * DOW — weekly (single weekday or comma list)
  if (isNum(minStr) && isNum(hourStr) && isStar(domStr) && isStar(monStr) && (isNum(dowStr) || isList(dowStr) || isRange(dowStr))) {
    const h = num(hourStr);
    const m = num(minStr);
    if (h >= 0 && h <= 23 && m >= 0 && m <= 59) {
      const days = dowStr.split(",").flatMap((d) => {
        if (d.includes("-")) {
          const [a, b] = d.split("-").map(num);
          if (Number.isFinite(a) && Number.isFinite(b)) {
            const out: string[] = [];
            for (let i = a; i <= b; i++) out.push(WEEKDAYS[i % 7]);
            return out;
          }
          return [];
        }
        const i = num(d);
        return Number.isFinite(i) ? [WEEKDAYS[i % 7]] : [];
      });
      if (days.length === 1) return `${days[0]}s at ${fmtHM(h, m)}`;
      if (days.length > 1) return `${days.join(", ")} at ${fmtHM(h, m)}`;
    }
  }

  // M H DOM * * — monthly on a day-of-month
  if (isNum(minStr) && isNum(hourStr) && (isNum(domStr) || isList(domStr)) && isStar(monStr) && isStar(dowStr)) {
    const h = num(hourStr);
    const m = num(minStr);
    if (h >= 0 && h <= 23 && m >= 0 && m <= 59) {
      const ord = (n: number) => {
        const s = ["th", "st", "nd", "rd"];
        const v = n % 100;
        return `${n}${s[(v - 20) % 10] || s[v] || s[0]}`;
      };
      const days = domStr.split(",").map(num).filter((n) => Number.isFinite(n) && n >= 1 && n <= 31);
      if (days.length === 1) return `monthly on the ${ord(days[0])} at ${fmtHM(h, m)}`;
      if (days.length > 1) return `monthly on the ${days.map(ord).join(", ")} at ${fmtHM(h, m)}`;
    }
  }

  // M H DOM MON * — on a specific month/day (e.g. birthday-style cron)
  if (isNum(minStr) && isNum(hourStr) && isNum(domStr) && (isNum(monStr) || isList(monStr)) && isStar(dowStr)) {
    const h = num(hourStr);
    const m = num(minStr);
    const d = num(domStr);
    if (h >= 0 && h <= 23 && m >= 0 && m <= 59 && d >= 1 && d <= 31) {
      const months = monStr.split(",").map(num).filter((n) => Number.isFinite(n) && n >= 1 && n <= 12);
      if (months.length) return `${months.map((n) => MONTHS[n - 1]).join(", ")} ${d} at ${fmtHM(h, m)}`;
    }
  }

  // 0 */N * * * — every N hours
  if (isNum(minStr) && hourStr.startsWith("*/") && isStar(domStr) && isStar(monStr) && isStar(dowStr)) {
    const n = num(hourStr.slice(2));
    if (Number.isFinite(n) && n > 0) return `every ${n} hours`;
  }

  return "custom schedule";
}
