window.__ModuleLoader__.load({
	id: "freqtrade-fleet-manager",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		//#region \0rolldown/runtime.js
		var __create = Object.create;
		var __defProp = Object.defineProperty;
		var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
		var __getOwnPropNames = Object.getOwnPropertyNames;
		var __getProtoOf = Object.getPrototypeOf;
		var __hasOwnProp = Object.prototype.hasOwnProperty;
		var __copyProps = (to, from, except, desc) => {
			if (from && typeof from === "object" || typeof from === "function") for (var keys = __getOwnPropNames(from), i = 0, n = keys.length, key; i < n; i++) {
				key = keys[i];
				if (!__hasOwnProp.call(to, key) && key !== except) __defProp(to, key, {
					get: ((k) => from[k]).bind(null, key),
					enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable
				});
			}
			return to;
		};
		var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(isNodeMode || !mod || !mod.__esModule || !__hasOwnProp.call(mod, "default") ? __defProp(target, "default", {
			value: mod,
			enumerable: true
		}) : target, mod));
		//#endregion
		let react = require("react");
		react = __toESM(react, 1);
		//#region client/index.js
		/**
		* freqtrade-fleet-manager — browser half: the Settings → "Freqtrade Fleet"
		* section. It fetches `/freqtrade/api/fleet?ping=1` (served by the host entry
		* on the host web server) and renders the instance list with live `/ping`
		* health. The host entry owns the registry and all 54 `ft_*` tools; this half
		* is read-only presentation.
		*
		* @module freqtrade-fleet-manager/client
		*/
		/** Hard dependency: the slots service (settings.section is declared by the settings UI). */
		const inject = ["slots"];
		const CSS = [
			".ftdash { font-size: 13px; line-height: 1.5; }",
			".ftdash-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }",
			".ftdash-head b { font-weight: 600; }",
			".ftdash-btn { cursor: pointer; padding: 4px 10px; border-radius: 6px; border: 1px solid rgba(128,128,128,0.4); background: transparent; font-size: 12px; }",
			".ftdash-table { width: 100%; border-collapse: collapse; margin-top: 4px; }",
			".ftdash-table th, .ftdash-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid rgba(128,128,128,0.25); }",
			".ftdash-empty { padding: 12px 0; opacity: 0.85; }",
			".ftdash-err { padding: 12px 0; color: #c0392b; }",
			".ftdash-foot { margin-top: 10px; opacity: 0.7; font-size: 12px; }"
		].join("\n");
		function FleetDashboard() {
			const [state, setState] = react.default.useState({
				loading: true,
				data: null,
				error: null
			});
			function refresh() {
				setState((prev) => ({
					loading: true,
					data: prev.data,
					error: null
				}));
				fetch("/freqtrade/api/fleet?ping=1").then((r) => {
					if (!r.ok) throw new Error("HTTP " + r.status);
					return r.json();
				}).then((data) => setState({
					loading: false,
					data,
					error: null
				})).catch((e) => setState({
					loading: false,
					data: null,
					error: String(e && e.message || e)
				}));
			}
			react.default.useEffect(() => {
				refresh();
			}, []);
			const data = state.data;
			const rows = data && data.instances || [];
			const health = data && data.health || [];
			const hmap = {};
			for (const h of health) hmap[h.name] = h;
			const th = (label) => react.default.createElement("th", null, label);
			const cells = rows.map((r) => {
				const h = hmap[r.name];
				let status = "—";
				if (h) status = h.up ? "up" : "down";
				if (h && h.up && h.latency_ms !== null && h.latency_ms !== void 0) status += " (" + h.latency_ms + "ms)";
				return react.default.createElement("tr", { key: r.name }, react.default.createElement("td", null, r.name), react.default.createElement("td", null, r.host === "ssh" ? "ssh" : "local"), react.default.createElement("td", null, r.strategy || ""), react.default.createElement("td", null, r.dry_run === false ? "live" : "dry"), react.default.createElement("td", null, status));
			});
			return react.default.createElement("div", { className: "ftdash" }, react.default.createElement("div", { className: "ftdash-head" }, react.default.createElement("b", null, "Freqtrade Fleet"), react.default.createElement("button", {
				className: "ftdash-btn",
				onClick: refresh
			}, state.loading ? "Refreshing…" : "Refresh")), state.error ? react.default.createElement("div", { className: "ftdash-err" }, state.error) : null, state.loading && !data ? react.default.createElement("div", null, "Loading fleet…") : null, data && data.count === 0 ? react.default.createElement("div", { className: "ftdash-empty" }, "No instances registered. Ask the agent to run ft_instances_add.") : null, rows.length ? react.default.createElement("table", { className: "ftdash-table" }, react.default.createElement("thead", null, react.default.createElement("tr", null, th("Instance"), th("Host"), th("Strategy"), th("Mode"), th("Status"))), react.default.createElement("tbody", null, cells)) : null, data ? react.default.createElement("div", { className: "ftdash-foot" }, data.count + " instance(s) · registry: " + (data.registryFile || "in-memory")) : null);
		}
		/**
		* Client plugin body: inject the stylesheet, then register the settings
		* section once the settings UI declares the slot.
		* @param ctx - client root context.
		*/
		function apply(ctx) {
			ctx.effect(() => {
				const tag = document.createElement("style");
				tag.setAttribute("data-plugin-css", "freqtrade-fleet-manager");
				tag.textContent = CSS;
				document.head.append(tag);
				return () => tag.remove();
			}, "freqtrade-fleet-manager: dashboard styles");
			ctx.slots.inject("settings.section", () => ctx.slots.register({
				name: "settings.section",
				id: "freqtrade-fleet",
				order: 50,
				label: "Freqtrade Fleet"
			}, FleetDashboard));
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map