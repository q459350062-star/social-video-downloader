import http from "node:http";
import https from "node:https";
import { URL } from "node:url";

const raw = process.env.RESIDENTIAL_PROXY_URL;
if (!raw) {
  console.error("[proxy-bridge] RESIDENTIAL_PROXY_URL is not configured");
  process.exit(1);
}

const upstream = new URL(/^[a-z]+:\/\//i.test(raw) ? raw : "http://" + raw);
if (!["http:", "https:"].includes(upstream.protocol)) {
  console.error("[proxy-bridge] only HTTP/HTTPS upstream proxies are supported");
  process.exit(1);
}

const upstreamPort = Number(upstream.port || (upstream.protocol === "https:" ? 443 : 80));
const transport = upstream.protocol === "https:" ? https : http;
const username = decodeURIComponent(upstream.username || "");
const password = decodeURIComponent(upstream.password || "");
const proxyAuth = username || password
  ? "Basic " + Buffer.from(username + ":" + password).toString("base64")
  : undefined;
const listenPort = Number(process.env.LOCAL_PROXY_PORT || 3128);

function upstreamOptions(method, path, headers = {}) {
  const out = {
    hostname: upstream.hostname,
    port: upstreamPort,
    method,
    path,
    headers: { ...headers },
  };
  if (proxyAuth) out.headers["Proxy-Authorization"] = proxyAuth;
  return out;
}

const server = http.createServer((req, res) => {
  const headers = { ...req.headers, host: req.headers.host };
  const proxyReq = transport.request(
    upstreamOptions(req.method || "GET", req.url || "/", headers),
    (proxyRes) => {
      res.writeHead(proxyRes.statusCode || 502, proxyRes.headers);
      proxyRes.pipe(res);
    }
  );
  proxyReq.on("error", (error) => {
    console.error("[proxy-bridge] HTTP upstream error:", error.code || error.message);
    if (!res.headersSent) res.writeHead(502);
    res.end("Residential proxy upstream error");
  });
  req.pipe(proxyReq);
});

server.on("connect", (req, clientSocket, head) => {
  const proxyReq = transport.request(
    upstreamOptions("CONNECT", req.url, { Host: req.url })
  );
  proxyReq.on("connect", (res, upstreamSocket, upstreamHead) => {
    if (res.statusCode !== 200) {
      clientSocket.end(
        "HTTP/1.1 " + (res.statusCode || 502) + " " +
        (res.statusMessage || "Proxy Error") + "\r\n\r\n"
      );
      upstreamSocket.destroy();
      return;
    }
    clientSocket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (head?.length) upstreamSocket.write(head);
    if (upstreamHead?.length) clientSocket.write(upstreamHead);
    upstreamSocket.pipe(clientSocket);
    clientSocket.pipe(upstreamSocket);
  });
  proxyReq.on("error", (error) => {
    console.error("[proxy-bridge] CONNECT upstream error:", error.code || error.message);
    clientSocket.end("HTTP/1.1 502 Bad Gateway\r\n\r\n");
  });
  proxyReq.end();
});

server.listen(listenPort, "127.0.0.1", () => {
  console.log(
    "[proxy-bridge] listening on 127.0.0.1:" + listenPort +
    " via " + upstream.protocol + "//" + upstream.hostname + ":" + upstreamPort
  );
});
