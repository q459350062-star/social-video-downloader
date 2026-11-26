import http from "node:http";
import https from "node:https";
import net from "node:net";
import { URL } from "node:url";

const raw = process.env.RESIDENTIAL_PROXY_URL;
if (!raw) {
  console.error("[proxy-bridge] RESIDENTIAL_PROXY_URL is not configured");
  process.exit(1);
}

const upstream = new URL(/^[a-z]+:\/\//i.test(raw) ? raw : "http://" + raw);
const isSocks = upstream.protocol === "socks5:" || upstream.protocol === "socks5h:";
if (!isSocks && !["http:", "https:"].includes(upstream.protocol)) {
  console.error("[proxy-bridge] supported upstream protocols: SOCKS5, HTTP, HTTPS");
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

function readExactly(socket, length) {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    const onData = (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length < length) return;
      socket.off("data", onData);
      socket.off("error", onError);
      const result = buffer.subarray(0, length);
      const remainder = buffer.subarray(length);
      if (remainder.length) socket.unshift(remainder);
      resolve(result);
    };
    const onError = (error) => {
      socket.off("data", onData);
      reject(error);
    };
    socket.on("data", onData);
    socket.once("error", onError);
    socket.resume();
  });
}

async function socks5Connect(host, port) {
  const socket = net.connect(upstreamPort, upstream.hostname);
  await new Promise((resolve, reject) => {
    socket.once("connect", resolve);
    socket.once("error", reject);
  });

  try {
    socket.write(Buffer.from(username || password ? [0x05, 0x02, 0x00, 0x02] : [0x05, 0x01, 0x00]));
    const method = await readExactly(socket, 2);
    if (method[0] !== 0x05 || method[1] === 0xff) {
      throw new Error("SOCKS5 upstream rejected available authentication methods");
    }

    if (method[1] === 0x02) {
      const userBytes = Buffer.from(username, "utf8");
      const passBytes = Buffer.from(password, "utf8");
      if (!userBytes.length || userBytes.length > 255 || passBytes.length > 255) {
        throw new Error("SOCKS5 username/password length is invalid");
      }
      socket.write(Buffer.concat([
        Buffer.from([0x01, userBytes.length]),
        userBytes,
        Buffer.from([passBytes.length]),
        passBytes,
      ]));
      const authReply = await readExactly(socket, 2);
      if (authReply[0] !== 0x01 || authReply[1] !== 0x00) {
        throw new Error("SOCKS5 username/password authentication failed");
      }
    } else if (method[1] !== 0x00) {
      throw new Error("SOCKS5 upstream selected an unsupported authentication method");
    }

    let address;
    let addressType;
    if (net.isIPv4(host)) {
      addressType = 0x01;
      address = Buffer.from(host.split(".").map(Number));
    } else if (net.isIPv6(host)) {
      addressType = 0x04;
      // IPv6 literals are uncommon for the target services; pass as a domain
      // when supported by the upstream SOCKS5 implementation.
      const hostBytes = Buffer.from(host, "utf8");
      if (hostBytes.length > 255) throw new Error("SOCKS5 target host is too long");
      addressType = 0x03;
      address = Buffer.concat([Buffer.from([hostBytes.length]), hostBytes]);
    } else {
      const hostBytes = Buffer.from(host, "utf8");
      if (!hostBytes.length || hostBytes.length > 255) throw new Error("SOCKS5 target host is invalid");
      addressType = 0x03;
      address = Buffer.concat([Buffer.from([hostBytes.length]), hostBytes]);
    }

    const portBytes = Buffer.from([(port >> 8) & 0xff, port & 0xff]);
    socket.write(Buffer.concat([Buffer.from([0x05, 0x01, 0x00, addressType]), address, portBytes]));

    const reply = await readExactly(socket, 4);
    if (reply[0] !== 0x05 || reply[1] !== 0x00) {
      throw new Error("SOCKS5 CONNECT failed with reply code " + reply[1]);
    }
    let boundAddressLength;
    if (reply[3] === 0x01) boundAddressLength = 4;
    else if (reply[3] === 0x04) boundAddressLength = 16;
    else if (reply[3] === 0x03) boundAddressLength = (await readExactly(socket, 1))[0];
    else throw new Error("SOCKS5 returned an unknown address type");
    await readExactly(socket, boundAddressLength + 2);
    return socket;
  } catch (error) {
    socket.destroy();
    throw error;
  }
}

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
  if (isSocks) {
    res.writeHead(501, { "content-type": "text/plain" });
    res.end("SOCKS5 bridge accepts HTTPS CONNECT requests only");
    return;
  }
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

server.on("connect", async (req, clientSocket, head) => {
  try {
    const split = req.url.lastIndexOf(":");
    const host = req.url.slice(0, split).replace(/^\[|\]$/g, "");
    const port = Number(req.url.slice(split + 1));
    if (!host || !Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error("Invalid CONNECT target");
    }

    let upstreamSocket;
    if (isSocks) {
      upstreamSocket = await socks5Connect(host, port);
    } else {
      const proxyReq = transport.request(
        upstreamOptions("CONNECT", req.url, { Host: req.url })
      );
      upstreamSocket = await new Promise((resolve, reject) => {
        proxyReq.on("connect", (res, socket, upstreamHead) => {
          if (res.statusCode !== 200) {
            socket.destroy();
            reject(new Error("HTTP upstream CONNECT status " + res.statusCode));
            return;
          }
          if (upstreamHead?.length) clientSocket.write(upstreamHead);
          resolve(socket);
        });
        proxyReq.on("error", reject);
        proxyReq.end();
      });
    }

    clientSocket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (head?.length) upstreamSocket.write(head);
    upstreamSocket.pipe(clientSocket);
    clientSocket.pipe(upstreamSocket);
  } catch (error) {
    console.error("[proxy-bridge] CONNECT failed:", error.code || error.message);
    clientSocket.end("HTTP/1.1 502 Bad Gateway\r\n\r\n");
  }
});

server.listen(listenPort, "127.0.0.1", () => {
  console.log(
    "[proxy-bridge] listening on 127.0.0.1:" + listenPort +
    " via " + upstream.protocol + "//" + upstream.hostname + ":" + upstreamPort +
    (username || password ? " (authenticated)" : " (no auth)")
  );
});
