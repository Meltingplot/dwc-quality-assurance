// @vitest-environment node
// Filesystem-only assertions about the repo layout; happy-dom would serve
// import.meta.url as an http: URL, which fileURLToPath cannot resolve.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const read = (p) => fs.readFileSync(path.join(ROOT, p), "utf8");

function flatKeys(obj, prefix = "") {
	return Object.entries(obj).flatMap(([k, v]) =>
		v !== null && typeof v === "object" ? flatKeys(v, `${prefix}${k}.`) : [`${prefix}${k}`]);
}

function sourceFiles(dir) {
	return fs.readdirSync(path.join(ROOT, dir), { withFileTypes: true }).flatMap((e) =>
		e.isDirectory() ? sourceFiles(path.join(dir, e.name)) : (/\.(ts|vue)$/.test(e.name) ? [path.join(dir, e.name)] : []));
}

describe("plugin.json", () => {
	const manifest = JSON.parse(read("plugin.json"));

	it("identifies the plugin", () => {
		expect(manifest.id).toBe("QualityAssurance");
		expect(manifest.sbcRequired).toBe(true);
		expect(manifest.sbcExecutable).toBe("qa-daemon.py");
		expect(manifest.sbcAutoRestart).toBe(true);
		expect(manifest.dwcVersion).toBe("auto-major");
	});

	it("declares every plugin data key the daemon writes", () => {
		// DSF refuses set_plugin_data for undeclared keys
		const daemon = read("dsf/qa-daemon.py");
		const written = [...daemon.matchAll(/PLUGIN_DATA_KEYS\s*=\s*\(([^)]*)\)/g)]
			.flatMap((m) => [...m[1].matchAll(/"(\w+)"/g)].map((k) => k[1]));
		expect(written.length).toBeGreaterThan(0);
		for (const key of written) {
			expect(Object.keys(manifest.data)).toContain(key);
		}
	});
});

describe("layout", () => {
	it("has the builder entry point and the daemon", () => {
		expect(fs.existsSync(path.join(ROOT, "src/index.ts"))).toBe(true);
		expect(fs.existsSync(path.join(ROOT, "dsf/qa-daemon.py"))).toBe(true);
	});
});

describe("i18n", () => {
	const en = JSON.parse(read("src/i18n/en.json"));
	const de = JSON.parse(read("src/i18n/de.json"));

	it("has the same keys in every language", () => {
		expect(flatKeys(de).sort()).toEqual(flatKeys(en).sort());
	});

	it("has every key the sources use", () => {
		const keys = new Set(flatKeys(en));
		const used = sourceFiles("src").flatMap((f) =>
			[...read(f).matchAll(/plugins\.QualityAssurance\.([\w.]+)/g)].map((m) => m[1].replace(/\.$/, "")));
		const missing = used.filter((k) => !keys.has(k) && !flatKeys(en).some((e) => e.startsWith(`${k}.`)));
		expect(missing).toEqual([]);
	});
});
