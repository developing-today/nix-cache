import { bindings, defineConfig, triggers } from "cf/config";

export default defineConfig({
	worker: {
		name: "nix-cache",
		compatibilityDate: "2026-10-01",
		workersDev: true,
		entrypoint: "src/index.ts",
		triggers: [
			triggers.scheduled({
				schedule: "17 3 * * *",
			}),
			triggers.queue({
				maxBatchSize: 20,
				maxRetries: 5,
				name: "nix-cache-indexing",
			}),
		],
		env: {
			DB: bindings.d1({
				name: "nix-cache-db",
				id: "5de66bbe-1dd8-4f19-95ac-3e95807fe513",
			}),
			INDEX: bindings.kv({
				id: "c30a4b3978a449a1928f381511fc324c",
			}),
			NARS: bindings.r2({
				name: "nix-cache-nars",
			}),
			INDEX_QUEUE: bindings.queue({
				name: "nix-cache-indexing",
			}),
		},
	},
});
