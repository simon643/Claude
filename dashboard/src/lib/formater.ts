/**
 * Local stand-in for the `@efferd/formater` registry dependency, which lives on
 * efferd.com and could not be fetched in this environment. It reimplements the
 * `formatDate` surface the dashboard block consumes.
 *
 * If/when efferd.com becomes reachable, run:
 *   npx shadcn@latest add @efferd/formater
 * and replace this file with the upstream version.
 */

export type DateFormatStyle =
	| "day-month"
	| "month-day"
	| "month-year"
	| "short"
	| "long";

function toDate(value: string | number | Date): Date {
	if (value instanceof Date) {
		return value;
	}
	if (typeof value === "number") {
		return new Date(value);
	}
	// Anchor bare ISO dates (YYYY-MM-DD) to midday to avoid timezone slippage.
	if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
		return new Date(`${value}T12:00:00`);
	}
	return new Date(value);
}

const OPTIONS: Record<DateFormatStyle, Intl.DateTimeFormatOptions> = {
	"day-month": { month: "short", day: "numeric" },
	"month-day": { month: "short", day: "numeric" },
	"month-year": { month: "short", year: "numeric" },
	short: { year: "numeric", month: "short", day: "numeric" },
	long: { year: "numeric", month: "long", day: "numeric" },
};

export function formatDate(
	value: string | number | Date,
	style: DateFormatStyle = "short",
	locale?: string,
): string {
	const date = toDate(value);
	if (Number.isNaN(date.getTime())) {
		return String(value);
	}
	return new Intl.DateTimeFormat(locale, OPTIONS[style] ?? OPTIONS.short).format(
		date,
	);
}

export function formatCurrency(
	value: number,
	currency = "USD",
	locale?: string,
): string {
	return new Intl.NumberFormat(locale, {
		style: "currency",
		currency,
		maximumFractionDigits: 2,
	}).format(value);
}
