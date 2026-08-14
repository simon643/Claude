import {
	ArrowDownIcon,
	ArrowUpIcon,
	ChevronDownIcon,
	ChevronUpIcon,
	type LucideIcon,
	MinusIcon,
	TrendingDownIcon,
	TrendingUpIcon,
} from "lucide-react";
import type * as React from "react";

/**
 * Local stand-in for efferd's multi-library `IconPlaceholder`. The upstream
 * `delta` component references `<IconPlaceholder lucide="..." tabler="..." />`
 * but the registry manifest never ships the component itself (it resolves from
 * `@efferd/app-shell-1`, which is on efferd.com and unreachable here).
 *
 * This version resolves the `lucide` prop against the installed `lucide-react`
 * package and ignores the other icon-library props. Swap it for the upstream
 * component once efferd.com is reachable.
 */

const LUCIDE_ICONS: Record<string, LucideIcon> = {
	MinusIcon,
	TrendingUpIcon,
	TrendingDownIcon,
	ArrowUpIcon,
	ArrowDownIcon,
	ChevronUpIcon,
	ChevronDownIcon,
};

type IconPlaceholderProps = React.ComponentProps<"svg"> & {
	lucide?: string;
	hugeicons?: string;
	phosphor?: string;
	remixicon?: string;
	tabler?: string;
};

export function IconPlaceholder({
	lucide,
	// The remaining icon-library hints are accepted for API parity and ignored.
	hugeicons: _hugeicons,
	phosphor: _phosphor,
	remixicon: _remixicon,
	tabler: _tabler,
	...props
}: IconPlaceholderProps) {
	const Icon = (lucide && LUCIDE_ICONS[lucide]) || MinusIcon;
	return <Icon {...props} />;
}
