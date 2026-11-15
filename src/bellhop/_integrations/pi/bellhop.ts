import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const MAX_BYTES = 64 * 1024;
const SOCKET_PATH_LIMIT = 104;
const IDLE_MS = 5000;
const WIRE = 2;
const TRIPLE_QUOTES = /"""|'''/g;

export type Mode = "followUp" | "steer";
export type Deliver = (body: string, mode: Mode) => void;
export type Where = { session: string | null; transcript: string | null };
export type State = Where & { compacting: boolean };

export function configuredRuntimeDir(text: string): string | null | undefined {
	let multiline = false;
	for (const line of text.split("\n")) {
		if ((line.match(TRIPLE_QUOTES) ?? []).length % 2 === 1) {
			multiline = !multiline;
			continue;
		}
		if (multiline) continue;
		if (/^\s*\[/.test(line)) return undefined;
		if (!/^\s*["']?runtime_dir["']?\s*=/.test(line)) continue;
		const found = /^\s*runtime_dir\s*=\s*(?:"([^"\\]*)"|'([^']*)')\s*(?:#.*)?$/.exec(line);
		return found ? (found[1] ?? found[2]) : null;
	}
	return undefined;
}

function fromConfig(env: NodeJS.ProcessEnv): string | null | undefined {
	const home = env.HOME || os.homedir();
	const configHome = env.XDG_CONFIG_HOME || path.join(home, ".config");
	if (!path.isAbsolute(configHome)) return undefined;
	try {
		return configuredRuntimeDir(
			fs.readFileSync(path.join(configHome, "bellhop", "config.toml"), "utf8"),
		);
	} catch {
		return undefined;
	}
}

export function runtimeDir(env: NodeJS.ProcessEnv, uid: number): string | undefined {
	const configured = env.BELLHOP_RUNTIME_DIR ? env.BELLHOP_RUNTIME_DIR : fromConfig(env);
	if (configured === null) return undefined;
	const chosen = configured ?? `/tmp/bellhop-${uid}`;
	return path.isAbsolute(chosen) ? chosen : undefined;
}

export function ensurePrivateDir(dir: string, uid: number): boolean {
	try {
		fs.mkdirSync(dir, { mode: 0o700 });
	} catch {}
	try {
		const info = fs.lstatSync(dir);
		return info.isDirectory() && info.uid === uid && (info.mode & 0o777) === 0o700;
	} catch {
		return false;
	}
}

export type Request = { header: string; body: string; bodyBytes: number };

export function reply(request: Request, state: State, deliver: Deliver): string {
	const { header, body, bodyBytes } = request;
	if (header === "ping") {
		const pong = { wire: WIRE, session: state.session, transcript: state.transcript };
		return `ok pong ${JSON.stringify(pong)}\n`;
	}
	const [verb, when, target, count, ...rest] = header.split(/\s+/);
	if (verb !== "deliver" || (when !== "boundary" && when !== "interrupt") || rest.length) {
		return `error unknown header ${header}\n`;
	}
	if (!target || !count || !/^\d+$/.test(count)) return "error wire_version\n";
	if (Number(count) !== bodyBytes) return "error truncated\n";
	if (state.session !== null && target !== state.session) {
		return `error session_mismatch ${state.session}\n`;
	}
	if (!body) return "error empty message\n";
	if (state.compacting) return "busy compacting\n";
	const mode: Mode = when === "interrupt" ? "steer" : "followUp";
	try {
		deliver(body, mode);
	} catch (error) {
		return `error ${String(error).split("\n")[0]}\n`;
	}
	return `ok ${mode}\n`;
}

export function split(raw: Buffer): Request {
	const cut = raw.indexOf(0x0a);
	if (cut < 0) return { header: raw.toString("utf8").trim(), body: "", bodyBytes: 0 };
	const body = raw.subarray(cut + 1);
	return {
		header: raw.subarray(0, cut).toString("utf8").trim(),
		body: body.toString("utf8"),
		bodyBytes: body.length,
	};
}

export default function bellhop(pi: ExtensionAPI) {
	const uid = process.getuid?.() ?? -1;
	let server: net.Server | undefined;
	let socketPath: string | undefined;
	let inode: number | undefined;
	let compacting = false;
	let context: ExtensionContext | undefined;
	const deliver: Deliver = (body, mode) => pi.sendUserMessage(body, { deliverAs: mode });
	const where = (): Where => {
		try {
			const manager = context?.sessionManager;
			const file = manager?.getSessionFile();
			return {
				session: manager?.getSessionId() ?? null,
				transcript: file && path.isAbsolute(file) ? file : null,
			};
		} catch {
			return { session: null, transcript: null };
		}
	};

	const handle = (conn: net.Socket) => {
		const chunks: Buffer[] = [];
		let size = 0;
		conn.setTimeout(IDLE_MS, () => conn.destroy());
		conn.on("data", (chunk: Buffer) => {
			size += chunk.length;
			if (size > MAX_BYTES) conn.destroy();
			else chunks.push(chunk);
		});
		conn.on("end", () => {
			conn.end(reply(split(Buffer.concat(chunks)), { ...where(), compacting }, deliver));
		});
		conn.on("error", () => undefined);
	};

	const serve = () => {
		if (server) return;
		const dir = runtimeDir(process.env, uid);
		if (!dir) return;
		const target = path.join(dir, `pi-${process.pid}.sock`);
		if (Buffer.byteLength(target) >= SOCKET_PATH_LIMIT || !ensurePrivateDir(dir, uid)) return;
		const temp = path.join(dir, `.pi-${process.pid}.tmp`);
		try {
			fs.rmSync(temp, { force: true });
		} catch {
			return;
		}
		const listening = net.createServer({ allowHalfOpen: true }, handle);
		const abandon = () => {
			listening.close();
			if (server === listening) server = undefined;
			try {
				fs.rmSync(temp, { force: true });
			} catch {}
		};
		listening.on("error", abandon);
		listening.listen(temp, () => {
			try {
				fs.chmodSync(temp, 0o600);
				fs.renameSync(temp, target);
				inode = fs.lstatSync(target).ino;
				socketPath = target;
			} catch {
				abandon();
			}
		});
		server = listening;
	};

	const stop = () => {
		server?.close();
		server = undefined;
		try {
			if (socketPath && fs.lstatSync(socketPath).ino === inode) fs.rmSync(socketPath);
		} catch {}
		socketPath = undefined;
		inode = undefined;
	};

	pi.on("session_start", async (_event, ctx) => {
		context = ctx;
		serve();
	});
	pi.on("session_before_compact", async () => {
		compacting = true;
	});
	pi.on("session_compact", async () => {
		compacting = false;
	});
	pi.on("session_compact_failed", async () => {
		compacting = false;
	});
	pi.on("session_shutdown", async () => {
		context = undefined;
		stop();
	});
}
