// Shared usage types — client-safe (no node imports), so both the server module
// ($lib/server/usage.ts) and the Usage.svelte component can import them. Shape
// matches qflix-top-pub.sh --json (schema_version 1).

/** One stack component (app/role bucket). */
export interface UsageComponent {
	label: string;
	role: string;
	cpu_cores: number;
	cpu_pct_of_stack: number;
	ram_gib: number;
	ram_pct_of_stack: number;
}

/** Whole-stack totals — our footprint, never other tenants'. */
export interface UsageStack {
	cpu_cores: number;
	cpu_pct_of_box: number;
	ram_gib: number;
	ram_gib_total: number;
	ram_pct_of_box: number;
}

export interface Usage {
	schema_version: number;
	captured_at: string;
	host: string;
	ncpu: number;
	interval_s: number;
	stack: UsageStack;
	components: UsageComponent[];
}
