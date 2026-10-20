import { InMemoryTransport } from "@modelcontextprotocol/server";
import { describe, expect, it } from "vitest";
import { buildServer } from "../src/mcp";

// Which tools a caller sees is the whole authorization model for /mcp: the
// OAuth provider advertises memex:read as required but does not enforce it.
async function toolNames(scopes: string[]): Promise<string[]> {
  const server = buildServer({} as Env, { scopes, client: "test" });
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair();
  await server.connect(serverSide);
  const replies = new Map<number, (msg: unknown) => void>();
  clientSide.onmessage = (msg: any) => replies.get(msg.id)?.(msg);
  await clientSide.start();
  const call = (id: number, method: string, params: object) =>
    new Promise<any>((resolve) => {
      replies.set(id, resolve);
      void clientSide.send({ jsonrpc: "2.0", id, method, params } as never);
    });
  await call(1, "initialize", {
    protocolVersion: "2025-11-25",
    capabilities: {},
    clientInfo: { name: "test", version: "0" },
  });
  await clientSide.send({ jsonrpc: "2.0", method: "notifications/initialized" } as never);
  const res = await call(2, "tools/list", {});
  await server.close();
  return (res.result?.tools ?? []).map((t: { name: string }) => t.name).sort();
}

describe("tool access follows token scopes", () => {
  it("read-only sees the read tools and no save_memo", async () => {
    expect(await toolNames(["memex:read"])).toEqual(["observations", "projects", "read", "search", "timeline"]);
  });
  it("write-only can save but not read", async () => {
    expect(await toolNames(["memex:write"])).toEqual(["save_memo"]);
  });
  it("no scopes, no tools", async () => {
    expect(await toolNames([])).toEqual([]);
  });
  it("both scopes see everything", async () => {
    expect(await toolNames(["memex:read", "memex:write"])).toHaveLength(6);
  });
});
