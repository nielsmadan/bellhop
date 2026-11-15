import * as fs from "node:fs";
import * as net from "node:net";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MAX_BYTES = 64 * 1024;

export default function bellhopSpike(pi: ExtensionAPI) {
	const socketPath = process.env.BELLHOP_SPIKE_SOCKET;
	if (!socketPath) return;
	let server: net.Server | undefined;
	let compacting = false;

	const accept = (header: string, body: string): string => {
		if (header === "ping") return "ok pong\n";
		if (!body) return "error empty message\n";
		if (compacting) return "busy compacting\n";
		if (header === "deliver boundary") {
			pi.sendUserMessage(body, { deliverAs: "followUp" });
			return "ok followUp\n";
		}
		if (header === "deliver interrupt") {
			pi.sendUserMessage(body, { deliverAs: "steer" });
			return "ok steer\n";
		}
		return `error unknown header ${header}\n`;
	};

	const serve = () => {
		if (server) return;
		fs.rmSync(socketPath, { force: true });
		server = net.createServer({ allowHalfOpen: true }, (conn) => {
			const chunks: Buffer[] = [];
			let size = 0;
			conn.on("data", (chunk: Buffer) => {
				size += chunk.length;
				if (size > MAX_BYTES) conn.destroy();
				else chunks.push(chunk);
			});
			conn.on("end", () => {
				const text = Buffer.concat(chunks).toString("utf8");
				const cut = text.indexOf("\n");
				const header = (cut < 0 ? text : text.slice(0, cut)).trim();
				const body = cut < 0 ? "" : text.slice(cut + 1);
				conn.end(accept(header, body));
			});
			conn.on("error", () => undefined);
		});
		server.listen(socketPath, () => fs.chmodSync(socketPath, 0o600));
	};

	pi.on("session_start", async () => serve());
	pi.on("session_before_compact", async () => {
		compacting = true;
	});
	pi.on("session_compact", async () => {
		compacting = false;
	});
	pi.on("session_shutdown", async () => {
		server?.close();
		server = undefined;
		fs.rmSync(socketPath, { force: true });
	});
}
